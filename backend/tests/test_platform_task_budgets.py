from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func, insert

from app.platform import (
    task_service as tasks,
    budget_service as budgets,
    model_runtime as runtime,
    executor_service,
)
from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.budget_protocol import ReservationInput, SettlementInput
from app.platform.task_protocol import TaskSpendingLimit, TaskSpendingUpdate
from app.platform.models import (
    CreativeTask,
    WorkBudget,
    TaskRoutingDecision,
    identifier,
    now,
)
from app.platform.security import create_owner
from platform_fixtures import signed_in, PASSWORD
from test_platform_models import setup, model_fixture


@pytest.fixture
def model():
    with model_fixture() as fixture:
        yield fixture


def summary(db, task_id):
    with db.sessions() as session:
        return budgets.task_summary(session, session.get(CreativeTask, task_id))


def version(db, task_id):
    with db.sessions() as session:
        return session.get(CreativeTask, task_id).version


def configure(db, task_id, money, tokens, **extra):
    return budgets.configure_task(
        db,
        task_id,
        TaskSpendingUpdate(
            expected_version=version(db, task_id),
            currency="CNY",
            limit_microunits=money,
            limit_tokens=tokens,
            **extra,
        ),
    )


def create(platform, model, *, custom=None):
    settings, db = platform
    body = setup(platform, model)
    if custom is not None:
        body = body.model_copy(update={"spending_limit": TaskSpendingLimit(**custom)})
    return body, tasks.create_task(db, body)


def test_default_limit_and_creation_idempotence(platform, model):
    _, db = platform
    body, task_id = create(platform, model)
    value = summary(db, task_id)
    assert (value["limit_microunits"], value["limit_tokens"]) == (4224 * 3, 4096 * 3)
    assert tasks.create_task(db, body) == task_id
    configure(db, task_id, 10000, 9000)
    assert tasks.create_task(db, body) == task_id
    assert summary(db, task_id)["limit_tokens"] == 9000
    with pytest.raises(HTTPException):
        tasks.create_task(
            db,
            body.model_copy(
                update={
                    "spending_limit": TaskSpendingLimit(
                        currency="CNY", limit_microunits=10000, limit_tokens=9000
                    )
                }
            ),
        )


@pytest.mark.parametrize(
    "limits",
    [
        {"currency": "USD", "limit_microunits": 10000, "limit_tokens": 10000},
        {"currency": "CNY", "limit_microunits": 4223, "limit_tokens": 10000},
        {"currency": "CNY", "limit_microunits": 10000, "limit_tokens": 4095},
    ],
)
def test_custom_limit_must_allow_one_call(platform, model, limits):
    _, db = platform
    with pytest.raises(HTTPException):
        create(platform, model, custom=limits)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0
    assert model.calls == []


def test_retries_accumulate_and_raise_limit_does_not_resume(platform, model):
    settings, db = platform
    body, task_id = create(
        platform,
        model,
        custom={"currency": "CNY", "limit_microunits": 4224, "limit_tokens": 4096},
    )
    model.response["choices"][0]["finish_reason"] = "length"
    runtime.run_model(db, settings)
    first = summary(db, task_id)
    assert first["spent_microunits"] == 140 and first["spent_tokens"] == 120
    with pytest.raises(budgets.TaskBudgetExceeded):
        tasks.control(db, task_id, "resume", version(db, task_id))
    configure(db, task_id, 8448, 8192)
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "PAUSED"
    tasks.control(db, task_id, "resume", version(db, task_id))
    model.response["id"] = "second-fixture-receipt"
    model.response["choices"][0]["finish_reason"] = "stop"
    runtime.run_model(db, settings)
    final = summary(db, task_id)
    assert (final["spent_microunits"], final["spent_tokens"], final["held_tokens"]) == (
        280,
        240,
        0,
    )
    assert len(model.calls) == 2
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert task.state == "SUCCEEDED" and task.attempt_count == 2


def test_unknown_hold_survives_pause_and_lowering_limit(platform, model):
    settings, db = platform
    body, task_id = create(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation_id = runtime.prepare(db, settings, envelope)[0]
    tasks.control(db, task_id, "pause", version(db, task_id))
    configure(db, task_id, 0, 0)
    value = summary(db, task_id)
    assert value["unknown_count"] == 1 and value["held_tokens"] == 4096
    assert value["remaining_tokens"] == 0 and value["over_limit"]
    with pytest.raises(HTTPException):
        tasks.control(db, task_id, "resume", version(db, task_id))
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        budgets.settle(
            session,
            task,
            reservation_id,
            SettlementInput(
                receipt_key="confirmed-unused",
                input_tokens=0,
                output_tokens=0,
                actual_microunits=0,
                evidence="Test confirms no HTTP request was sent",
            ),
        )
    assert summary(db, task_id)["held_tokens"] == 0
    assert model.calls == []


def test_concurrent_operations_cannot_overspend_task_limit(platform, model):
    _, db = platform
    body, task_id = create(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    configure(db, task_id, 100, 100)
    barrier = Barrier(2)

    def reserve(key):
        barrier.wait(timeout=5)
        try:
            with db.sessions.begin() as session:
                task = tasks.locked_task(session, task_id)
                return budgets.reserve(
                    session,
                    task,
                    envelope["attempt_id"],
                    ReservationInput(
                        operation_key=key,
                        currency="CNY",
                        reserved_tokens=80,
                        reserved_microunits=80,
                    ),
                ).id
        except budgets.TaskBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ["one", "two"]))
    assert sum(value is not None for value in results) == 1
    assert summary(db, task_id)["held_tokens"] == 80
    with db.sessions() as session:
        assert session.get(WorkBudget, body.work_id).held_tokens == 80


