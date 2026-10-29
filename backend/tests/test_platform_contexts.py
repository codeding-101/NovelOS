from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Barrier, Event

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform import context_service as context, memory_service as memory
from app.platform.api import create_app
from app.platform.context_protocol import SnapshotInput
from app.platform.database import migration_config
from app.platform.models import (
    Asset,
    AssetRevision,
    AuditEvent,
    ContextMemory,
    ContextSnapshot,
)
from app.platform.works import append_revision, create_asset, AssetInput
from platform_fixtures import signed_in
from test_platform_memory import draft, decision, revise_source
from test_platform_story import state
from test_platform_tasks import task_input


def config(body, **changes):
    return SnapshotInput(
        **{
            "title": "章节分析上下文",
            "objective": "核对林舟的身份与行踪。",
            "input_revision_id": body.input_revision_id,
            "capacity_bytes": 65536,
            "request_key": "context-1",
            **changes,
        }
    )


def approved(db, body, key="身份", **changes):
    proposal = memory.create(
        db, body.work_id, draft(body, entry_key=key, request_key=key, **changes)
    )
    memory.decide(db, body.work_id, proposal, decision())
    return proposal


def detail(db, work_id, snapshot_id):
    with db.sessions() as session:
        return context.data(
            session,
            context.require_snapshot(session, work_id, snapshot_id),
            detail=True,
        )


def choose(proposal_id, required=True):
    return {"proposal_id": proposal_id, "required": required}


def test_exact_utf8_capacity_hash_and_no_truncation(platform):
    _, db = platform
    body = task_input(db)
    memory_id = approved(db, body, content='船医林舟 said "hello"。\n风🌧️')
    request = config(body, memories=[choose(memory_id)])
    first = context.create(db, body.work_id, request)
    row = detail(db, body.work_id, first)
    encoded = row["document"].encode("utf-8")
    assert row["used_bytes"] == row["required_bytes"] == len(encoded)
    assert len(encoded) > len(row["document"])
    assert row["sha256"] == hashlib.sha256(encoded).hexdigest()
    assert json.loads(row["document"])["memories"][0]["content"].endswith("风🌧️")
    assert json.loads(row["document"])["input"]["content"] == "山风\n雨声\n"
    exact = context.create(
        db,
        body.work_id,
        config(
            body,
            memories=[choose(memory_id)],
            capacity_bytes=len(encoded),
            request_key="exact",
        ),
    )
    blocked = context.create(
        db,
        body.work_id,
        config(
            body,
            memories=[choose(memory_id)],
            capacity_bytes=len(encoded) - 1,
            request_key="one-less",
        ),
    )
    assert detail(db, body.work_id, exact)["status"] == "READY"
    rejected = detail(db, body.work_id, blocked)
    assert rejected["status"] == "BLOCKED"
    assert rejected["blockers"] == [
        {"code": "REQUIRED_CAPACITY_EXCEEDED", "reference_id": None}
    ]
    assert (
        rejected["document"] == row["document"]
    )  # Entire required content retained for diagnosis.
    assert context.check(db, body.work_id, exact)["assembly_ready"]
    assert not context.check(db, body.work_id, blocked)["assembly_ready"]


def test_required_first_optional_priority_and_whole_item_omissions(platform):
    _, db = platform
    body = task_input(db)
    required = approved(db, body, "必须", content="已确认世界规则")
    large = approved(db, body, "过大", content="长记忆" * 200)
    small = approved(db, body, "较短", content="短事实")
    base_id = context.create(
        db, body.work_id, config(body, memories=[choose(required), choose(small)])
    )
    allowance = detail(db, body.work_id, base_id)["used_bytes"]
    request = config(
        body,
        memories=[choose(large, False), choose(required), choose(small, False)],
        capacity_bytes=allowance,
        request_key="pack",
    )
    snapshot = context.create(db, body.work_id, request)
    row = detail(db, body.work_id, snapshot)
    assert row["status"] == "READY" and row["used_bytes"] == allowance
    assert [
        item["proposal_id"] for item in json.loads(row["document"])["memories"]
    ] == [required, small]
    assert [item["included"] for item in row["memories"]] == [False, True, True]
    assert row["memories"][0]["omission_reason"] == "OPTIONAL_CAPACITY_EXCEEDED"
    # When both fit separately but not together, selection order is the priority.
    first = approved(db, body, "甲甲", content="甲" * 100)
    second = approved(db, body, "乙乙", content="乙" * 100)
    one = context.create(
        db, body.work_id, config(body, memories=[choose(first)], request_key="measure")
    )
    limit = detail(db, body.work_id, one)["used_bytes"]
    for index, order in enumerate(([first, second], [second, first])):
        item = context.create(
            db,
            body.work_id,
            config(
                body,
                memories=[choose(p, False) for p in order],
                capacity_bytes=limit,
                request_key=f"order-{index}",
            ),
        )
        actual = json.loads(detail(db, body.work_id, item)["document"])
        assert [m["proposal_id"] for m in actual["memories"]] == [order[0]]


