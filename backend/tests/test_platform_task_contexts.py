from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, Event

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform import (
    context_service,
    task_service as tasks,
    task_context,
    memory_service,
)
from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.models import (
    Asset,
    AssetRevision,
    CreativeTask,
    TaskContext,
    TaskSubmission,
    BudgetReservation,
    WorkBudget,
)
from app.platform.task_protocol import LeaseInput, CheckpointInput
from app.platform.works import append_revision
from platform_fixtures import signed_in
from test_platform_contexts import config, approved, choose
from test_platform_memory import draft, decision, revise_source
from test_platform_tasks import task_input, result
from test_platform_models import model_fixture, setup, runtime, transport
from test_platform_agents import prepare, grant, auth, AgentClient, diagnostic


@pytest.fixture
def model():
    with model_fixture() as fixture:
        yield fixture


def bind(db, body, *, capacity=65536, memory=None):
    memory = memory or approved(db, body)
    snapshot = context_service.create(
        db,
        body.work_id,
        config(
            body,
            objective=body.objective,
            capacity_bytes=capacity,
            memories=[choose(memory)],
        ),
    )
    return body.model_copy(update={"context_snapshot_id": snapshot}), memory


def status(db, task_id):
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        return task.state, task.reason, task.version, task.attempt_count


def replace_memory(db, body, proposal):
    new = memory_service.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="身份",
            request_key="new-setting",
            base_proposal_id=proposal,
            content="林舟改任领航员。",
        ),
    )
    memory_service.decide(db, body.work_id, new, decision())


def acknowledged(envelope, **changes):
    return result(
        envelope,
        protocol_version="1.1",
        context_ack=task_context.acknowledgement(envelope),
        **changes,
    )


def test_task_binding_delivery_receipt_and_legacy_compatibility(platform):
    settings, db = platform
    body, _ = bind(db, task_input(db))
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        made = client.post("/api/v1/tasks", json=body.model_dump(), headers=headers)
        assert made.status_code == 201
        task_id = made.json()["id"]
        assert tasks.create_task(db, body) == task_id
        envelope = client.post(f"/api/v1/tasks/{task_id}/claim", headers=headers).json()
        assert envelope["protocol_version"] == "1.1"
        with db.sessions() as session:
            snapshot = context_service.require_snapshot(
                session, body.work_id, body.context_snapshot_id
            )
            assert envelope["context"]["document"] == snapshot.document
            assert envelope["context"]["sha256"] == snapshot.sha256
        received = tasks.submit(db, task_id, acknowledged(envelope))
        assert received["disposition"] == "ACCEPTED"
        assert tasks.submit(db, task_id, acknowledged(envelope)) == received
        assert (
            client.get(f"/api/v1/tasks/{task_id}").json()["context"]["snapshot_id"]
            == body.context_snapshot_id
        )
        old = task_input(db)
        old_id = tasks.create_task(db, old)
        legacy = client.post(f"/api/v1/tasks/{old_id}/claim", headers=headers).json()
        assert legacy["protocol_version"] == "1.0" and "context" not in legacy
        assert "checkpoint" in legacy and legacy["checkpoint"] is None
        assert tasks.submit(db, old_id, result(legacy))["disposition"] == "ACCEPTED"


def test_creation_rejects_blocked_foreign_mismatched_and_stale_snapshots(platform):
    _, db = platform
    body, memory = bind(db, task_input(db))
    for changed in (
        body.model_copy(update={"objective": "另一个目标"}),
        task_input(db).model_copy(
            update={"context_snapshot_id": body.context_snapshot_id}
        ),
    ):
        with pytest.raises(HTTPException):
            tasks.create_task(db, changed)
    blocked, _ = bind(db, task_input(db), capacity=1)
    with pytest.raises(HTTPException):
        tasks.create_task(db, blocked)
    replace_memory(db, body, memory)
    with pytest.raises(HTTPException):
        tasks.create_task(db, body)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0


