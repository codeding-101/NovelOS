from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select, update, func
from sqlalchemy.exc import DBAPIError

from app.platform.api import create_app
from app.platform import (
    executor_service as routing,
    task_service as tasks,
    budget_service as budgets,
)
from app.platform.executor_protocol import ExecutorCreate, ExecutorUpdate, PolicyInput
from app.platform.budget_protocol import BudgetInput, ReservationInput
from app.platform.models import (
    Executor,
    CreativeTask,
    TaskRoutingDecision,
    BudgetReservation,
    ProviderCredential,
)
from app.platform.security import CredentialVault
from platform_fixtures import signed_in
from test_platform_tasks import task_input


def declaration(**changes):
    return ExecutorCreate(
        **{
            "name": "离线登记",
            "mode": "api",
            "capabilities": ["analyze", "draft"],
            "request_key": "register-1",
            **changes,
        }
    )


def edit(db, executor_id, **changes):
    with db.sessions() as session:
        item = session.get(Executor, executor_id)
        body = ExecutorUpdate(
            expected_version=item.version,
            **{**{key: getattr(item, key) for key in routing.FIELDS}, **changes},
        )
    return routing.configure(db, executor_id, body)


def policy(db, work_id, allowed=None, preferred=None, diagnostics=False):
    with db.sessions() as session:
        current = routing.policy(session, work_id)
    return routing.configure_policy(
        db,
        work_id,
        PolicyInput(
            expected_version=current["version"],
            allowed_ids=allowed if allowed is not None else ["human", "local_test"],
            preferred_ids=preferred or [],
            auto_local_diagnostics=diagnostics,
        ),
    )


def preview(db, work_id, **kwargs):
    with db.sessions() as session:
        return routing.evaluate(
            session, work_id, kwargs.pop("task_type", "analyze"), **kwargs
        )


def test_executor_registration_access_idempotence_and_no_secret_leak(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        payload = declaration().model_dump()
        assert client.post("/api/v1/executors", json=payload).status_code == 401
        headers = signed_in(client, settings)
        assert client.post("/api/v1/executors", json=payload).status_code == 403
        response = client.post("/api/v1/executors", json=payload, headers=headers)
        assert response.status_code == 201
        assert (
            client.post("/api/v1/executors", json=payload, headers=headers).json()
            == response.json()
        )
        assert (
            client.post(
                "/api/v1/executors",
                json={**payload, "name": "changed"},
                headers=headers,
            ).status_code
            == 409
        )
        for changes in (
            {"mode": "local_test"},
            {"external_calls": True},
            {"input_rate": 1},
            {"capabilities": ["analyze", "analyze"]},
            {"priority": 101},
        ):
            assert (
                client.post(
                    "/api/v1/executors", json={**payload, **changes}, headers=headers
                ).status_code
                == 422
            )
        listing = client.get("/api/v1/executors").json()
        external = next(row for row in listing if row["id"] == response.json()["id"])
        assert external["enabled"] is False and external["adapter_ready"] is False
        assert external["external_calls"] is False
        assert (
            "registration_hash" not in external and "registration_key" not in external
        )
        assert (
            client.put(
                "/api/v1/executors/local_test",
                headers=headers,
                json={
                    **{
                        key: next(row for row in listing if row["id"] == "local_test")[
                            key
                        ]
                        for key in routing.FIELDS
                    },
                    "expected_version": 1,
                    "capabilities": ["draft"],
                },
            ).status_code
            == 422
        )


def test_auto_route_defaults_to_human_and_opt_in_local_is_durable(platform):
    _, db = platform
    body = task_input(db).model_copy(update={"executor_id": "auto"})
    assert preview(db, body.work_id)["selected_id"] == "human"
    policy(db, body.work_id, preferred=["local_test", "human"])
    assert preview(db, body.work_id)["selected_id"] == "human"
    policy(db, body.work_id, preferred=["local_test", "human"], diagnostics=True)
    task_id = tasks.create_task(db, body)
    assert tasks.create_task(db, body) == task_id
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).executor_id == "local_test"
        decision = session.get(TaskRoutingDecision, task_id)
        assert decision.requested_executor_id == "auto" and decision.policy_version == 2
        assert decision.snapshot["selected_id"] == "local_test"
    policy(db, body.work_id, preferred=["local_test", "human"], diagnostics=False)
    assert tasks.try_claim(db, task_id, "local_test") is None
    with db.sessions() as session:
        assert session.get(TaskRoutingDecision, task_id).policy_version == 2
        paused_version = session.get(CreativeTask, task_id).version
    policy(db, body.work_id, preferred=["local_test", "human"], diagnostics=True)
    tasks.control(db, task_id, "resume", paused_version)
    assert tasks.run_local(db)
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "SUCCEEDED"
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(
                update(TaskRoutingDecision)
                .where(TaskRoutingDecision.task_id == task_id)
                .values(policy_version=9)
            )


