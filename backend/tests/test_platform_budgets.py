"""Accounting fault tests use an in-process fake executor; no network or paid calls."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import timedelta
from alembic import command

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from sqlalchemy import func, select, update, delete, insert
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.platform import budget_service as budget, task_service as tasks
from app.platform import executor_service
from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.budget_protocol import BudgetInput, ReservationInput, SettlementInput
from app.platform.models import (
    BudgetEvent,
    BudgetReservation,
    CreativeTask,
    Executor,
    WorkBudget,
    identifier,
    now,
)
from platform_fixtures import signed_in
from test_platform_tasks import task_input, expire


@pytest.fixture(autouse=True)
def offline_accounting_adapter(monkeypatch):
    # Budget tests model a future adapter in process; production routing must keep it closed.
    original = executor_service.ready_for_claim
    monkeypatch.setattr(
        executor_service,
        "ready_for_claim",
        lambda session, task: (
            True if task.executor_id == "test_api" else original(session, task)
        ),
    )


def test_budget_upgrade_preserves_existing_task(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0004_task_runtime")
    body = task_input(db)
    # Seed the historical schema without invoking newer application routing services.
    with db.sessions.begin() as session:
        task_id = identifier()
        session.execute(
            insert(CreativeTask.__table__).values(
                id=task_id,
                work_id=body.work_id,
                executor_id="human",
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
    db.upgrade()
    assert db.require_current() == "0016_summary_decisions"
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert (
            task.state == "QUEUED" and task.input_revision_id == body.input_revision_id
        )
        assert session.get(WorkBudget, body.work_id) is None
    assert tasks.try_claim(db, task_id, "human") is not None


def setup(db, money=100, tokens=100, body=None):
    body = body or task_input(db)
    with db.sessions.begin() as session:
        if session.get(Executor, "test_api") is None:
            session.add(
                Executor(
                    id="test_api",
                    name="In-process fixture only",
                    mode="api",
                    capabilities=["analyze"],
                )
            )
    budget.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=0,
            currency="CNY",
            limit_microunits=money,
            limit_tokens=tokens,
        ),
    )
    task_id = add_task(db, body)
    return body, task_id, tasks.try_claim(db, task_id, "test_api", provider=True)


def add_task(db, body):
    task_id = tasks.create_task(db, body)
    with db.sessions.begin() as session:
        session.get(CreativeTask, task_id).executor_id = "test_api"
    return task_id


def reserve(db, task_id, envelope, key="call-1", money=80, tokens=80, currency="CNY"):
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        item = budget.reserve(
            session,
            task,
            envelope["attempt_id"],
            ReservationInput(
                operation_key=key,
                currency=currency,
                reserved_microunits=money,
                reserved_tokens=tokens,
            ),
        )
        return item.id


def dispatch(db, task_id, reservation_id):
    with db.sessions.begin() as session:
        return budget.dispatch(
            session, tasks.locked_task(session, task_id), reservation_id
        )


def settlement(**changes):
    return SettlementInput(
        **{
            "receipt_key": "provider-call-1",
            "actual_microunits": 60,
            "input_tokens": 30,
            "output_tokens": 20,
            "evidence": "Isolated test provider receipt, no real charge",
            **changes,
        }
    )


def settle(db, task_id, reservation_id, **changes):
    with db.sessions.begin() as session:
        return budget.settle(
            session,
            tasks.locked_task(session, task_id),
            reservation_id,
            settlement(**changes),
        ).id


def test_budget_auth_configuration_currency_and_exact_units(platform):
    settings, db = platform
    body = task_input(db)
    path = f"/api/v1/works/{body.work_id}/budget"
    config = {
        "expected_version": 0,
        "currency": "CNY",
        "limit_microunits": 5_000_000_000,
        "limit_tokens": 7_000_000_000,
    }
    with TestClient(create_app(settings, db)) as client:
        assert client.get(path).status_code == 401
        headers = signed_in(client, settings)
        assert client.get(path).json()["configured"] is False
        assert client.put(path, json=config).status_code == 403
        for invalid in (1.5, "100", True, -1, 10**12 + 1):
            assert (
                client.put(
                    path, json={**config, "limit_microunits": invalid}, headers=headers
                ).status_code
                == 422
            )
        assert client.put(path, json=config, headers=headers).status_code == 200
        assert client.put(path, json=config, headers=headers).json() == {"version": 1}
        current = client.get(path).json()
        assert current["available_microunits"] == 5_000_000_000
        assert current["available_tokens"] == 7_000_000_000
        assert (
            current["external_execution"] is False
            and current["scope"] == "work_lifetime"
        )
        assert (
            client.put(
                path,
                json={**config, "expected_version": 1, "currency": "USD"},
                headers=headers,
            ).status_code
            == 409
        )
        assert (
            client.put(
                path, json={**config, "limit_tokens": 2}, headers=headers
            ).status_code
            == 409
        )
        assert (
            client.put(
                path,
                json={**config, "expected_version": 1, "limit_tokens": 0},
                headers=headers,
            ).status_code
            == 200
        )
        assert len(client.get(path + "/events").json()) == 2
        assert client.get(path + "/reservations").json() == []


def test_concurrent_reservations_cannot_overspend_shared_work(platform):
    _, db = platform
    body, task_id, envelope = setup(db)
    second = add_task(db, body.model_copy(update={"request_key": "second"}))
    second_envelope = tasks.try_claim(db, second, "test_api", provider=True)
    barrier = Barrier(2)

    def claim(pair):
        barrier.wait(timeout=10)
        try:
            return reserve(db, *pair)
        except HTTPException as exc:
            assert exc.status_code == 409
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(claim, [(task_id, envelope), (second, second_envelope)])
        )
    assert sum(item is not None for item in results) == 1
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert account.held_microunits == 80 and account.held_tokens == 80
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 1


def test_limits_currency_and_nonbillable_execution_fail_closed(platform):
    _, db = platform
    body, task_id, envelope = setup(db, money=0, tokens=20)
    with pytest.raises(HTTPException):
        reserve(db, task_id, envelope, money=1, tokens=1)
    with pytest.raises(HTTPException):
        reserve(db, task_id, envelope, money=0, tokens=21)
    with pytest.raises(HTTPException):
        reserve(db, task_id, envelope, money=0, tokens=1, currency="USD")
    with pytest.raises(ValidationError):
        reserve(db, task_id, envelope, money=0, tokens=0)
    item = reserve(db, task_id, envelope, money=0, tokens=20)
    assert reserve(db, task_id, envelope, money=0, tokens=20) == item
    with pytest.raises(HTTPException):
        reserve(db, task_id, envelope, money=0, tokens=19)
    manual_body = task_input(db)
    manual = tasks.create_task(db, manual_body)
    manual_lease = tasks.try_claim(db, manual, "human")
    with pytest.raises(HTTPException):
        reserve(db, manual, manual_lease)
    with db.sessions() as session:
        assert session.get(WorkBudget, body.work_id).held_tokens == 20


def test_settlement_duplicate_atomic_ledger_and_immutable_events(platform):
    _, db = platform
    body, task_id, envelope = setup(db)
    reservation = reserve(db, task_id, envelope)
    with pytest.raises(HTTPException):
        settle(db, task_id, reservation)
    assert dispatch(db, task_id, reservation)
    assert dispatch(db, task_id, reservation) is False
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: settle(db, task_id, reservation), range(2)))
    assert receipts == [reservation, reservation]
    with pytest.raises(HTTPException):
        settle(db, task_id, reservation, actual_microunits=61)
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert (
            account.spent_microunits,
            account.spent_tokens,
            account.held_microunits,
            account.held_tokens,
        ) == (60, 50, 0, 0)
        assert (
            session.scalar(
                select(func.count())
                .select_from(BudgetEvent)
                .where(BudgetEvent.kind == "SETTLED")
            )
            == 1
        )
        event_id = session.scalar(select(BudgetEvent.id))
    for statement in (
        update(BudgetEvent).where(BudgetEvent.id == event_id).values(kind="erased"),
        delete(BudgetEvent).where(BudgetEvent.id == event_id),
    ):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(statement)


def test_cancel_releases_only_unsent_and_reconciliation_does_not_resume(platform):
    settings, db = platform
    body, task_id, envelope = setup(db, money=1000, tokens=1000)
    unsent = reserve(db, task_id, envelope)
    sent = reserve(db, task_id, envelope, key="call-2")
    dispatch(db, task_id, sent)
    tasks.control(db, task_id, "cancel", 2)
    with db.sessions() as session:
        assert session.get(BudgetReservation, unsent).state == "RELEASED"
        item = session.get(BudgetReservation, sent)
        assert item.state == "UNKNOWN"
        item_version = item.version
        assert session.get(WorkBudget, body.work_id).held_microunits == 80
    second = add_task(db, body.model_copy(update={"request_key": "second"}))
    second_envelope = tasks.try_claim(db, second, "test_api", provider=True)
    with pytest.raises(HTTPException):
        reserve(db, second, second_envelope)
    with pytest.raises(HTTPException):
        dispatch(db, task_id, sent)
    path = f"/api/v1/works/{body.work_id}/budget"
    payload = {**settlement().model_dump(), "expected_version": item_version}
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        assert client.get(path).json()["unknown_count"] == 1
        assert len(client.get(path + "/reservations?unknown_only=true").json()) == 1
        assert (
            client.post(
                path + f"/reservations/{sent}/reconcile", json=payload
            ).status_code
            == 403
        )
        assert (
            client.post(
                path + f"/reservations/{sent}/reconcile",
                json={**payload, "evidence": " "},
                headers=headers,
            ).status_code
            == 422
        )
        other = task_input(db)
        assert (
            client.post(
                f"/api/v1/works/{other.work_id}/budget/reservations/{sent}/reconcile",
                json=payload,
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + f"/reservations/{sent}/reconcile", json=payload, headers=headers
            ).status_code
            == 200
        )
        assert (
            client.post(
                path + f"/reservations/{sent}/reconcile", json=payload, headers=headers
            ).status_code
            == 200
        )
        assert client.get(path).json()["unknown_count"] == 0
    with db.sessions() as session:
        assert session.get(CreativeTask, task_id).state == "CANCELLED"
    assert reserve(db, second, second_envelope)


def test_expiry_preserves_unknown_and_overrun_blocks_future_calls(platform):
    _, db = platform
    body, task_id, envelope = setup(db)
    reservation = reserve(db, task_id, envelope)
    dispatch(db, task_id, reservation)
    expire(db, task_id)
    tasks.recover(db)
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert task.state == "PAUSED" and task.reason == "BUDGET_UNCERTAIN"
        version = task.version
    with pytest.raises(HTTPException):
        tasks.control(db, task_id, "resume", version)
    settle(db, task_id, reservation, actual_microunits=130, input_tokens=120)
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert account.spent_microunits == 130 and account.held_microunits == 0
        assert session.scalar(
            select(BudgetEvent).where(BudgetEvent.kind == "SETTLED")
        ).details["over_reservation"]
    tasks.control(db, task_id, "resume", version)
    next_attempt = tasks.try_claim(db, task_id, "test_api", provider=True)
    with pytest.raises(HTTPException):
        reserve(db, task_id, next_attempt)
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        budget_version = account.version
    budget.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=budget_version,
            currency="CNY",
            limit_microunits=1000,
            limit_tokens=1000,
        ),
    )
    assert reserve(db, task_id, next_attempt)


def test_lower_limits_stop_dispatch_without_erasing_hold(platform):
    _, db = platform
    body, task_id, envelope = setup(db)
    reservation = reserve(db, task_id, envelope)
    with db.sessions() as session:
        version = session.get(WorkBudget, body.work_id).version
    budget.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=version, currency="CNY", limit_microunits=0, limit_tokens=0
        ),
    )
    with pytest.raises(HTTPException):
        dispatch(db, task_id, reservation)
    tasks.control(db, task_id, "pause", 2)
    with db.sessions() as session:
        assert session.get(BudgetReservation, reservation).state == "RELEASED"
        assert session.get(WorkBudget, body.work_id).held_microunits == 0


def test_accounting_rollback_on_event_failure(platform, monkeypatch):
    _, db = platform
    body, task_id, envelope = setup(db)
    record = budget.record

    def fail(*args, **kwargs):
        raise RuntimeError("Injected failure before ledger commit")

    monkeypatch.setattr(budget, "record", fail)
    with pytest.raises(RuntimeError):
        reserve(db, task_id, envelope)
    with db.sessions() as session:
        assert session.get(WorkBudget, body.work_id).held_microunits == 0
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0
    monkeypatch.setattr(budget, "record", record)
    reservation = reserve(db, task_id, envelope)
    dispatch(db, task_id, reservation)
    monkeypatch.setattr(budget, "record", fail)
    with pytest.raises(RuntimeError):
        settle(db, task_id, reservation)
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert account.held_microunits == 80 and account.spent_microunits == 0
        assert session.get(BudgetReservation, reservation).state == "DISPATCHED"


def test_same_provider_receipt_cannot_charge_two_works(platform):
    _, db = platform
    first_body, first, first_envelope = setup(db)
    second_body, second, second_envelope = setup(db)
    first_hold = reserve(db, first, first_envelope)
    second_hold = reserve(db, second, second_envelope)
    dispatch(db, first, first_hold)
    dispatch(db, second, second_hold)
    barrier = Barrier(2)

    def finish(pair):
        barrier.wait(timeout=10)
        try:
            return settle(db, *pair)
        except (HTTPException, IntegrityError):
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(finish, [(first, first_hold), (second, second_hold)]))
    assert sum(item is not None for item in results) == 1
    with db.sessions() as session:
        accounts = [
            session.get(WorkBudget, item.work_id) for item in (first_body, second_body)
        ]
        assert sum(item.spent_microunits for item in accounts) == 60
        assert sum(item.held_microunits for item in accounts) == 80
