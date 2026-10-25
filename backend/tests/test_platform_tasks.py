from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, update

from app.platform.api import create_app
from app.platform.models import (
    Asset,
    AssetRevision,
    CreativeTask,
    TaskAttempt,
    TaskCheckpoint,
    TaskSubmission,
    Work,
    now,
)
from app.platform.task_protocol import (
    CheckpointInput,
    LeaseInput,
    ResultEnvelope,
    TaskInput,
)
from app.platform import task_service as service
from app.platform.works import AssetInput, append_revision, create_asset
from platform_fixtures import signed_in


def task_input(db, executor="human", **changes):
    with db.sessions.begin() as session:
        work = Work(title="任务测试作品")
        session.add(work)
        session.flush()
        _, revision = create_asset(
            session, work.id, AssetInput(title="第一章", content="山风\n雨声\n")
        )
        return TaskInput(
            work_id=work.id,
            executor_id=executor,
            input_revision_id=revision.id,
            task_type="analyze",
            objective="核对章节",
            request_key="creation-1",
            **changes,
        )


def result(envelope, **changes):
    payload = {
        "task_id": envelope["task_id"],
        "executor_id": envelope["executor_id"],
        "attempt_id": envelope["attempt_id"],
        "lease_token": envelope["lease_token"],
        "submission_id": "result-1",
        "result_status": "success",
        "finish_reason": "complete",
        "output_artifacts": [
            {"kind": "analyze", "title": "候选分析", "content": "人工分析结果"}
        ],
        "evidence_revision_ids": [envelope["input"]["revision_id"]],
    }
    return ResultEnvelope(**{**payload, **changes})


def expire(db, task_id):
    with db.sessions.begin() as session:
        session.execute(
            update(TaskAttempt)
            .where(TaskAttempt.task_id == task_id)
            .values(lease_until=now() - timedelta(seconds=1))
        )