@pytest.mark.parametrize("changed", ["memory", "source"])
def test_stale_before_claim_consumes_no_attempt(platform, changed):
    _, db = platform
    body, memory = bind(db, task_input(db))
    task_id = tasks.create_task(db, body)
    if changed == "memory":
        replace_memory(db, body, memory)
    else:
        revise_source(db, body.input_revision_id)
    assert tasks.try_claim(db, task_id, "human") is None
    assert status(db, task_id)[:2] == ("FAILED", "CONTEXT_STALE")
    assert status(db, task_id)[3] == 0


def test_stale_during_execution_refuses_renew_checkpoint_takeover_and_quarantines(
    platform,
):
    _, db = platform
    body, memory = bind(db, task_input(db))
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, "human")
    lease = {key: envelope[key] for key in ("attempt_id", "lease_token")}
    saved = tasks.checkpoint(
        db,
        task_id,
        CheckpointInput(**lease, sequence=1, step="note", payload={"note": "已读"}),
    )
    replace_memory(db, body, memory)
    for action in (
        lambda: tasks.heartbeat(db, task_id, LeaseInput(**lease)),
        lambda: tasks.checkpoint(
            db, task_id, CheckpointInput(**lease, sequence=2, step="note", payload={})
        ),
        lambda: tasks.takeover(db, task_id, status(db, task_id)[2]),
    ):
        with pytest.raises(HTTPException):
            action()
    # Repeating a durable checkpoint is a receipt, not permission to perform new work.
    assert (
        tasks.checkpoint(
            db,
            task_id,
            CheckpointInput(**lease, sequence=1, step="note", payload={"note": "已读"}),
        )
        == saved
    )
    received = tasks.submit(db, task_id, acknowledged(envelope))
    assert (
        received["disposition"] == "QUARANTINED"
        and received["reason"] == "CONTEXT_STALE"
    )
    assert tasks.submit(db, task_id, acknowledged(envelope)) == received


def test_paused_task_cannot_resume_with_changed_context(platform):
    _, db = platform
    body, memory = bind(db, task_input(db))
    task_id = tasks.create_task(db, body)
    tasks.control(db, task_id, "pause", 1)
    replace_memory(db, body, memory)
    with pytest.raises(HTTPException):
        tasks.control(db, task_id, "resume", status(db, task_id)[2])
    assert status(db, task_id)[0] == "PAUSED"


@pytest.mark.parametrize("wrong", ["missing", "id", "hash", "version"])
def test_missing_or_wrong_acknowledgement_is_quarantined(platform, wrong):
    _, db = platform
    body, _ = bind(db, task_input(db))
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, "human")
    ack = task_context.acknowledgement(envelope)
    version = "1.1"
    if wrong == "missing":
        ack = None
    elif wrong == "id":
        ack["snapshot_id"] = "other"
    elif wrong == "hash":
        ack["sha256"] = "0" * 64
    else:
        version = "1.0"
    received = tasks.submit(
        db, task_id, result(envelope, protocol_version=version, context_ack=ack)
    )
    assert (
        received["disposition"] == "QUARANTINED"
        and received["reason"] == "CONTEXT_MISMATCH"
    )


def test_local_worker_acknowledges_context_and_keeps_diagnostic_scope(platform):
    _, db = platform
    body, _ = bind(db, task_input(db, executor="local_test"))
    task_id = tasks.create_task(db, body)
    assert tasks.run_local(db)
    assert status(db, task_id)[0] == "SUCCEEDED"
    with db.sessions() as session:
        saved = session.scalar(
            select(TaskSubmission).where(TaskSubmission.task_id == task_id)
        )
        assert saved.envelope["context_ack"]["snapshot_id"] == body.context_snapshot_id
        assert "不是模型创作" in saved.envelope["output_artifacts"][0]["content"]


