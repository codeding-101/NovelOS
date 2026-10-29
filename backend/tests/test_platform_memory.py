from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform import memory_service as memory, task_service as tasks
from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.memory_protocol import DecisionInput, ProposalInput
from app.platform.models import (
    Asset,
    AssetRevision,
    MemoryProposal,
    MemoryDecision,
    MemoryEntry,
    MemorySource,
    now,
)
from app.platform.works import append_revision
from platform_fixtures import signed_in
from test_platform_tasks import task_input, result


def draft(body, **changes):
    return ProposalInput(
        **{
            "entry_key": "人物-林舟",
            "kind": "character",
            "title": "林舟身份",
            "content": "林舟是一名船医。",
            "source_revision_ids": [body.input_revision_id],
            "request_key": "proposal-1",
            **changes,
        }
    )


def decision(outcome="approved", reason="已核对来源，采纳此设定"):
    return DecisionInput(outcome=outcome, reason=reason)


def detail(db, work_id, proposal_id):
    with db.sessions() as session:
        return memory.data(
            session, memory.require_proposal(session, work_id, proposal_id)
        )


def revise_source(db, revision_id):
    with db.sessions.begin() as session:
        revision = session.get(AssetRevision, revision_id)
        asset = session.get(Asset, revision.asset_id)
        return append_revision(
            session, asset, asset.head_version, "更新后的来源", "林舟曾任船医。"
        ).id


def test_candidate_isolation_approval_idempotence_and_provenance(platform):
    settings, db = platform
    body = task_input(db)
    request = draft(body)
    proposal = memory.create(db, body.work_id, request)
    assert memory.create(db, body.work_id, request) == proposal
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MemoryEntry)) == 0
    with TestClient(create_app(settings, db)) as client:
        signed_in(client, settings)
        prefix = f"/api/v1/works/{body.work_id}/memory"
        assert client.get(prefix).json() == []
        assert len(client.get(prefix + "/proposals").json()) == 1
        approved = memory.decide(db, body.work_id, proposal, decision())
        assert approved["is_current"] and approved["version"] == 1
        assert memory.decide(db, body.work_id, proposal, decision())["id"] == proposal
        assert client.get(prefix + "?q=船医").json()[0]["id"] == proposal
        assert client.get(prefix + "?q=%25").json() == []
        assert client.get(prefix + "?kind=event").json() == []
        assert client.get(prefix + "/proposals").json() == []
        assert len(client.get(prefix + "/history?entry_key=人物-林舟").json()) == 1
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(MemoryDecision)) == 1
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1
        source = session.get(AssetRevision, body.input_revision_id)
        assert approved["sources"][0]["sha256"] == source.sha256
        assert source.status == "DRAFT"
    with pytest.raises(HTTPException):
        memory.create(db, body.work_id, draft(body, content="不同内容"))
    with pytest.raises(HTTPException):
        memory.decide(db, body.work_id, proposal, decision("rejected"))


def test_replacement_history_and_old_approval_retry_never_rewinds(platform):
    settings, db = platform
    body = task_input(db)
    first = memory.create(db, body.work_id, draft(body))
    memory.decide(db, body.work_id, first, decision())
    second = memory.create(
        db,
        body.work_id,
        draft(
            body,
            request_key="second",
            base_proposal_id=first,
            content="林舟已离开船队。",
        ),
    )
    memory.decide(db, body.work_id, second, decision())
    assert memory.decide(db, body.work_id, first, decision())["is_current"] is False
    assert detail(db, body.work_id, second)["version"] == 2
    with TestClient(create_app(settings, db)) as client:
        signed_in(client, settings)
        prefix = f"/api/v1/works/{body.work_id}/memory"
        assert client.get(prefix).json()[0]["id"] == second
        assert [
            r["version"]
            for r in client.get(prefix + "/history?entry_key=人物-林舟").json()
        ] == [2, 1]
    with pytest.raises(HTTPException):
        memory.create(db, body.work_id, draft(body, request_key="no-base"))