def test_explicit_tasks_obey_policy_and_queued_work_rechecks_configuration(platform):
    _, db = platform
    body = task_input(db)
    task_id = tasks.create_task(db, body)
    edit(db, "human", enabled=False)
    assert tasks.try_claim(db, task_id, "human") is None
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert (task.state, task.reason, task.attempt_count) == (
            "PAUSED",
            "EXECUTOR_BLOCKED",
            0,
        )
        version = task.version
    with pytest.raises(HTTPException):
        tasks.control(db, task_id, "resume", version)
    edit(db, "human", enabled=True)
    policy(db, body.work_id, allowed=[])
    with pytest.raises(HTTPException):
        tasks.create_task(db, body.model_copy(update={"request_key": "new"}))
    with pytest.raises(HTTPException):
        tasks.control(db, task_id, "resume", version)
    policy(db, body.work_id, allowed=["human"])
    tasks.control(db, task_id, "resume", version)
    assert tasks.try_claim(db, task_id, "human")


def test_priority_availability_and_capability_do_not_turn_diagnostics_into_writing(
    platform,
):
    _, db = platform
    body = task_input(db)
    policy(db, body.work_id, diagnostics=True)
    edit(db, "local_test", priority=50)
    assert preview(db, body.work_id)["selected_id"] == "local_test"
    assert preview(db, body.work_id, task_type="draft")["selected_id"] == "human"
    edit(db, "local_test", available=False)
    assert preview(db, body.work_id)["selected_id"] == "human"
    edit(db, "human", available=False)
    assert preview(db, body.work_id)["selected_id"] is None
    with pytest.raises(HTTPException):
        tasks.create_task(db, body.model_copy(update={"executor_id": "auto"}))


def priced_executor(settings, db):
    with db.sessions.begin() as session:
        credential = ProviderCredential(
            name="test pricing credential",
            provider_type="model_api",
            ciphertext=CredentialVault(settings.master_key).encrypt(
                "isolated-secret-no-network"
            ),
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
    executor = routing.register(
        db,
        declaration(
            enabled=True,
            currency="CNY",
            input_rate=400000,
            output_rate=500000,
            context_tokens=10,
            credential_id=credential_id,
        ),
    )
    return executor, credential_id


def test_external_preflight_exact_price_context_budget_and_adapter_gate(platform):
    settings, db = platform
    body = task_input(db)
    executor, _ = priced_executor(settings, db)
    policy(db, body.work_id, allowed=[executor], preferred=[executor])
    before = preview(db, body.work_id, input_tokens=3, max_output_tokens=2)
    candidate = next(row for row in before["candidates"] if row["id"] == executor)
    assert candidate["estimated_microunits"] == 3  # ceil(1.2) + ceil(1.0)
    assert set(candidate["reasons"]) == {"ADAPTER_NOT_READY", "BUDGET_MISSING"}
    budgets.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=0, currency="CNY", limit_microunits=2, limit_tokens=4
        ),
    )
    result = preview(db, body.work_id, input_tokens=3, max_output_tokens=2)
    candidate = next(row for row in result["candidates"] if row["id"] == executor)
    assert set(candidate["reasons"]) == {
        "ADAPTER_NOT_READY",
        "MONEY_LIMIT",
        "TOKEN_LIMIT",
    }
    assert result["selected_id"] is None and result["reservation_created"] is False
    budgets.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=1, currency="CNY", limit_microunits=100, limit_tokens=100
        ),
    )
    result = preview(db, body.work_id, input_tokens=3, max_output_tokens=2)
    assert next(row for row in result["candidates"] if row["id"] == executor)[
        "reasons"
    ] == ["ADAPTER_NOT_READY"]
    with pytest.raises(HTTPException):
        tasks.create_task(db, body.model_copy(update={"executor_id": "auto"}))
    edit(db, executor, currency="USD")
    rows = preview(db, body.work_id, input_tokens=10, max_output_tokens=1)["candidates"]
    reasons = next(row for row in rows if row["id"] == executor)["reasons"]
    assert "CONTEXT_EXCEEDED" in reasons and "CURRENCY_MISMATCH" in reasons
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0


