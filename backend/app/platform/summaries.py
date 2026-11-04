"""Immutable, source-bound summary candidates; never canonical facts."""

import hashlib
import json
from datetime import timezone
from typing import Literal
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, field_validator, model_validator
from sqlalchemy import select
from app.platform.budget_service import signature
from app.platform.executor_service import lock_work
from app.platform.memory_service import source_rows
from app.platform.memory_protocol import DecisionInput
from app.platform.models import SummaryCandidate, SummaryDecision
from app.platform.security import utc
from app.platform.task_protocol import Contract
from app.platform.works import audit, require_work

LEVELS = ("scene", "chapter", "volume", "book")
router = APIRouter(prefix="/api/v1/works/{work_id}/summaries", tags=["summaries"])


class SummaryInput(Contract):
    level: Literal["scene", "chapter", "volume", "book"]
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(min_length=1, max_length=32768)
    narrative_notes: str = Field(default="", max_length=16384)
    unresolved: str = Field(default="", max_length=16384)
    source_revision_ids: list[str] = Field(default_factory=list, max_length=20)
    child_summary_ids: list[str] = Field(default_factory=list, max_length=20)
    request_key: str = Field(min_length=1, max_length=128)

    @field_validator("title", "content", "request_key")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Must not be blank")
        return value

    @field_validator("source_revision_ids", "child_summary_ids")
    @classmethod
    def identifiers(cls, values):
        if len(set(values)) != len(values) or any(not v or len(v) > 32 for v in values):
            raise ValueError("Invalid or duplicate reference")
        return values

    @model_validator(mode="after")
    def evidence(self):
        if not self.source_revision_ids and not self.child_summary_ids:
            raise ValueError("Sources required")
        return self


def require_summary(session, work_id, summary_id):
    row = session.get(SummaryCandidate, summary_id)
    if row is None or row.work_id != work_id:
        raise HTTPException(404, "该作品中没有此摘要")
    return row


class SummaryReview(DecisionInput):
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def decision_data(decision):
    return {
        "outcome": decision.outcome,
        "reason": decision.reason,
        "created_at": utc(decision.created_at).astimezone(timezone.utc),
    }


def data(session, row):
    document = json.loads(row.document)
    evidence = source_rows(
        session, row.work_id, [s["revision_id"] for s in document["sources"]]
    )
    decision = session.get(SummaryDecision, row.id)
    return {
        "id": row.id,
        "work_id": row.work_id,
        "created_at": row.created_at,
        "status": decision.outcome.upper() if decision else "CANDIDATE",
        "decision": decision_data(decision) if decision else None,
        "sha256": row.sha256,
        "document": document,
        "document_text": row.document,
        "source_changed": any(not s["current"] for s in evidence),
        "changed_revision_ids": [
            s["revision_id"] for s in evidence if not s["current"]
        ],
    }


def create(db, work_id, body):
    fingerprint = signature(body.model_dump())
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        previous = session.scalar(
            select(SummaryCandidate).where(
                SummaryCandidate.work_id == work_id,
                SummaryCandidate.request_key == body.request_key,
            )
        )
        if previous:
            if previous.request_hash != fingerprint:
                raise HTTPException(409, "同一提交标识不能替换摘要内容")
            return previous.id
        sources = set(body.source_revision_ids)
        children = []
        for child_id in body.child_summary_ids:
            child = require_summary(session, work_id, child_id)
            document = json.loads(child.document)
            if LEVELS.index(document["level"]) != LEVELS.index(body.level) - 1:
                raise HTTPException(409, "上级摘要只能引用紧邻下一层摘要")
            sources.update(s["revision_id"] for s in document["sources"])
            children.append({"id": child.id, "sha256": child.sha256})
        if len(sources) > 200:
            raise HTTPException(409, "摘要来源超过当前 200 个版本上限，请缩小范围")
        evidence = source_rows(session, work_id, sorted(sources), lock=True)
        if any(not s["current"] for s in evidence):
            raise HTTPException(409, "摘要依据已有新版本，请重新核对原文与下级摘要")
        document = body.model_dump(
            exclude={"request_key", "source_revision_ids", "child_summary_ids"}
        )
        document.update(
            {
                "schema_version": "1.0",
                "status": "CANDIDATE",
                "direct_source_revision_ids": body.source_revision_ids,
                "children": children,
                "sources": [
                    {k: v for k, v in s.items() if k != "current"} for s in evidence
                ],
            }
        )
        serialized = json.dumps(
            document, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        row = SummaryCandidate(
            work_id=work_id,
            document=serialized,
            sha256=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            request_key=body.request_key,
            request_hash=fingerprint,
        )
        session.add(row)
        session.flush()
        audit(
            session,
            "summary.created",
            work_id,
            row.id,
            level=body.level,
            sha256=row.sha256,
        )
        return row.id


@router.post("", status_code=201)
def propose(work_id: str, body: SummaryInput, request: Request):
    return {"id": create(request.app.state.database, work_id, body)}


@router.post("/{summary_id}/decision")
def decide(work_id: str, summary_id: str, body: SummaryReview, request: Request):
    with request.app.state.database.sessions.begin() as session:
        lock_work(session, work_id)
        row = require_summary(session, work_id, summary_id)
        if row.sha256 != body.expected_sha256:
            raise HTTPException(409, "请核对摘要版本后重新审核")
        previous = session.get(SummaryDecision, row.id)
        if previous:
            if (previous.outcome, previous.reason) != (body.outcome, body.reason):
                raise HTTPException(409, "摘要已有审核决定，请查看原记录")
            return decision_data(previous)
        if body.outcome == "approved":
            document = json.loads(row.document)
            for child in document["children"]:
                decision = session.get(SummaryDecision, child["id"])
                if decision is None or decision.outcome != "approved":
                    raise HTTPException(409, "批准上级摘要前须批准全部下级摘要")
            revisions = [ref["revision_id"] for ref in document["sources"]]
            if any(
                not ref["current"]
                for ref in source_rows(session, work_id, revisions, lock=True)
            ):
                raise HTTPException(409, "摘要来源已变化，请核对原文并创建新候选")
        decision = SummaryDecision(
            summary_id=row.id, outcome=body.outcome, reason=body.reason
        )
        session.add(decision)
        audit(
            session,
            "summary." + body.outcome,
            work_id,
            row.id,
            sha256=row.sha256,
            reason=body.reason,
        )
        session.flush()
        return decision_data(decision)


@router.get("")
def listing(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            data(session, row)
            for row in session.scalars(
                select(SummaryCandidate)
                .where(SummaryCandidate.work_id == work_id)
                .order_by(SummaryCandidate.created_at.desc(), SummaryCandidate.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.get("/{summary_id}")
def detail(work_id: str, summary_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        return data(session, require_summary(session, work_id, summary_id))
