from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Barrier

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform import budget_service as budgets, executor_service as routing
from app.platform import (
    model_runtime as runtime,
    quota_service as quotas,
    task_service as tasks,
)
from app.platform.api import create_app
from app.platform.budget_protocol import BudgetInput, ReservationInput, SettlementInput
from app.platform.database import migration_config
from app.platform.executor_protocol import ExecutorCreate, PolicyInput
from app.platform.models import (
    CreativeTask,
    Executor,
    MonthlyQuota,
    QuotaBinding,
    QuotaBooking,
    Work,
    WorkBudget,
)
from app.platform.quotas import QuotaBind, QuotaCreate, QuotaUpdate
from app.platform.task_protocol import TaskSpendingUpdate
from platform_fixtures import signed_in
from test_platform_models import model_fixture, setup
from test_platform_tasks import task_input


@pytest.fixture
def model():
    with model_fixture() as fixture:
        yield fixture


def value(db, quota_id):
    with db.sessions() as session:
        return quotas.summary(session, session.get(MonthlyQuota, quota_id))


def change(db, quota_id, limit):
    return quotas.configure(
        db,
        quota_id,
        QuotaUpdate(
            expected_version=value(db, quota_id)["version"], limit_tokens=limit
        ),
    )


def new_quota(db, limit=10000, key="pool-1", zone="Asia/Shanghai"):
    return quotas.create(
        db,
        QuotaCreate(
            name="模拟共享额度", timezone=zone, limit_tokens=limit, request_key=key
        ),
    )


def bind(db, quota_id, executor_id):
    return quotas.bind(
        db,
        quota_id,
        QuotaBind(
            expected_version=value(db, quota_id)["version"], executor_id=executor_id
        ),
    )


def prepare(platform, model, limit=10000):
    _, db = platform
    body = setup(platform, model)
    pool = new_quota(db, limit)
    bind(db, pool, body.executor_id)
    task_id = tasks.create_task(db, body)
    return body, pool, task_id


def second_work(db, executor_id):
    body = task_input(db, executor=executor_id)
    routing.configure_policy(
        db,
        body.work_id,
        PolicyInput(
            expected_version=0, allowed_ids=[executor_id], preferred_ids=[executor_id]
        ),
    )
    budgets.configure(
        db,
        body.work_id,
        BudgetInput(
            expected_version=0,
            currency="CNY",
            limit_tokens=20000,
            limit_microunits=20000,
        ),
    )
    return body


def reserve(db, task_id, attempt_id, key="call-1", tokens=4096):
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        item = budgets.reserve(
            session,
            task,
            attempt_id,
            ReservationInput(
                operation_key=key,
                currency="CNY",
                reserved_tokens=tokens,
                reserved_microunits=100,
            ),
        )
        return item.id


def settle(db, task_id, reservation_id, tokens=120):
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        budgets.settle(
            session,
            task,
            reservation_id,
            SettlementInput(
                receipt_key=reservation_id,
                actual_microunits=100,
                input_tokens=tokens,
                output_tokens=0,
                evidence="本机测试用量确认",
            ),
        )


def task_version(db, task_id):
    with db.sessions() as session:
        return session.get(CreativeTask, task_id).version


def test_shared_limit_across_works_and_executor_aliases(platform, model):
    settings, db = platform
    first, pool, first_id = prepare(platform, model, 4215)
    with db.sessions() as session:
        executor = session.get(Executor, first.executor_id)
        fields = {key: getattr(executor, key) for key in routing.FIELDS}
    alias = routing.register(
        db, ExecutorCreate(**fields, mode="api", request_key="alias-2")
    )
    bind(db, pool, alias)
    second = second_work(db, alias)
    second_id = tasks.create_task(db, second)
    runtime.execute(db, settings, first_id, first.executor_id)
    assert value(db, pool)["spent_tokens"] == 120
    assert tasks.try_claim(db, second_id, alias, provider=True) is None
    with db.sessions() as session:
        task = session.get(CreativeTask, second_id)
        assert (task.state, task.reason, task.attempt_count) == (
            "PAUSED",
            "MONTHLY_QUOTA_EXCEEDED",
            0,
        )
    with pytest.raises(quotas.QuotaExceeded):
        tasks.control(db, second_id, "resume", task_version(db, second_id))
    change(db, pool, 10000)
    tasks.control(db, second_id, "resume", task_version(db, second_id))
    model.response["id"] = "alias-response"
    runtime.execute(db, settings, second_id, alias)
    assert value(db, pool)["spent_tokens"] == 240
    assert len(model.calls) == 2