def test_mandatory_objective_and_input_block_optional_assembly(platform):
    _, db = platform
    body = task_input(db)
    proposal = approved(db, body)
    snapshot = context.create(
        db,
        body.work_id,
        config(body, capacity_bytes=1, memories=[choose(proposal, False)]),
    )
    row = detail(db, body.work_id, snapshot)
    assert row["status"] == "BLOCKED"
    assert row["memories"][0]["omission_reason"] == "REQUIRED_CONTEXT_BLOCKED"
    assert json.loads(row["document"])["memories"] == []
    assert json.loads(row["document"])["objective"] == config(body).objective


def test_story_time_required_block_optional_omission_and_plain_facts(platform):
    _, db = platform
    body = task_input(db)
    timed = approved(db, body, story_state=state(10, 20))
    plain = approved(db, body, "未标时间")
    for instant, ready in ((10, True), (19, True), (20, False), (None, True)):
        snapshot = context.create(
            db,
            body.work_id,
            config(
                body,
                story_at=instant,
                memories=[choose(timed)],
                request_key=f"time-{instant}",
            ),
        )
        row = detail(db, body.work_id, snapshot)
        assert (row["status"] == "READY") == ready
        if not ready:
            assert row["blockers"][0]["code"] == "STORY_TIME_MISMATCH"
    snapshot = context.create(
        db,
        body.work_id,
        config(
            body,
            story_at=20,
            memories=[choose(timed, False), choose(plain)],
            request_key="skip-time",
        ),
    )
    row = detail(db, body.work_id, snapshot)
    assert row["status"] == "READY"
    assert row["memories"][0]["omission_reason"] == "STORY_TIME_MISMATCH"
    assert json.loads(row["document"])["memories"][0]["proposal_id"] == plain


def test_only_current_canonical_memories_and_same_work_sources(platform):
    _, db = platform
    body = task_input(db)
    candidate = memory.create(db, body.work_id, draft(body))
    with pytest.raises(HTTPException):
        context.create(db, body.work_id, config(body, memories=[choose(candidate)]))
    memory.decide(db, body.work_id, candidate, decision("rejected"))
    with pytest.raises(HTTPException):
        context.create(
            db, body.work_id, config(body, memories=[choose(candidate, False)])
        )
    old = approved(db, body, "正式")
    replacement = memory.create(
        db,
        body.work_id,
        draft(body, entry_key="正式", request_key="replace", base_proposal_id=old),
    )
    memory.decide(db, body.work_id, replacement, decision())
    with pytest.raises(HTTPException):
        context.create(db, body.work_id, config(body, memories=[choose(old)]))
    other = task_input(db)
    foreign = approved(db, other)
    for request in (
        config(body, memories=[choose(foreign)]),
        config(body, input_revision_id=other.input_revision_id),
    ):
        with pytest.raises(HTTPException) as error:
            context.create(db, body.work_id, request)
        assert error.value.status_code == 404
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ContextSnapshot)) == 0