def test_agent_delivery_reconnect_stale_denial_and_owner_isolation(platform):
    settings, db = platform
    _, base = prepare(db)
    body, memory = bind(db, base.model_copy(update={"request_key": "bound-agent"}))
    task_id = tasks.create_task(db, body)
    with (
        TestClient(create_app(settings, db)) as owner,
        TestClient(create_app(settings, db)) as agent,
    ):
        headers = signed_in(owner, settings)
        issued = grant(owner, headers, task_id)
        path = f"/api/agent/v1/tasks/{task_id}"
        response = agent.post(path + "/claim", headers=auth(issued))
        assert response.status_code == 200
        assert response.json()["context"]["snapshot_id"] == body.context_snapshot_id
        assert (
            agent.get(
                f"/api/v1/works/{body.work_id}/contexts/{body.context_snapshot_id}",
                headers=auth(issued),
            ).status_code
            == 401
        )
        replace_memory(db, body, memory)
        assert agent.post(path + "/claim", headers=auth(issued)).status_code == 409
        envelope = response.json()
        payload = acknowledged(envelope).model_dump(mode="json")
        payload["lease_token"] = envelope["lease_token"]
        received = agent.post(path + "/results", headers=auth(issued), json=payload)
        assert received.json()["reason"] == "CONTEXT_STALE"


def test_reference_agent_handles_bound_snapshot_and_retries_lost_ack(
    platform, tmp_path
):
    settings, db = platform
    _, base = prepare(db)
    body, _ = bind(db, base.model_copy(update={"request_key": "reference-bound"}))
    task_id = tasks.create_task(db, body)
    with TestClient(create_app(settings, db)) as client:
        issued = grant(client, signed_in(client, settings), task_id)
        reference = AgentClient("https://testserver", task_id, issued["token"])
        lost = True

        def request(path="", body=None):
            nonlocal lost
            response = client.request(
                "POST" if body is not None else "GET",
                f"/api/agent/v1/tasks/{task_id}" + path,
                headers=auth(issued),
                json=body,
            )
            assert response.status_code == 200, response.text
            if path == "/results" and lost:
                lost = False
                raise RuntimeError("Lost ack")
            return response.json()

        reference.request = request
        path = tmp_path / "bound-agent.json"
        with pytest.raises(RuntimeError):
            diagnostic(reference, path)
        assert diagnostic(reference, path)["disposition"] == "ACCEPTED"
        assert (
            json.loads(path.read_text(encoding="utf-8"))["result"]["context_ack"][
                "snapshot_id"
            ]
            == body.context_snapshot_id
        )


def test_loopback_model_includes_frozen_context_and_settles_stale_result(
    platform, model
):
    settings, db = platform
    body, memory = bind(db, setup(platform, model))
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation, chosen, payload, secret = runtime.prepare(db, settings, envelope)
    prompt = json.loads(payload["messages"][1]["content"])
    assert prompt["context"] == envelope["context"]
    raw = transport.send(model.endpoint, payload, secret, 1, test_mode=True)
    replace_memory(db, body, memory)
    received = runtime.finish(db, envelope, reservation, chosen, raw)
    assert (
        received["reason"] == "CONTEXT_STALE"
        and received["disposition"] == "QUARANTINED"
    )
    assert runtime.finish(db, envelope, reservation, chosen, raw) == received
    with db.sessions() as session:
        assert session.get(BudgetReservation, reservation).state == "SETTLED"
        assert session.get(WorkBudget, body.work_id).spent_tokens == 120
    assert len(model.calls) == 1


def test_model_preflight_blocks_changed_context_without_reservation(platform, model):
    settings, db = platform
    body, memory = bind(db, setup(platform, model))
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    replace_memory(db, body, memory)
    with pytest.raises(HTTPException):
        runtime.prepare(db, settings, envelope)
    assert model.calls == []
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0