def test_concurrent_cross_work_reservations_do_not_exceed_pool(platform, model):
    _, db = platform
    first, pool, first_id = prepare(platform, model, 5000)
    with db.sessions() as session:
        executor = session.get(Executor, first.executor_id)
        fields = {key: getattr(executor, key) for key in routing.FIELDS}
    alias = routing.register(
        db, ExecutorCreate(**fields, mode="api", request_key="parallel-alias")
    )
    bind(db, pool, alias)
    second = second_work(db, alias)
    second_id = tasks.create_task(db, second)
    envelopes = [
        tasks.try_claim(db, task_id, executor_id, provider=True)
        for task_id, executor_id in ((first_id, first.executor_id), (second_id, alias))
    ]
    barrier = Barrier(2)

    def invoke(envelope):
        barrier.wait(timeout=5)
        try:
            return reserve(db, envelope["task_id"], envelope["attempt_id"])
        except quotas.QuotaExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, envelopes))
    assert sum(row is not None for row in results) == 1
    assert value(db, pool)["held_tokens"] == 4096
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaBooking)) == 1
        assert session.scalar(select(func.sum(WorkBudget.held_tokens))) == 4096


def test_lowered_pool_rechecked_before_send_and_release(platform, model):
    _, db = platform
    body, pool, task_id = prepare(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation = reserve(db, task_id, envelope["attempt_id"])
    assert reserve(db, task_id, envelope["attempt_id"]) == reservation
    change(db, pool, 0)
    with pytest.raises(quotas.QuotaExceeded):
        with db.sessions.begin() as session:
            budgets.dispatch(session, tasks.locked_task(session, task_id), reservation)
    assert value(db, pool)["over_limit"]
    tasks.control(db, task_id, "pause", task_version(db, task_id))
    assert value(db, pool)["held_tokens"] == 0
    assert model.calls == []


def test_month_boundary_unknown_hold_and_late_settlement(platform, model, monkeypatch):
    settings, db = platform
    clock = [datetime(2026, 10, 31, 15, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(quotas, "now", lambda: clock[0])
    body, pool, task_id = prepare(platform, model, 5000)
    second = second_work(db, body.executor_id)
    second_id = tasks.create_task(db, second)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation = runtime.prepare(db, settings, envelope)[0]
    tasks.control(db, task_id, "pause", task_version(db, task_id))
    clock[0] = datetime(2026, 10, 31, 16, 0, tzinfo=timezone.utc)
    snapshot = value(db, pool)
    assert (
        snapshot["period"],
        snapshot["spent_tokens"],
        snapshot["held_tokens"],
        snapshot["unknown_count"],
    ) == ("2026-11", 0, 4096, 1)
    assert tasks.try_claim(db, second_id, body.executor_id, provider=True) is None
    settle(db, task_id, reservation)
    assert value(db, pool)["held_tokens"] == 0
    assert value(db, pool)["spent_tokens"] == 0
    with db.sessions() as session:
        assert (
            quotas.summary(session, session.get(MonthlyQuota, pool), "2026-10")[
                "spent_tokens"
            ]
            == 120
        )
    assert model.calls == []


def test_old_unsent_reservation_cannot_dispatch_in_new_month(
    platform, model, monkeypatch
):
    _, db = platform
    clock = [datetime(2026, 12, 31, 15, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(quotas, "now", lambda: clock[0])
    body, pool, task_id = prepare(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation = reserve(db, task_id, envelope["attempt_id"])
    clock[0] = datetime(2026, 12, 31, 16, 0, tzinfo=timezone.utc)
    with pytest.raises(quotas.QuotaExceeded, match="月份"):
        with db.sessions.begin() as session:
            budgets.dispatch(session, tasks.locked_task(session, task_id), reservation)
    assert value(db, pool)["period"] == "2027-01"
    tasks.control(db, task_id, "pause", task_version(db, task_id))
    tasks.control(db, task_id, "resume", task_version(db, task_id))
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    fresh = reserve(db, task_id, envelope["attempt_id"])
    with db.sessions.begin() as session:
        assert budgets.dispatch(session, tasks.locked_task(session, task_id), fresh)
        assert session.get(QuotaBooking, fresh).period == "2027-01"


def test_settled_month_rolls_over_and_utc_boundary(platform, model, monkeypatch):
    settings, db = platform
    clock = [datetime(2026, 10, 31, 23, 59, tzinfo=timezone.utc)]
    monkeypatch.setattr(quotas, "now", lambda: clock[0])
    body = setup(platform, model)
    pool = new_quota(db, zone="UTC")
    bind(db, pool, body.executor_id)
    task_id = tasks.create_task(db, body)
    runtime.execute(db, settings, task_id, body.executor_id)
    assert value(db, pool)["spent_tokens"] == 120
    clock[0] = datetime(2026, 11, 1, tzinfo=timezone.utc)
    assert value(db, pool)["spent_tokens"] == 0
    with db.sessions() as session:
        assert (
            quotas.summary(session, session.get(MonthlyQuota, pool), "2026-10")[
                "spent_tokens"
            ]
            == 120
        )


def test_actual_overspend_retained_duplicate_settlement_and_rollback(platform, model):
    settings, db = platform
    body, pool, task_id = prepare(platform, model, 4096)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reservation = runtime.prepare(db, settings, envelope)[0]
    with pytest.raises(RuntimeError):
        with db.sessions.begin() as session:
            budgets.settle(
                session,
                tasks.locked_task(session, task_id),
                reservation,
                SettlementInput(
                    receipt_key="rollback",
                    actual_microunits=0,
                    input_tokens=1,
                    output_tokens=0,
                    evidence="rollback fixture",
                ),
            )
            raise RuntimeError("atomic rollback")
    assert value(db, pool)["held_tokens"] == 4096
    settle(db, task_id, reservation, 5000)
    settle(db, task_id, reservation, 5000)
    snapshot = value(db, pool)
    assert (
        snapshot["spent_tokens"] == 5000
        and snapshot["over_limit"]
        and snapshot["held_tokens"] == 0
    )


def test_binding_rejects_reassignment_and_historical_usage(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    runtime.execute(db, settings, task_id, body.executor_id)
    pool = new_quota(db)
    with pytest.raises(HTTPException, match="已有调用账目"):
        bind(db, pool, body.executor_id)
    with pytest.raises(HTTPException):
        bind(db, pool, "human")
    assert value(db, pool)["executor_ids"] == []


def test_idempotence_versions_fixed_attribution_and_routing(platform, model):
    _, db = platform
    body, pool, task_id = prepare(platform, model)
    old_version = value(db, pool)["version"]
    assert bind(db, pool, body.executor_id)["version"] == old_version
    other = new_quota(db, key="other")
    with pytest.raises(HTTPException):
        bind(db, other, body.executor_id)
    change(db, pool, 0)
    assert new_quota(db) == pool
    assert value(db, pool)["limit_tokens"] == 0
    with pytest.raises(HTTPException):
        new_quota(db, limit=1)
    with pytest.raises(HTTPException):
        quotas.configure(
            db, pool, QuotaUpdate(expected_version=old_version, limit_tokens=5000)
        )
    with db.sessions() as session:
        preview = routing.evaluate(session, body.work_id, body.task_type)
        row = next(
            row for row in preview["candidates"] if row["id"] == body.executor_id
        )
        assert "MONTHLY_QUOTA_EXCEEDED" in row["reasons"]
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(
                text("DELETE FROM quota_bindings WHERE executor_id = :id"),
                {"id": body.executor_id},
            )
    change(db, pool, 10000)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    reserve(db, task_id, envelope["attempt_id"])
    with pytest.raises(DBAPIError):
        with db.sessions.begin() as session:
            session.execute(text("UPDATE quota_bookings SET period = '2000-01'"))


def test_owner_api_permissions_validation_and_history(platform, model):
    settings, db = platform
    body, pool, task_id = prepare(platform, model)
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/v1/quotas").status_code == 401
        headers = signed_in(client, settings)
        payload = {
            "expected_version": value(db, pool)["version"],
            "limit_tokens": 10000,
        }
        assert client.put(f"/api/v1/quotas/{pool}", json=payload).status_code == 403
        assert (
            client.put(
                f"/api/v1/quotas/{pool}",
                json={**payload, "limit_tokens": True},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/v1/quotas/{pool}",
                json={**payload, "timezone": "UTC"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.put(
                f"/api/v1/quotas/{pool}",
                json=payload,
                headers={**headers, "Origin": "https://foreign.invalid"},
            ).status_code
            == 403
        )
        assert (
            client.get(f"/api/v1/quotas/{pool}").json()["external_execution"] is False
        )
        assert client.get("/api/v1/quotas?limit=101").status_code == 422
        runtime.execute(db, settings, task_id, body.executor_id)
        rows = client.get(f"/api/v1/quotas/{pool}/calls").json()
        assert (
            len(rows) == 1
            and rows[0]["state"] == "SETTLED"
            and rows[0]["work_id"] == body.work_id
        )
        assert client.get(f"/api/v1/quotas/{pool}/calls?offset=1").json() == []
        events = client.get(f"/api/v1/quotas/{pool}/events").json()
        assert {row["action"] for row in events} == {"quota.created", "quota.bound"}
    with TestClient(create_app(settings, db)) as agent:
        assert (
            agent.get(
                "/api/v1/quotas", headers={"Authorization": "Bearer agent-only"}
            ).status_code
            == 401
        )


def test_upgrade_preserves_prior_assets_without_inventing_pools(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0009_task_budgets")
    with db.sessions.begin() as session:
        work = Work(title="升级前作品")
        session.add(work)
        session.flush()
        work_id = work.id
    db.upgrade()
    assert db.require_current() == "0012_story_states"
    with db.sessions() as session:
        assert session.get(Work, work_id).title == "升级前作品"
        for table in (MonthlyQuota, QuotaBinding, QuotaBooking):
            assert session.scalar(select(func.count()).select_from(table)) == 0


@pytest.mark.parametrize("layer", ["work", "task"])
def test_shared_quota_never_bypasses_other_budget_limits(platform, model, layer):
    _, db = platform
    body, pool, task_id = prepare(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    if layer == "work":
        with db.sessions() as session:
            version = session.get(WorkBudget, body.work_id).version
        budgets.configure(
            db,
            body.work_id,
            BudgetInput(
                expected_version=version,
                currency="CNY",
                limit_tokens=50,
                limit_microunits=20000,
            ),
        )
    else:
        budgets.configure_task(
            db,
            task_id,
            TaskSpendingUpdate(
                expected_version=task_version(db, task_id),
                currency="CNY",
                limit_tokens=50,
                limit_microunits=20000,
            ),
        )
    with pytest.raises(HTTPException):
        reserve(db, task_id, envelope["attempt_id"], tokens=100)
    assert value(db, pool)["held_tokens"] == 0
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaBooking)) == 0
        assert session.get(WorkBudget, body.work_id).held_tokens == 0


def test_binding_race_with_first_reservation_has_single_valid_outcome(platform, model):
    _, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    pool = new_quota(db, limit=0)
    barrier = Barrier(2)

    def bind_or_reserve(action):
        barrier.wait(timeout=5)
        try:
            if action == "bind":
                bind(db, pool, body.executor_id)
            else:
                reserve(db, task_id, envelope["attempt_id"])
            return action
        except HTTPException:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(bind_or_reserve, ["bind", "reserve"]))
    assert sum(outcome is not None for outcome in outcomes) == 1
    assert value(db, pool)["held_tokens"] == 0
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaBooking)) == 0
        assert (session.get(QuotaBinding, body.executor_id) is not None) == (
            outcomes[0] == "bind"
        )


def test_reservation_rollback_leaves_no_quota_or_work_hold(platform, model):
    _, db = platform
    body, pool, task_id = prepare(platform, model)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    version = value(db, pool)["version"]
    with pytest.raises(RuntimeError):
        with db.sessions.begin() as session:
            task = tasks.locked_task(session, task_id)
            budgets.reserve(
                session,
                task,
                envelope["attempt_id"],
                ReservationInput(
                    operation_key="rollback",
                    currency="CNY",
                    reserved_tokens=100,
                    reserved_microunits=100,
                ),
            )
            session.flush()
            raise RuntimeError("rollback all ledgers")
    assert value(db, pool)["version"] == version
    assert value(db, pool)["held_tokens"] == 0
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(QuotaBooking)) == 0
        assert session.get(WorkBudget, body.work_id).held_tokens == 0