def test_credential_deletion_and_unknown_cost_preflight(platform):
    settings, db = platform
    body = task_input(db)
    executor, credential_id = priced_executor(settings, db)
    policy(db, body.work_id, allowed=["human", executor])
    budgets.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=0, currency="CNY", limit_microunits=100, limit_tokens=100
        ),
    )
    # Seed a future paid attempt only inside this isolated fixture.
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, "human")
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        task.executor_id = executor
        reservation = budgets.reserve(
            session,
            task,
            envelope["attempt_id"],
            ReservationInput(
                operation_key="fixture",
                currency="CNY",
                reserved_microunits=5,
                reserved_tokens=5,
            ),
        )
        assert budgets.dispatch(session, task, reservation.id)
    tasks.control(db, task_id, "pause", 2)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        assert (
            client.delete(
                f"/api/v1/credentials/{credential_id}", headers=headers
            ).status_code
            == 204
        )
        response = client.post(
            "/api/v1/executor-routing/preview",
            headers=headers,
            json={
                "work_id": body.work_id,
                "task_type": "analyze",
                "input_tokens": 1,
                "max_output_tokens": 1,
            },
        )
        assert response.status_code == 200
        assert "isolated-secret-no-network" not in response.text
        reasons = next(
            row for row in response.json()["candidates"] if row["id"] == executor
        )["reasons"]
        assert "BUDGET_UNCERTAIN" in reasons and "CREDENTIAL_MISSING" in reasons


def test_concurrent_registration_and_stale_executor_configuration(
    platform, monkeypatch
):
    _, db = platform
    barrier = Barrier(2)

    def create(_):
        barrier.wait(timeout=10)
        return routing.register(db, declaration())

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(create, range(2)))
    assert ids[0] == ids[1]
    item_id = ids[0]
    edit(db, item_id, priority=1)
    with pytest.raises(HTTPException):
        routing.configure(
            db,
            item_id,
            ExecutorUpdate(expected_version=1, name="stale", capabilities=["analyze"]),
        )
    body = task_input(db).model_copy(update={"executor_id": "auto"})
    policy(db, body.work_id, diagnostics=True)
    original_lock = routing.lock_executor

    def reorder_after_lock(session, executor_id):
        item = original_lock(session, executor_id)
        item.priority = -1
        return item

    monkeypatch.setattr(routing, "lock_executor", reorder_after_lock)
    with pytest.raises(HTTPException) as changed:
        tasks.create_task(db, body)
    assert changed.value.status_code == 409
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0
        assert session.get(Executor, "human").priority == 0


def test_concurrent_policy_changes_and_invalid_rules(platform):
    settings, db = platform
    body = task_input(db)
    barrier = Barrier(2)

    def change(ids):
        barrier.wait(timeout=10)
        try:
            return routing.configure_policy(
                db,
                body.work_id,
                PolicyInput(expected_version=0, allowed_ids=ids, preferred_ids=[]),
            )
        except HTTPException as exc:
            assert exc.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(change, [["human"], ["local_test"]]))
    assert results.count(1) == 1 and results.count(None) == 1
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = f"/api/v1/works/{body.work_id}/execution-policy"
        for invalid in (
            {"allowed_ids": ["missing"], "preferred_ids": []},
            {"allowed_ids": ["human"], "preferred_ids": ["local_test"]},
        ):
            assert (
                client.put(
                    path, headers=headers, json={"expected_version": 1, **invalid}
                ).status_code
                == 422
            )
