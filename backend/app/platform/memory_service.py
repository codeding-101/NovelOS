"""Owner-reviewed memory. Candidates never enter the canonical listing automatically."""

from datetime import timedelta
from fastapi import HTTPException
from sqlalchemy import select, update

from app.platform.budget_service import signature
from app.platform.executor_service import lock_work
from app.platform import story_service
from app.platform.models import (
    Asset,
    AssetRevision,
    CreativeTask,
    MemoryDecision,
    MemoryEntry,
    MemoryProposal,
    MemorySource,
    MemoryStoryState,
    TaskSubmission,
    now,
)
from app.platform.security import utc
from app.platform.works import audit


def require_proposal(session, work_id, proposal_id):
    item = session.get(MemoryProposal, proposal_id)
    if item is None or item.work_id != work_id:
        raise HTTPException(404, "该作品中没有此记忆提案")
    return item


def source_rows(session, work_id, revision_ids, *, lock=False):
    rows = list(
        session.scalars(
            select(AssetRevision)
            .where(AssetRevision.id.in_(revision_ids))
            .order_by(AssetRevision.asset_id, AssetRevision.id)
        )
    )
    if len(rows) != len(revision_ids):
        raise HTTPException(404, "来源版本不存在")
    if lock:
        # Same asset row used by append_revision; sorted locks prevent multi-source inversion.
        for asset_id in sorted({row.asset_id for row in rows}):
            session.execute(
                update(Asset)
                .where(Asset.id == asset_id)
                .values(head_version=Asset.head_version)
                .execution_options(synchronize_session=False)
            )
    result = []
    for row in rows:
        asset = session.get(Asset, row.asset_id, populate_existing=True)
        if asset.work_id != work_id:
            raise HTTPException(404, "来源版本不属于该作品")
        result.append(
            {
                "revision_id": row.id,
                "asset_id": row.asset_id,
                "title": row.title,
                "number": row.number,
                "sha256": row.sha256,
                "current": row.number == asset.head_version,
            }
        )
    return result


def sources(session, proposal, *, lock=False):
    ids = list(
        session.scalars(
            select(MemorySource.revision_id).where(
                MemorySource.proposal_id == proposal.id
            )
        )
    )
    return source_rows(session, proposal.work_id, ids, lock=lock)


def blockers(session, proposal, evidence):
    result = []
    head = session.get(MemoryEntry, (proposal.work_id, proposal.entry_key))
    if (head.proposal_id if head else None) != proposal.base_proposal_id:
        result.append("MEMORY_CHANGED")
    if utc(proposal.expires_at) <= now():
        result.append("PROPOSAL_EXPIRED")
    if not evidence or any(not row["current"] for row in evidence):
        result.append("SOURCE_CHANGED")
    if story_service.conflicts(session, proposal):
        result.append("STORY_STATE_OVERLAP")
    return result


def data(session, proposal, *, include_content=True):
    decision = session.get(MemoryDecision, proposal.id)
    evidence = sources(session, proposal)
    head = session.get(MemoryEntry, (proposal.work_id, proposal.entry_key))
    return {
        "id": proposal.id,
        "entry_key": proposal.entry_key,
        "kind": proposal.kind,
        "story_state": story_service.state_data(session, proposal.id),
        "story_conflicts": [
            {"id": row.id, "entry_key": row.entry_key, "title": row.title}
            for row in story_service.conflicts(session, proposal)
        ]
        if not decision
        else [],
        "title": proposal.title,
        **({"content": proposal.content} if include_content else {}),
        "base_proposal_id": proposal.base_proposal_id,
        "submission_id": proposal.submission_id,
        "status": decision.outcome if decision else "pending",
        "version": decision.version if decision else None,
        "decision_reason": decision.reason if decision else None,
        "decided_at": utc(decision.created_at) if decision else None,
        "is_current": bool(head and head.proposal_id == proposal.id),
        "blockers": blockers(session, proposal, evidence) if not decision else [],
        "source_changed": any(not row["current"] for row in evidence),
        "sources": evidence,
        "created_at": utc(proposal.created_at),
        "expires_at": utc(proposal.expires_at),
    }