def test_competing_approvals_have_single_winner(platform):
    _, db = platform
    body = task_input(db)
    first = memory.create(db, body.work_id, draft(body))
    second = memory.create(
        db, body.work_id, draft(body, request_key="second", content="林舟是领航员。")
    )
    barrier = Barrier(2)

    def approve(proposal_id):
        barrier.wait(timeout=5)
        try:
            return memory.decide(db, body.work_id, proposal_id, decision())["id"]
        except HTTPException:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, [first, second]))
    assert sum(r is not None for r in results) == 1
    losing = second if results[0] else first
    assert detail(db, body.work_id, losing)["blockers"] == ["MEMORY_CHANGED"]
    memory.decide(db, body.work_id, losing, decision("rejected", "其他提案已生效"))
    with db.sessions() as session:
        assert session.get(MemoryEntry, (body.work_id, "人物-林舟")).version == 1


def test_source_changes_invalidate_approval_and_preserve_evidence(platform):
    _, db = platform
    body = task_input(db)
    proposal = memory.create(db, body.work_id, draft(body))
    fresh = revise_source(db, body.input_revision_id)
    assert "SOURCE_CHANGED" in detail(db, body.work_id, proposal)["blockers"]
    with pytest.raises(HTTPException):
        memory.decide(db, body.work_id, proposal, decision())
    with pytest.raises(HTTPException):
        memory.create(db, body.work_id, draft(body, request_key="stale-source"))
    updated = memory.create(
        db,
        body.work_id,
        draft(body, request_key="fresh-source", source_revision_ids=[fresh]),
    )
    memory.decide(db, body.work_id, updated, decision())
    revise_source(db, fresh)
    canonical = detail(db, body.work_id, updated)
    assert canonical["is_current"] and canonical["source_changed"]
    assert (
        detail(db, body.work_id, proposal)["sources"][0]["revision_id"]
        == body.input_revision_id
    )


def test_expired_proposal_can_only_be_rejected(platform, monkeypatch):
    _, db = platform
    body = task_input(db)
    proposal = memory.create(db, body.work_id, draft(body, ttl_seconds=60))
    future = now() + timedelta(seconds=61)
    monkeypatch.setattr(memory, "now", lambda: future)
    assert "PROPOSAL_EXPIRED" in detail(db, body.work_id, proposal)["blockers"]
    with pytest.raises(HTTPException):
        memory.decide(db, body.work_id, proposal, decision())
    memory.decide(db, body.work_id, proposal, decision("rejected", "已过期"))
    assert detail(db, body.work_id, proposal)["status"] == "rejected"


def test_cross_work_sources_base_and_submission_are_rejected(platform):
    _, db = platform
    own, foreign = task_input(db), task_input(db)
    with pytest.raises(HTTPException):
        memory.create(
            db, own.work_id, draft(own, source_revision_ids=[foreign.input_revision_id])
        )
    proposal = memory.create(db, foreign.work_id, draft(foreign))
    memory.decide(db, foreign.work_id, proposal, decision())
    with pytest.raises(HTTPException):
        memory.create(db, own.work_id, draft(own, base_proposal_id=proposal))
    with pytest.raises(HTTPException):
        memory.decide(db, own.work_id, proposal, decision())
    with pytest.raises(HTTPException):
        memory.create(db, own.work_id, draft(own, submission_id="missing"))


def test_task_result_link_requires_accepted_same_work_and_input(platform):
    _, db = platform
    body = task_input(db)
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, "human")
    tasks.submit(db, task_id, result(envelope))
    from app.platform.models import TaskSubmission

    with db.sessions() as session:
        submission_id = session.scalar(
            select(TaskSubmission.id).where(TaskSubmission.task_id == task_id)
        )
    proposal = memory.create(db, body.work_id, draft(body, submission_id=submission_id))
    assert detail(db, body.work_id, proposal)["submission_id"] == submission_id
    other = task_input(db)
    with pytest.raises(HTTPException):
        memory.create(db, other.work_id, draft(other, submission_id=submission_id))
    # A rejected/late task result cannot become a memory source.
    late_id = tasks.create_task(db, other)
    late_envelope = tasks.try_claim(db, late_id, "human")
    with db.sessions() as session:
        from app.platform.models import CreativeTask

        version = session.get(CreativeTask, late_id).version
    tasks.control(db, late_id, "cancel", version)
    tasks.submit(db, late_id, result(late_envelope))
    with db.sessions() as session:
        late_submission = session.scalar(
            select(TaskSubmission.id).where(TaskSubmission.task_id == late_id)
        )
    with pytest.raises(HTTPException):
        memory.create(db, other.work_id, draft(other, submission_id=late_submission))