def test_model_worker_completes_bound_task_with_acknowledgement(platform, model):
    settings, db = platform
    body, _ = bind(db, setup(platform, model))
    task_id = tasks.create_task(db, body)
    assert runtime.execute(db, settings, task_id, body.executor_id)
    assert status(db, task_id)[0] == "SUCCEEDED"
    with db.sessions() as session:
        saved = session.scalar(
            select(TaskSubmission).where(TaskSubmission.task_id == task_id)
        )
        assert saved.envelope["context_ack"]["snapshot_id"] == body.context_snapshot_id
    assert len(model.calls) == 1


def test_creation_waits_for_source_edit_then_rechecks(platform, monkeypatch):
    _, db = platform
    body, _ = bind(db, task_input(db))
    if db.engine.dialect.name == "sqlite":
        revise_source(db, body.input_revision_id)
        with pytest.raises(HTTPException):
            tasks.create_task(db, body)
    else:
        writer_ready, checking = Event(), Event()
        original = context_service.freshness

        def observed(*args, **kwargs):
            if kwargs.get("lock"):
                checking.set()
            return original(*args, **kwargs)

        monkeypatch.setattr(context_service, "freshness", observed)

        def writer():
            with db.sessions.begin() as session:
                revision = session.get(AssetRevision, body.input_revision_id)
                asset = session.get(Asset, revision.asset_id)
                append_revision(session, asset, 1, "新版本", "正文更新")
                session.flush()
                writer_ready.set()
                assert checking.wait(timeout=10)

        with ThreadPoolExecutor(max_workers=2) as pool:
            writing = pool.submit(writer)
            assert writer_ready.wait(timeout=10)
            creating = pool.submit(tasks.create_task, db, body)
            writing.result(timeout=15)
            with pytest.raises(HTTPException):
                creating.result(timeout=15)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskContext)) == 0
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0


def test_routing_counts_full_snapshot_in_model_capacity(platform, model):
    _, db = platform
    base = setup(platform, model)
    proposal = approved(db, base, content="重要设定" * 1500)
    body, _ = bind(db, base, memory=proposal)
    with pytest.raises(HTTPException):
        tasks.create_task(db, body)
    assert model.calls == []


def test_immutable_binding_and_creation_rollback(platform, monkeypatch):
    _, db = platform
    body, _ = bind(db, task_input(db))
    original = tasks.event

    def fail(session, *args, **kwargs):
        original(session, *args, **kwargs)
        session.flush()
        raise RuntimeError("Audit failed")

    monkeypatch.setattr(tasks, "event", fail)
    with pytest.raises(RuntimeError):
        tasks.create_task(db, body)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskContext)) == 0
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0
    monkeypatch.setattr(tasks, "event", original)
    tasks.create_task(db, body)
    for sql in (
        "DELETE FROM task_contexts",
        "UPDATE task_contexts SET snapshot_id = snapshot_id",
    ):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(text(sql))


def test_concurrent_claims_keep_one_bound_attempt(platform):
    _, db = platform
    body, _ = bind(db, task_input(db))
    task_id = tasks.create_task(db, body)
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        return tasks.try_claim(db, task_id, "human")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            f.result(timeout=15) for f in (pool.submit(claim), pool.submit(claim))
        ]
    assert len([r for r in results if r]) == 1
    assert status(db, task_id)[3] == 1


def test_upgrade_preserves_old_task_hash_and_no_implicit_binding(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        conf = migration_config()
        conf.attributes["connection"] = connection
        command.upgrade(conf, "0013_context_snapshots")
    body = task_input(db)
    task_id = tasks.create_task(db, body)
    db.upgrade()
    assert db.require_current() == "0014_task_contexts"
    assert tasks.create_task(db, body) == task_id
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(TaskContext)) == 0
    envelope = tasks.try_claim(db, task_id, "human")
    assert envelope["protocol_version"] == "1.0"
    assert tasks.submit(db, task_id, result(envelope))["disposition"] == "ACCEPTED"