def test_reducing_limit_after_reservation_prevents_dispatch(platform, model):
    _, db = platform
    body, task_id = create(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        held = budgets.reserve(
            session,
            task,
            envelope["attempt_id"],
            ReservationInput(
                operation_key="unsent",
                currency="CNY",
                reserved_tokens=100,
                reserved_microunits=100,
            ),
        ).id
    configure(db, task_id, 0, 0)
    with pytest.raises(budgets.TaskBudgetExceeded):
        with db.sessions.begin() as session:
            budgets.dispatch(session, tasks.locked_task(session, task_id), held)
    assert summary(db, task_id)["held_tokens"] == 100
    tasks.control(db, task_id, "pause", version(db, task_id))
    assert summary(db, task_id)["held_tokens"] == 0 and model.calls == []


def test_actual_usage_over_task_limit_is_recorded(platform, model):
    settings, db = platform
    _, task_id = create(
        platform,
        model,
        custom={"currency": "CNY", "limit_microunits": 4224, "limit_tokens": 4096},
    )
    model.response["usage"] = {
        "prompt_tokens": 5000,
        "completion_tokens": 20,
        "total_tokens": 5020,
    }
    runtime.run_model(db, settings)
    value = summary(db, task_id)
    assert value["spent_tokens"] == 5020 and value["spent_microunits"] == 5040
    assert value["over_limit"] and value["remaining_tokens"] == 0
    with pytest.raises(budgets.TaskBudgetExceeded):
        tasks.control(db, task_id, "resume", version(db, task_id))


def test_task_limit_does_not_consume_other_tasks_allowance(platform, model):
    settings, db = platform
    body, first = create(platform, model)
    configure(db, first, 0, 0)
    second = tasks.create_task(db, body.model_copy(update={"request_key": "another"}))
    runtime.run_model(db, settings)
    with db.sessions() as session:
        task = session.get(CreativeTask, first)
        assert (
            task.state == "PAUSED"
            and task.reason == "TASK_BUDGET_EXCEEDED"
            and task.attempt_count == 0
        )
        assert session.get(CreativeTask, second).state == "SUCCEEDED"
    assert len(model.calls) == 1


def test_budget_update_auth_conflict_currency_and_audit(platform, model):
    settings, db = platform
    _, task_id = create(platform, model)
    path = f"/api/v1/tasks/{task_id}/budget"
    with TestClient(create_app(settings, db)) as client:
        body = {
            "expected_version": 1,
            "currency": "CNY",
            "limit_microunits": 20000,
            "limit_tokens": 20000,
        }
        assert client.put(path, json=body).status_code == 401
        headers = signed_in(client, settings)
        assert client.put(path, json=body).status_code == 403
        assert (
            client.put(
                path, headers=headers, json={**body, "limit_tokens": True}
            ).status_code
            == 422
        )
        assert (
            client.put(
                path, headers=headers, json={**body, "currency": "USD"}
            ).status_code
            == 409
        )
        assert client.put(path, headers=headers, json=body).status_code == 200
        assert (
            client.put(
                path, headers=headers, json={**body, "limit_tokens": 20001}
            ).status_code
            == 409
        )
        assert summary(db, task_id)["limit_tokens"] == 20000
        tasks.control(db, task_id, "cancel", version(db, task_id))
        assert (
            client.put(
                path,
                headers=headers,
                json={**body, "expected_version": version(db, task_id)},
            ).status_code
            == 409
        )


def test_upgrade_keeps_legacy_model_blocked_until_budget_is_set(
    platform_database, model
):
    settings, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0008_model_api")
    with db.sessions.begin() as session:
        create_owner(session, "owner", PASSWORD)
    body = setup((settings, db), model)
    task_id = identifier()
    with db.sessions.begin() as session:
        decision = executor_service.evaluate(
            session, body.work_id, body.task_type, automatic=False, check_quotas=False
        )
        session.execute(
            insert(CreativeTask.__table__).values(
                id=task_id,
                work_id=body.work_id,
                executor_id=body.executor_id,
                input_revision_id=body.input_revision_id,
                task_type=body.task_type,
                objective=body.objective,
                request_key=body.request_key,
                request_hash=tasks.fingerprint(
                    body.model_dump(exclude={"spending_limit"})
                ),
                deadline_at=now() + timedelta(days=1),
            )
        )
        session.add(
            TaskRoutingDecision(
                task_id=task_id,
                requested_executor_id=body.executor_id,
                selected_executor_id=body.executor_id,
                policy_version=decision["policy_version"],
                snapshot=decision,
            )
        )
    db.upgrade()
    assert db.require_current() == "0011_memory_review"
    assert tasks.create_task(db, body) == task_id
    assert summary(db, task_id)["configured"] is False
    runtime.run_model(db, settings)
    assert model.calls == []
    configure(db, task_id, 12672, 12288)
    tasks.control(db, task_id, "resume", version(db, task_id))
    runtime.run_model(db, settings)
    assert len(model.calls) == 1 and summary(db, task_id)["spent_tokens"] == 120