def test_decision_failure_rolls_back_head_and_audit(platform, monkeypatch):
    _, db = platform
    body = task_input(db)
    proposal = memory.create(db, body.work_id, draft(body))

    original_audit = memory.audit

    def fail(*args, **kwargs):
        original_audit(*args, **kwargs)
        args[0].flush()
        raise RuntimeError("Audit persistence failed")

    monkeypatch.setattr(memory, "audit", fail)
    with pytest.raises(RuntimeError):
        memory.decide(db, body.work_id, proposal, decision())
    with db.sessions() as session:
        assert session.get(MemoryEntry, (body.work_id, "人物-林舟")) is None
        assert session.get(MemoryDecision, proposal) is None


@pytest.mark.parametrize(
    "table", ["memory_proposals", "memory_sources", "memory_decisions"]
)
def test_immutable_memory_records(platform, table):
    _, db = platform
    body = task_input(db)
    proposal = memory.create(db, body.work_id, draft(body))
    memory.decide(db, body.work_id, proposal, decision())
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(text(f"DELETE FROM {table}"))
    column = (
        "title"
        if table == "memory_proposals"
        else "revision_id"
        if table == "memory_sources"
        else "reason"
    )
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(text(f"UPDATE {table} SET {column} = {column}"))


def test_api_authorization_validation_and_pagination(platform):
    settings, db = platform
    body = task_input(db)
    prefix = f"/api/v1/works/{body.work_id}/memory"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(prefix).status_code == 401
        headers = signed_in(client, settings)
        payload = draft(body).model_dump()
        assert client.post(prefix + "/proposals", json=payload).status_code == 403
        assert (
            client.post(
                prefix + "/proposals",
                json={**payload, "ttl_seconds": True},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                prefix + "/proposals",
                json={**payload, "source_revision_ids": []},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                prefix + "/proposals", json={**payload, "content": " "}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                prefix + "/proposals",
                json=payload,
                headers={**headers, "Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        created = client.post(prefix + "/proposals", json=payload, headers=headers)
        assert created.status_code == 201
        proposal_id = created.json()["id"]
        assert client.get(prefix + "/proposals?offset=1").json() == []
        assert client.get(prefix + "?limit=101").status_code == 422
        assert client.get(prefix + "/proposals?status=madeup").status_code == 422
        assert (
            client.post(
                prefix + f"/proposals/{proposal_id}/decision",
                json=decision().model_dump(),
                headers=headers,
            ).status_code
            == 200
        )
    with TestClient(create_app(settings, db)) as agent:
        assert (
            agent.get(
                prefix, headers={"Authorization": "Bearer agent-token"}
            ).status_code
            == 401
        )


def test_approval_waits_for_source_writer_then_rechecks(platform, monkeypatch):
    _, db = platform
    if db.engine.dialect.name == "sqlite":
        # SQLite serializes the whole write transaction; both operations still use the real service.
        body = task_input(db)
        proposal = memory.create(db, body.work_id, draft(body))
        revise_source(db, body.input_revision_id)
        with pytest.raises(HTTPException):
            memory.decide(db, body.work_id, proposal, decision())
        return
    body = task_input(db)
    proposal = memory.create(db, body.work_id, draft(body))
    writer_ready, approval_ready = Event(), Event()
    original = memory.source_rows

    def observed(*args, **kwargs):
        if kwargs.get("lock"):
            approval_ready.set()
        return original(*args, **kwargs)

    monkeypatch.setattr(memory, "source_rows", observed)

    def writer():
        with db.sessions.begin() as session:
            revision = session.get(AssetRevision, body.input_revision_id)
            asset = session.get(Asset, revision.asset_id)
            append_revision(session, asset, 1, "新来源", "来源已修改")
            session.flush()
            writer_ready.set()
            assert approval_ready.wait(timeout=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        writing = pool.submit(writer)
        assert writer_ready.wait(timeout=10)
        approving = pool.submit(memory.decide, db, body.work_id, proposal, decision())
        writing.result(timeout=15)
        with pytest.raises(HTTPException):
            approving.result(timeout=15)
    assert detail(db, body.work_id, proposal)["status"] == "pending"


def test_upgrade_does_not_promote_assets_or_task_results(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0010_monthly_quotas")
    body = task_input(db)
    db.upgrade()
    assert db.require_current() == "0015_summary_candidates"
    with db.sessions() as session:
        assert session.get(AssetRevision, body.input_revision_id).status == "DRAFT"
        for table in (MemoryEntry, MemoryProposal, MemorySource, MemoryDecision):
            assert session.scalar(select(func.count()).select_from(table)) == 0