def test_frozen_body_and_idempotence_survive_memory_and_input_changes(platform):
    _, db = platform
    body = task_input(db)
    old = approved(db, body)
    request = config(body, memories=[choose(old)])
    snapshot = context.create(db, body.work_id, request)
    frozen = detail(db, body.work_id, snapshot)
    assert context.create(db, body.work_id, request) == snapshot
    with pytest.raises(HTTPException):
        context.create(
            db, body.work_id, config(body, memories=[choose(old)], capacity_bytes=2)
        )
    replacement = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="身份",
            request_key="replace",
            base_proposal_id=old,
            content="林舟的新身份",
        ),
    )
    memory.decide(db, body.work_id, replacement, decision())
    check = context.check(db, body.work_id, snapshot)
    assert (
        not check["assembly_ready"] and check["issues"][0]["code"] == "MEMORY_CHANGED"
    )
    revise_source(db, body.input_revision_id)
    row = detail(db, body.work_id, snapshot)
    assert row["document"] == frozen["document"] and row["sha256"] == frozen["sha256"]
    assert {i["code"] for i in row["freshness"]["issues"]} == {
        "INPUT_CHANGED",
        "MEMORY_CHANGED",
    }
    assert context.create(db, body.work_id, request) == snapshot
    with db.sessions() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.action.like("context.%"))
            )
        )
        assert {e.action for e in events} == {"context.created", "context.checked"}
        assert all(
            "核对林舟" not in json.dumps(e.details, ensure_ascii=False) for e in events
        )


def test_source_staleness_and_omitted_reference_changes(platform):
    _, db = platform
    body = task_input(db)
    with db.sessions.begin() as session:
        _, source = create_asset(
            session, body.work_id, AssetInput(title="人物资料", content="船医")
        )
        source_id = source.id
    proposal = approved(db, body, source_revision_ids=[source_id])
    included = context.create(
        db, body.work_id, config(body, memories=[choose(proposal)])
    )
    measured = context.create(db, body.work_id, config(body, request_key="input-size"))
    input_bytes = detail(db, body.work_id, measured)["used_bytes"]
    skipped = context.create(
        db,
        body.work_id,
        config(
            body,
            memories=[choose(proposal, False)],
            capacity_bytes=input_bytes,
            request_key="skipped",
        ),
    )
    # Capacity makes the optional record absent, while the required input fits.
    assert not detail(db, body.work_id, skipped)["memories"][0]["included"]
    assert detail(db, body.work_id, skipped)["status"] == "READY"
    revise_source(db, source_id)
    assert (
        context.check(db, body.work_id, included)["issues"][0]["code"]
        == "SOURCE_CHANGED"
    )
    assert context.check(db, body.work_id, skipped)["fresh"]
    blocked = context.create(
        db,
        body.work_id,
        config(body, memories=[choose(proposal)], request_key="stale-required"),
    )
    assert detail(db, body.work_id, blocked)["blockers"][0]["code"] == "SOURCE_CHANGED"
    optional = context.create(
        db,
        body.work_id,
        config(body, memories=[choose(proposal, False)], request_key="stale-optional"),
    )
    assert (
        detail(db, body.work_id, optional)["memories"][0]["omission_reason"]
        == "SOURCE_CHANGED"
    )
    assert detail(db, body.work_id, optional)["status"] == "READY"
    revise_source(db, body.input_revision_id)
    old_input = context.create(db, body.work_id, config(body, request_key="old-input"))
    assert detail(db, body.work_id, old_input)["blockers"][0]["code"] == "INPUT_CHANGED"


def test_audit_failure_rolls_back_snapshot_and_references(platform, monkeypatch):
    _, db = platform
    body = task_input(db)
    proposal = approved(db, body)
    original = context.audit

    def fail(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        raise RuntimeError("Audit rollback")

    monkeypatch.setattr(context, "audit", fail)
    with pytest.raises(RuntimeError):
        context.create(db, body.work_id, config(body, memories=[choose(proposal)]))
    with db.sessions() as session:
        for model in (ContextSnapshot, ContextMemory):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "context.created")
            )
            == 0
        )


@pytest.mark.parametrize(
    "table,column",
    [("context_snapshots", "document"), ("context_memories", "omission_reason")],
)
def test_snapshot_records_are_immutable(platform, table, column):
    _, db = platform
    body = task_input(db)
    proposal = approved(db, body)
    context.create(db, body.work_id, config(body, memories=[choose(proposal)]))
    for sql in (f"DELETE FROM {table}", f"UPDATE {table} SET {column} = {column}"):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(text(sql))


def test_concurrent_duplicate_creation_has_one_snapshot_and_audit(platform):
    _, db = platform
    body = task_input(db)
    proposal = approved(db, body)
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=10)
        return context.create(
            db, body.work_id, config(body, memories=[choose(proposal)])
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result(timeout=15) for f in [pool.submit(create), pool.submit(create)]
        ]
    assert results[0] == results[1]
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ContextSnapshot)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "context.created")
            )
            == 1
        )