def create(db, work_id, body):
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        payload = body.model_dump()
        # Preserve pre-0012 request hashes when no structured state was supplied.
        if body.story_state is None:
            payload.pop("story_state")
        fingerprint = signature(payload)
        previous = session.scalar(
            select(MemoryProposal).where(
                MemoryProposal.work_id == work_id,
                MemoryProposal.request_key == body.request_key,
            )
        )
        if previous:
            if previous.request_hash != fingerprint:
                raise HTTPException(409, "同一提交标识不能替换提案内容")
            return previous.id
        head = session.get(MemoryEntry, (work_id, body.entry_key))
        if (head.proposal_id if head else None) != body.base_proposal_id:
            raise HTTPException(
                409, "正式记忆已变化，请基于最新版本重新提交；当前输入请保留"
            )
        evidence = source_rows(session, work_id, body.source_revision_ids, lock=True)
        if any(not row["current"] for row in evidence):
            raise HTTPException(409, "来源已有新版本，请核对后重新选择来源")
        if body.submission_id:
            submission = session.get(TaskSubmission, body.submission_id)
            task = session.get(CreativeTask, submission.task_id) if submission else None
            if (
                task is None
                or task.work_id != work_id
                or submission.disposition != "ACCEPTED"
            ):
                raise HTTPException(409, "只可关联同作品已接收的任务候选")
            if task.input_revision_id not in body.source_revision_ids:
                raise HTTPException(409, "关联任务候选时须包含其输入版本来源")
        proposal = MemoryProposal(
            work_id=work_id,
            **body.model_dump(
                exclude={"source_revision_ids", "ttl_seconds", "story_state"}
            ),
            request_hash=fingerprint,
            expires_at=now() + timedelta(seconds=body.ttl_seconds),
        )
        session.add(proposal)
        session.flush()
        if body.story_state is not None:
            session.add(
                MemoryStoryState(
                    proposal_id=proposal.id, **body.story_state.model_dump()
                )
            )
        session.add_all(
            [
                MemorySource(proposal_id=proposal.id, revision_id=row["revision_id"])
                for row in evidence
            ]
        )
        audit(
            session,
            "memory.proposed",
            work_id,
            proposal.id,
            entry_key=proposal.entry_key,
            base_proposal_id=proposal.base_proposal_id,
        )
        return proposal.id


def decide(db, work_id, proposal_id, body):
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        proposal = require_proposal(session, work_id, proposal_id)
        previous = session.get(MemoryDecision, proposal_id)
        if previous:
            if previous.outcome != body.outcome or previous.reason != body.reason:
                raise HTTPException(409, "该提案已有审核决定，不能覆盖")
            return data(session, proposal)
        evidence = sources(session, proposal, lock=body.outcome == "approved")
        version = None
        if body.outcome == "approved":
            invalid = blockers(session, proposal, evidence)
            if invalid:
                raise HTTPException(
                    409,
                    "提案不能批准（"
                    + "、".join(invalid)
                    + "）：请核对来源、版本、期限及故事状态区间后重新提交",
                )
            head = session.get(MemoryEntry, (work_id, proposal.entry_key))
            version = head.version + 1 if head else 1
            if head:
                head.proposal_id, head.version = proposal.id, version
            else:
                session.add(
                    MemoryEntry(
                        work_id=work_id,
                        entry_key=proposal.entry_key,
                        proposal_id=proposal.id,
                        version=version,
                    )
                )
        session.add(
            MemoryDecision(
                proposal_id=proposal.id,
                outcome=body.outcome,
                reason=body.reason,
                version=version,
            )
        )
        audit(
            session,
            "memory." + body.outcome,
            work_id,
            proposal.id,
            entry_key=proposal.entry_key,
            base_proposal_id=proposal.base_proposal_id,
            version=version,
            reason=body.reason,
        )
        session.flush()
        return data(session, proposal)