def test_task_contract_access_idempotence_and_no_external_execution(platform):
    settings, db = platform
    body = task_input(db)
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/v1/executors").status_code == 401
        assert client.post("/api/v1/tasks", json=body.model_dump()).status_code == 401
        headers = signed_in(client, settings)
        assert client.post("/api/v1/tasks", json=body.model_dump()).status_code == 403
        created = client.post("/api/v1/tasks", headers=headers, json=body.model_dump())
        assert created.status_code == 201, created.text
        assert (
            client.post("/api/v1/tasks", headers=headers, json=body.model_dump()).json()
            == created.json()
        )
        assert (
            client.post(
                "/api/v1/tasks",
                headers=headers,
                json={**body.model_dump(), "objective": "different"},
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/v1/tasks",
                headers=headers,
                json={
                    **body.model_dump(),
                    "executor_id": "real_api",
                    "request_key": "unregistered-api",
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/tasks",
                headers=headers,
                json={**body.model_dump(), "protocol_version": "2.0"},
            ).status_code
            == 422
        )
        envelope = client.post(
            f"/api/v1/tasks/{created.json()['id']}/claim", headers=headers
        ).json()
        assert envelope["permissions"] == {
            "candidate_submission": True,
            "write_assets": False,
            "finalize": False,
            "paid_calls": False,
            "external_calls": False,
        }
        details = client.get(f"/api/v1/tasks/{created.json()['id']}").text
        assert envelope["lease_token"] not in details and "token_hash" not in details
        invalid = result(envelope).model_dump(mode="json")
        invalid["lease_token"] = envelope["lease_token"]
        invalid["usage"] = {"cost_microunits": 1}
        assert (
            client.post(
                f"/api/v1/tasks/{envelope['task_id']}/results",
                headers=headers,
                json=invalid,
            ).status_code
            == 422
        )


def test_concurrent_claim_and_result_dedup_never_changes_asset(platform):
    _, db = platform
    body = task_input(db)
    task_id = service.create_task(db, body)
    barrier = Barrier(2)

    def claim(_):
        barrier.wait(timeout=10)
        return service.try_claim(db, task_id, "human")

    with ThreadPoolExecutor(max_workers=2) as pool:
        envelopes = list(pool.map(claim, range(2)))
    assert sum(e is not None for e in envelopes) == 1
    envelope = next(e for e in envelopes if e)
    submission = result(envelope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(lambda _: service.submit(db, task_id, submission), range(2))
        )
    assert receipts[0] == receipts[1] and receipts[0]["disposition"] == "ACCEPTED"
    with pytest.raises(HTTPException) as conflict:
        service.submit(db, task_id, result(envelope, warnings=["changed payload"]))
    assert conflict.value.status_code == 409
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "SUCCEEDED"
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 1
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1
        assert (
            session.get(AssetRevision, body.input_revision_id).content == "山风\n雨声\n"
        )


def test_checkpoint_persists_and_expired_attempt_cannot_submit(platform):
    settings, db = platform
    task_id = service.create_task(db, task_input(db))
    envelope = service.try_claim(db, task_id, "human")
    body = CheckpointInput(
        attempt_id=envelope["attempt_id"],
        lease_token=envelope["lease_token"],
        sequence=1,
        step="manual_progress",
        payload={"note": "完成一半"},
    )
    saved = service.checkpoint(db, task_id, body)
    assert service.checkpoint(db, task_id, body) == saved
    with pytest.raises(HTTPException):
        service.checkpoint(
            db, task_id, body.model_copy(update={"payload": {"note": "overwrite"}})
        )
    expire(db, task_id)
    with pytest.raises(HTTPException):
        service.heartbeat(
            db,
            task_id,
            LeaseInput(
                attempt_id=envelope["attempt_id"], lease_token=envelope["lease_token"]
            ),
        )
    service.recover(db)
    # A new app instance reads the checkpoint from the same database.
    with TestClient(create_app(settings, db)) as client:
        signed_in(client, settings)
        detail = client.get(f"/api/v1/tasks/{task_id}").json()
        assert detail["checkpoint"]["payload"] == {"note": "完成一半"}
        assert detail["state"] == "PAUSED"
    service.control(db, task_id, "resume", detail["version"])
    resumed = service.try_claim(db, task_id, "human")
    assert resumed["attempt_id"] != envelope["attempt_id"]
    assert resumed["checkpoint"]["id"] == saved["id"]
    assert service.submit(db, task_id, result(envelope))["disposition"] == "QUARANTINED"
    assert service.submit(db, task_id, result(resumed))["disposition"] == "ACCEPTED"


def test_cancel_pause_takeover_and_fenced_old_page(platform):
    _, db = platform
    task_id = service.create_task(db, task_input(db))
    envelope = service.try_claim(db, task_id, "human")
    with db.sessions() as session:
        version = session.get(CreativeTask, task_id).version
    takeover = service.takeover(db, task_id, version)
    assert (
        takeover["attempt_id"] == envelope["attempt_id"]
        and takeover["lease_token"] != envelope["lease_token"]
    )
    with pytest.raises(HTTPException) as revoked:
        service.submit(db, task_id, result(envelope))
    assert revoked.value.status_code == 403
    with db.sessions() as session:
        version = session.get(CreativeTask, task_id).version
    assert service.control(db, task_id, "pause", version)["state"] == "PAUSED"
    assert service.submit(db, task_id, result(takeover))["disposition"] == "QUARANTINED"
    with db.sessions() as session:
        version = session.get(CreativeTask, task_id).version
    service.control(db, task_id, "cancel", version)
    assert service.try_claim(db, task_id, "human") is None
    with pytest.raises(HTTPException):
        service.control(db, task_id, "resume", version + 1)


def test_incomplete_and_stale_outputs_are_not_success(platform):
    _, db = platform
    task_id = service.create_task(db, task_input(db))
    envelope = service.try_claim(db, task_id, "human")
    assert (
        service.submit(db, task_id, result(envelope, finish_reason="truncated"))[
            "disposition"
        ]
        == "PARTIAL"
    )
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert task.state == "PAUSED"
        version = task.version
    service.control(db, task_id, "resume", version)
    second = service.try_claim(db, task_id, "human")
    with db.sessions.begin() as session:
        revision = session.get(AssetRevision, second["input"]["revision_id"])
        append_revision(
            session, session.get(Asset, revision.asset_id), 1, "改稿", "更新后的正文"
        )
    receipt = service.submit(db, task_id, result(second))
    assert (
        receipt["disposition"] == "QUARANTINED" and receipt["reason"] == "INPUT_STALE"
    )
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "FAILED"

    paused_body = task_input(db)
    paused = service.create_task(db, paused_body)
    service.control(db, paused, "pause", 1)
    with db.sessions.begin() as session:
        revision = session.get(AssetRevision, paused_body.input_revision_id)
        append_revision(
            session, session.get(Asset, revision.asset_id), 1, "新正文", "暂停后改稿"
        )
    with pytest.raises(HTTPException) as stale_resume:
        service.control(db, paused, "resume", 2)
    assert stale_resume.value.status_code == 409
    with db.sessions() as session:
        assert session.get(CreativeTask, paused).state == "PAUSED"


def test_dependencies_scope_and_local_executor_checkpoint_recovery(platform):
    _, db = platform
    body = task_input(db, executor="local_test")
    first = service.create_task(db, body)
    dependent = service.create_task(
        db,
        body.model_copy(update={"request_key": "dependent", "dependencies": [first]}),
    )
    assert service.try_claim(db, dependent, "local_test") is None
    other = task_input(db)
    with pytest.raises(HTTPException):
        service.create_task(db, other.model_copy(update={"dependencies": [first]}))
    with pytest.raises(HTTPException):
        service.create_task(
            db, other.model_copy(update={"input_revision_id": body.input_revision_id})
        )
    envelope = service.try_claim(db, first, "local_test")
    stats = {
        "sha256": envelope["input"]["sha256"],
        "characters": len(envelope["input"]["content"]),
        "paragraphs": 2,
    }
    service.checkpoint(
        db,
        first,
        CheckpointInput(
            attempt_id=envelope["attempt_id"],
            lease_token=envelope["lease_token"],
            sequence=1,
            step="local_analysis",
            payload=stats,
        ),
    )
    expire(db, first)
    service.recover(db)
    with db.sessions.begin() as session:
        session.execute(
            update(CreativeTask)
            .where(CreativeTask.id == first)
            .values(available_at=now() - timedelta(seconds=1))
        )
    assert service.run_local(db)
    with db.sessions() as session:
        assert session.get(CreativeTask, first).state == "SUCCEEDED"
        saved = session.scalar(
            select(TaskSubmission).where(TaskSubmission.task_id == first)
        )
        assert saved.envelope["warnings"] == ["恢复并核对了已有检查点"]
        assert session.scalar(select(func.count()).select_from(TaskCheckpoint)) == 2
    assert service.run_local(db)
    with db.sessions() as session:
        assert session.get(CreativeTask, dependent).state == "SUCCEEDED"


def test_attempt_deadline_limits_and_cross_task_token(platform):
    _, db = platform
    task_id = service.create_task(db, task_input(db, max_attempts=1))
    envelope = service.try_claim(db, task_id, "human")
    other = service.create_task(db, task_input(db))
    with pytest.raises(HTTPException):
        service.submit(db, other, result(envelope))
    expire(db, task_id)
    service.recover(db)
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert task.state == "FAILED" and task.reason == "ATTEMPTS_EXHAUSTED"
    with db.sessions.begin() as session:
        session.execute(
            update(CreativeTask)
            .where(CreativeTask.id == other)
            .values(deadline_at=now() - timedelta(seconds=1))
        )
    assert service.try_claim(db, other, "human") is None
    with db.sessions() as session:
        assert session.get(CreativeTask, other).state == "FAILED"


def test_result_transaction_rollback_and_scope_rejection(platform, monkeypatch):
    _, db = platform
    task_id = service.create_task(db, task_input(db))
    envelope = service.try_claim(db, task_id, "human")
    original_event = service.event

    def fail(*_args, **_kwargs):
        raise RuntimeError("crash before commit")

    monkeypatch.setattr(service, "event", fail)
    with pytest.raises(RuntimeError):
        service.submit(db, task_id, result(envelope))
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "RUNNING"
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 0
    monkeypatch.setattr(service, "event", original_event)
    receipt = service.submit(
        db, task_id, result(envelope, evidence_revision_ids=["not-authorized"])
    )
    assert (
        receipt["disposition"] == "QUARANTINED"
        and receipt["reason"] == "OUTPUT_SCOPE_INVALID"
    )


def test_blocked_high_priority_queue_cannot_starve_its_dependency(platform):
    _, db = platform
    body = task_input(db, executor="local_test", priority=-10)
    parent = service.create_task(db, body)
    for index in range(51):
        service.create_task(
            db,
            body.model_copy(
                update={
                    "request_key": f"child-{index}",
                    "priority": 10,
                    "dependencies": [parent],
                }
            ),
        )
    assert service.run_local(db)
    with db.sessions() as session:
        assert session.get(CreativeTask, parent).state == "SUCCEEDED"
    manual = service.create_task(
        db, body.model_copy(update={"request_key": "manual", "executor_id": "human"})
    )
    child = service.create_task(
        db, body.model_copy(update={"request_key": "blocked", "dependencies": [manual]})
    )
    service.control(db, manual, "cancel", 1)
    service.recover(db)
    with db.sessions() as session:
        assert session.get(CreativeTask, child).state == "PAUSED"
        assert session.get(CreativeTask, child).reason == "DEPENDENCY_FAILED"
        child_version = session.get(CreativeTask, child).version
    with pytest.raises(HTTPException) as blocked_resume:
        service.control(db, child, "resume", child_version)
    assert blocked_resume.value.status_code == 409


def test_cancel_result_race_has_one_authoritative_outcome(platform):
    _, db = platform
    task_id = service.create_task(db, task_input(db))
    envelope = service.try_claim(db, task_id, "human")
    barrier = Barrier(2)

    def cancellation():
        barrier.wait(timeout=10)
        try:
            return service.control(db, task_id, "cancel", 2)
        except HTTPException as exc:
            assert exc.status_code == 409
            return None

    def submission():
        barrier.wait(timeout=10)
        return service.submit(db, task_id, result(envelope))

    with ThreadPoolExecutor(max_workers=2) as pool:
        cancelled, received = pool.submit(cancellation), pool.submit(submission)
        cancelled, received = cancelled.result(), received.result()
    with db.sessions() as session:
        state = session.get(CreativeTask, task_id).state
        if cancelled:
            assert state == "CANCELLED" and received["disposition"] == "QUARANTINED"
        else:
            assert state == "SUCCEEDED" and received["disposition"] == "ACCEPTED"


def test_local_failures_and_result_receipts_are_bounded(platform):
    _, db = platform
    task_id = service.create_task(
        db, task_input(db, executor="local_test", max_attempts=2)
    )
    for number in (1, 2):
        with db.sessions.begin() as session:
            session.execute(
                update(CreativeTask)
                .where(CreativeTask.id == task_id)
                .values(available_at=now() - timedelta(seconds=1))
            )
        envelope = service.try_claim(db, task_id, "local_test")
        service.submit(
            db,
            task_id,
            result(
                envelope,
                result_status="failed",
                finish_reason="error",
                error_code="LOCAL_EXECUTION_FAILED",
                output_artifacts=[],
            ),
        )
        with db.sessions() as session:
            assert session.get(CreativeTask, task_id).state == (
                "RETRY_WAIT" if number == 1 else "FAILED"
            )
    assert service.try_claim(db, task_id, "local_test") is None
    for index in range(15):
        assert (
            service.submit(
                db, task_id, result(envelope, submission_id=f"late-{index}")
            )["disposition"]
            == "QUARANTINED"
        )
    with pytest.raises(HTTPException) as capped:
        service.submit(db, task_id, result(envelope, submission_id="over-limit"))
    assert capped.value.status_code == 429