def test_assembly_waits_for_source_writer_then_blocks(platform, monkeypatch):
    _, db = platform
    body = task_input(db)
    proposal = approved(db, body)
    request = config(body, memories=[choose(proposal)])
    if db.engine.dialect.name == "sqlite":
        revise_source(db, body.input_revision_id)
        snapshot = context.create(db, body.work_id, request)
    else:
        writer_ready, assembly_ready = Event(), Event()
        original = memory.source_rows

        def observed(*args, **kwargs):
            if kwargs.get("lock"):
                assembly_ready.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(memory, "source_rows", observed)

        def writer():
            with db.sessions.begin() as session:
                revision = session.get(AssetRevision, body.input_revision_id)
                asset = session.get(Asset, revision.asset_id)
                append_revision(session, asset, 1, "更新", "新正文")
                session.flush()
                writer_ready.set()
                assert assembly_ready.wait(timeout=10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(writer)
            assert writer_ready.wait(timeout=10)
            creating = pool.submit(context.create, db, body.work_id, request)
            writing.result(timeout=15)
            snapshot = creating.result(timeout=15)
    row = detail(db, body.work_id, snapshot)
    assert row["status"] == "BLOCKED"
    assert {i["code"] for i in row["blockers"]} == {"INPUT_CHANGED", "SOURCE_CHANGED"}


def test_api_permissions_validation_scope_pagination_and_check(platform):
    settings, db = platform
    body = task_input(db)
    prefix = f"/api/v1/works/{body.work_id}/contexts"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(prefix).status_code == 401
        headers = signed_in(client, settings)
        payload = config(body).model_dump()
        assert client.post(prefix, json=payload).status_code == 403
        assert (
            client.post(
                prefix,
                json=payload,
                headers=headers | {"Origin": "https://other.invalid"},
            ).status_code
            == 403
        )
        for changes in (
            {"capacity_bytes": True},
            {"capacity_bytes": 0},
            {"capacity_bytes": 8388609},
            {"capacity_bytes": "1024"},
            {"story_at": -1},
            {"story_at": True},
            {"story_at": 1.5},
            {"objective": " "},
            {"title": "\x00"},
            {"memories": [choose("a"), choose("a")]},
            {"memories": [choose(str(i)) for i in range(51)]},
            {"memories": [{"proposal_id": "a", "required": "true"}]},
            {"unknown": 1},
        ):
            assert (
                client.post(prefix, json=payload | changes, headers=headers).status_code
                == 422
            )
        made = client.post(prefix, json=payload, headers=headers)
        assert made.status_code == 201
        snapshot = made.json()["id"]
        assert client.get(prefix + "?offset=1").json() == []
        assert client.get(prefix + "?limit=101").status_code == 422
        assert "document" not in client.get(prefix).json()[0]
        assert client.get(prefix + "/" + snapshot).json()["status"] == "READY"
        assert client.post(prefix + f"/{snapshot}/check", headers=headers).json()[
            "assembly_ready"
        ]
        assert client.post(prefix + f"/{snapshot}/check").status_code == 403
        other = task_input(db)
        foreign = f"/api/v1/works/{other.work_id}/contexts/{snapshot}"
        assert client.get(foreign).status_code == 404
        assert client.post(foreign + "/check", headers=headers).status_code == 404
    with TestClient(create_app(settings, db)) as agent:
        assert (
            agent.get(
                prefix, headers={"Authorization": "Bearer agent-token"}
            ).status_code
            == 401
        )


def test_upgrade_keeps_existing_memory_and_creates_no_implicit_snapshots(
    platform_database,
):
    _, db = platform_database
    with db.engine.begin() as connection:
        conf = migration_config()
        conf.attributes["connection"] = connection
        command.upgrade(conf, "0012_story_states")
    body = task_input(db)
    proposal = approved(db, body, story_state=state())
    db.upgrade()
    assert db.require_current() == "0015_summary_candidates"
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ContextSnapshot)) == 0
        assert session.get(AssetRevision, body.input_revision_id).status == "DRAFT"
    snapshot = context.create(
        db, body.work_id, config(body, memories=[choose(proposal)])
    )
    assert detail(db, body.work_id, snapshot)["status"] == "READY"
