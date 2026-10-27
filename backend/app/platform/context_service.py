"""Offline context assembly. Byte capacity is not a provider token count or execution permit."""

import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import select
from app.platform.budget_service import signature
from app.platform.executor_service import lock_work
from app.platform import memory_service, story_service
from app.platform.models import (
    AssetRevision,
    ContextSnapshot,
    ContextMemory,
    MemoryProposal,
    MemoryDecision,
    MemoryEntry,
    MemorySource,
    now,
)
from app.platform.security import utc
from app.platform.works import audit


def encode(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def byte_size(value):
    return len(encode(value).encode("utf-8"))


def require_snapshot(session, work_id, snapshot_id):
    snapshot = session.get(ContextSnapshot, snapshot_id)
    if snapshot is None or snapshot.work_id != work_id:
        raise HTTPException(404, "该作品中没有此上下文快照")
    return snapshot


def references(session, proposal_ids):
    return list(
        session.scalars(
            select(MemorySource.revision_id).where(
                MemorySource.proposal_id.in_(proposal_ids)
            )
        )
    )


def current_memory(session, work_id, proposal_id):
    proposal = memory_service.require_proposal(session, work_id, proposal_id)
    decision = session.get(MemoryDecision, proposal_id)
    head = session.get(MemoryEntry, (work_id, proposal.entry_key))
    if (
        not decision
        or decision.outcome != "approved"
        or not head
        or head.proposal_id != proposal_id
    ):
        raise HTTPException(409, "只能选择当前已批准的正式记忆；请刷新并重新核对")
    return proposal


def memory_document(session, proposal):
    evidence = memory_service.sources(session, proposal)
    decision = session.get(MemoryDecision, proposal.id)
    return {
        "proposal_id": proposal.id,
        "entry_key": proposal.entry_key,
        "version": decision.version,
        "kind": proposal.kind,
        "title": proposal.title,
        "content": proposal.content,
        "story_state": story_service.state_data(session, proposal.id),
        "sources": [
            {key: row[key] for key in ("revision_id", "asset_id", "number", "sha256")}
            for row in evidence
        ],
    }, evidence


def time_matches(state, at):
    return (
        state is None
        or at is None
        or (
            state["valid_from"] <= at
            and (state["valid_until"] is None or at < state["valid_until"])
        )
    )


def create(db, work_id, body):
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        fingerprint = signature(body.model_dump())
        previous = session.scalar(
            select(ContextSnapshot).where(
                ContextSnapshot.work_id == work_id,
                ContextSnapshot.request_key == body.request_key,
            )
        )
        if previous:
            if previous.request_hash != fingerprint:
                raise HTTPException(
                    409, "同一提交标识不能替换上下文配置；当前输入请保留"
                )
            return previous.id
        proposals = [
            current_memory(session, work_id, row.proposal_id) for row in body.memories
        ]
        ids = set(references(session, [p.id for p in proposals])) | {
            body.input_revision_id
        }
        # One sorted asset lock set avoids inversions when memories share their input source.
        evidence = memory_service.source_rows(session, work_id, sorted(ids), lock=True)
        evidence_by_id = {row["revision_id"]: row for row in evidence}
        revision = session.get(AssetRevision, body.input_revision_id)
        payload = {
            "schema_version": "context/1.0",
            "work_id": work_id,
            "objective": body.objective,
            "story_at": body.story_at,
            "input": {
                "revision_id": revision.id,
                "asset_id": revision.asset_id,
                "number": revision.number,
                "sha256": revision.sha256,
                "title": revision.title,
                "content": revision.content,
                "status": "DRAFT",
            },
            "memories": [],
        }
        blockers = []
        if not evidence_by_id[revision.id]["current"]:
            blockers.append({"code": "INPUT_CHANGED", "reference_id": revision.id})
        candidates = []
        for selected, proposal in zip(body.memories, proposals):
            doc, sources = memory_document(session, proposal)
            problems = []
            if not sources or any(not row["current"] for row in sources):
                problems.append("SOURCE_CHANGED")
            if not time_matches(doc["story_state"], body.story_at):
                problems.append("STORY_TIME_MISMATCH")
            if selected.required:
                payload["memories"].append(doc)
                blockers.extend(
                    {"code": code, "reference_id": proposal.id} for code in problems
                )
            candidates.append((selected, doc, problems))
        required_bytes = byte_size(payload)
        if required_bytes > body.capacity_bytes:
            blockers.append(
                {"code": "REQUIRED_CAPACITY_EXCEEDED", "reference_id": None}
            )
        selections = []
        for position, (selected, doc, problems) in enumerate(candidates):
            included, omission = selected.required, None
            if not selected.required:
                omission = (
                    problems[0]
                    if problems
                    else "REQUIRED_CONTEXT_BLOCKED"
                    if blockers
                    else None
                )
                if omission is None:
                    trial = {**payload, "memories": [*payload["memories"], doc]}
                    if byte_size(trial) <= body.capacity_bytes:
                        payload = trial
                        included = True
                    else:
                        omission = "OPTIONAL_CAPACITY_EXCEEDED"
            selections.append(
                ContextMemory(
                    proposal_id=selected.proposal_id,
                    position=position,
                    required=selected.required,
                    included=included,
                    omission_reason=omission,
                )
            )
        document = encode(payload)
        snapshot = ContextSnapshot(
            work_id=work_id,
            title=body.title,
            input_revision_id=revision.id,
            story_at=body.story_at,
            capacity_bytes=body.capacity_bytes,
            required_bytes=required_bytes,
            used_bytes=len(document.encode("utf-8")),
            status="BLOCKED" if blockers else "READY",
            blockers=blockers,
            document=document,
            sha256=hashlib.sha256(document.encode("utf-8")).hexdigest(),
            request_key=body.request_key,
            request_hash=fingerprint,
        )
        session.add(snapshot)
        session.flush()
        for row in selections:
            row.snapshot_id = snapshot.id
        session.add_all(selections)
        audit(
            session,
            "context.created",
            work_id,
            snapshot.id,
            status=snapshot.status,
            sha256=snapshot.sha256,
            capacity_bytes=body.capacity_bytes,
            used_bytes=snapshot.used_bytes,
            required_bytes=required_bytes,
            included=sum(row.included for row in selections),
            omitted=sum(not row.included for row in selections),
        )
        return snapshot.id


def selection_rows(session, snapshot_id):
    return list(
        session.scalars(
            select(ContextMemory)
            .where(ContextMemory.snapshot_id == snapshot_id)
            .order_by(ContextMemory.position)
        )
    )


def freshness(session, snapshot, *, lock=False):
    rows = [row for row in selection_rows(session, snapshot.id) if row.included]
    ids = set(references(session, [row.proposal_id for row in rows])) | {
        snapshot.input_revision_id
    }
    evidence = memory_service.source_rows(
        session, snapshot.work_id, sorted(ids), lock=lock
    )
    issues = [
        {
            "code": "INPUT_CHANGED"
            if row["revision_id"] == snapshot.input_revision_id
            else "SOURCE_CHANGED",
            "reference_id": row["revision_id"],
        }
        for row in evidence
        if not row["current"]
    ]
    for row in rows:
        proposal = session.get(MemoryProposal, row.proposal_id)
        head = session.get(MemoryEntry, (snapshot.work_id, proposal.entry_key))
        if not head or head.proposal_id != proposal.id:
            issues.append({"code": "MEMORY_CHANGED", "reference_id": proposal.id})
    return {"fresh": not issues, "issues": issues, "checked_at": now()}


def data(session, snapshot, *, detail=False):
    result = {
        key: getattr(snapshot, key)
        for key in (
            "id",
            "title",
            "input_revision_id",
            "story_at",
            "capacity_bytes",
            "required_bytes",
            "used_bytes",
            "status",
            "blockers",
            "sha256",
        )
    }
    result["created_at"] = utc(snapshot.created_at)
    if detail:
        result["document"] = snapshot.document
        result["freshness"] = freshness(session, snapshot)
        result["memories"] = []
        for row in selection_rows(session, snapshot.id):
            proposal = session.get(MemoryProposal, row.proposal_id)
            result["memories"].append(
                {
                    "proposal_id": row.proposal_id,
                    "title": proposal.title,
                    "entry_key": proposal.entry_key,
                    "required": row.required,
                    "included": row.included,
                    "omission_reason": row.omission_reason,
                }
            )
    return result


def check(db, work_id, snapshot_id):
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        snapshot = require_snapshot(session, work_id, snapshot_id)
        result = freshness(session, snapshot, lock=True)
        result["assembly_ready"] = snapshot.status == "READY" and result["fresh"]
        # This observation is never an execution lease; future consumers must recheck.
        audit(
            session,
            "context.checked",
            work_id,
            snapshot.id,
            fresh=result["fresh"],
            assembly_ready=result["assembly_ready"],
            issues=result["issues"],
        )
        return result
