from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier
import sys
import time

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func

from app.platform import model_runtime as runtime, model_transport as transport
from app.platform import (
    task_service as tasks,
    executor_service as routing,
    budget_service as budgets,
)
from app.platform.api import create_app
from app.platform.database import PlatformDatabase
from app.platform.executor_protocol import ExecutorCreate, PolicyInput
from app.platform.budget_protocol import BudgetInput
from app.platform.models import (
    ProviderCredential,
    CreativeTask,
    WorkBudget,
    BudgetReservation,
    BudgetEvent,
    TaskSubmission,
    AssetRevision,
    Executor,
    AuditEvent,
)
from app.platform.security import CredentialVault
from platform_fixtures import signed_in
from test_platform_tasks import task_input, expire, result

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from model_fixture import model_fixture, completion  # noqa: E402


@pytest.fixture
def model():
    with model_fixture() as fixture:
        yield fixture


def setup(platform, model, *, tokens=20000, money=20000):
    settings, db = platform
    with db.sessions.begin() as session:
        credential = ProviderCredential(
            name="fixture-key",
            provider_type="model_api",
            ciphertext=CredentialVault(settings.master_key).encrypt(
                "disposable-fixture-key"
            ),
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
    executor_id = routing.register(
        db,
        ExecutorCreate(
            name="模拟模型",
            mode="api",
            capabilities=["analyze"],
            request_key="fixture-executor",
            enabled=True,
            credential_id=credential_id,
            context_tokens=4096,
            currency="CNY",
            input_rate=1000000,
            output_rate=2000000,
            api_config={
                "endpoint": model.endpoint,
                "model": "fixture-model",
                "max_output_tokens": 128,
                "timeout_seconds": 1,
            },
        ),
    )
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
            limit_tokens=tokens,
            limit_microunits=money,
        ),
    )
    return body


def state(db, task_id):
    with db.sessions() as session:
        return session.get(CreativeTask, task_id).state


def test_model_completion_and_atomic_idempotent_settlement(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    prepared = runtime.prepare(db, settings, envelope)
    reservation_id, chosen, payload, secret = prepared
    assert runtime.prepare(db, settings, envelope) is None
    assert secret == "disposable-fixture-key"
    raw = transport.send(model.endpoint, payload, secret, 1, test_mode=True)
    first = runtime.finish(db, envelope, reservation_id, chosen, raw)
    assert runtime.finish(db, envelope, reservation_id, chosen, raw) == first
    assert not runtime.run_model(db, settings)
    assert len(model.calls) == 1 and payload["stream"] is False and payload["n"] == 1
    assert "tools" not in payload and payload["max_completion_tokens"] == 128
    assert state(db, task_id) == "SUCCEEDED"
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert (
            account.spent_tokens,
            account.spent_microunits,
            account.held_tokens,
        ) == (120, 140, 0)
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 1
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1
        saved = session.scalar(select(TaskSubmission)).envelope
        assert saved["usage"]["accounting"] == "settled"
        assert saved["usage"]["cost_microunits"] == 140
        assert "disposable-fixture-key" not in str(
            [row.details for row in session.scalars(select(AuditEvent))]
        )


@pytest.mark.parametrize(
    "failure",
    [
        "missing_usage",
        "inconsistent_usage",
        "length",
        "tool_calls",
        "empty",
        "over_usage",
        "http_error",
        "redirect",
    ],
)
def test_model_bad_responses_never_mark_success(platform, model, failure):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    unknown = failure in {
        "missing_usage",
        "inconsistent_usage",
        "http_error",
        "redirect",
    }
    if failure == "missing_usage":
        del model.response["usage"]
    elif failure == "inconsistent_usage":
        model.response["usage"]["total_tokens"] = 1
    elif failure in {"length", "tool_calls"}:
        model.response["choices"][0]["finish_reason"] = failure
    elif failure == "empty":
        model.response["choices"][0]["message"]["content"] = ""
    elif failure == "over_usage":
        model.response["usage"] = {
            "prompt_tokens": 5000,
            "completion_tokens": 20,
            "total_tokens": 5020,
        }
    else:
        model.status = 500 if failure == "http_error" else 302
    assert runtime.run_model(db, settings)
    assert state(db, task_id) == "PAUSED"
    assert not runtime.run_model(db, settings) and len(model.calls) == 1
    with db.sessions() as session:
        reservation = session.scalar(select(BudgetReservation))
        assert reservation.state == ("UNKNOWN" if unknown else "SETTLED")
        if unknown:
            assert session.get(WorkBudget, body.work_id).held_tokens == 4096
            version = session.get(CreativeTask, task_id).version
        else:
            assert session.scalar(select(TaskSubmission)).disposition == "PARTIAL"
    if unknown:
        with pytest.raises(HTTPException):
            tasks.control(db, task_id, "resume", version)


def test_timeout_keeps_hold_and_does_not_retry(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    model.on_request = lambda: time.sleep(2)
    start = time.monotonic()
    runtime.run_model(db, settings)
    assert time.monotonic() - start < 4
    assert state(db, task_id) == "PAUSED" and len(model.calls) == 1
    with db.sessions() as session:
        assert session.scalar(select(BudgetReservation)).state == "UNKNOWN"


def test_cancel_during_call_settles_usage_and_quarantines_candidate(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)

    def cancel():
        with db.sessions() as session:
            version = session.get(CreativeTask, task_id).version
        tasks.control(db, task_id, "cancel", version)

    model.on_request = cancel
    runtime.run_model(db, settings)
    assert state(db, task_id) == "CANCELLED"
    with db.sessions() as session:
        assert session.scalar(select(BudgetReservation)).state == "SETTLED"
        assert session.scalar(select(TaskSubmission)).disposition == "QUARANTINED"
        assert session.get(WorkBudget, body.work_id).spent_tokens == 120


def test_crash_after_dispatch_retains_unknown_charge(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
    runtime.prepare(db, settings, envelope)
    expire(db, task_id)
    tasks.recover(db)
    assert not runtime.run_model(db, settings)
    assert model.calls == [] and state(db, task_id) == "PAUSED"
    with db.sessions() as session:
        assert session.scalar(select(BudgetReservation)).state == "UNKNOWN"


def test_lost_dispatch_commit_ack_is_still_unknown(platform, model, monkeypatch):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    original = runtime.prepare

    def lose_ack(*args):
        original(*args)
        raise RuntimeError("Commit succeeded but its acknowledgement was lost")

    monkeypatch.setattr(runtime, "prepare", lose_ack)
    runtime.run_model(db, settings)
    with db.sessions() as session:
        task = session.get(CreativeTask, task_id)
        assert task.state == "PAUSED" and task.reason == "BUDGET_UNCERTAIN"
        assert session.scalar(select(BudgetReservation)).state == "UNKNOWN"
    assert model.calls == []


def test_result_failure_rolls_back_settlement(platform, model, monkeypatch):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)

    def fail(*args, **kwargs):
        raise RuntimeError("injected failure after settlement")

    monkeypatch.setattr(tasks, "submit_in_session", fail)
    runtime.run_model(db, settings)
    with db.sessions() as session:
        account = session.get(WorkBudget, body.work_id)
        assert account.spent_tokens == 0 and account.held_tokens == 4096
        assert session.scalar(select(BudgetReservation)).state == "UNKNOWN"
        assert session.scalar(select(func.count()).select_from(TaskSubmission)) == 0
        assert (
            session.scalar(
                select(func.count())
                .select_from(BudgetEvent)
                .where(BudgetEvent.kind == "SETTLED")
            )
            == 0
        )
    assert len(model.calls) == 1 and state(db, task_id) == "PAUSED"


def test_concurrent_workers_only_send_once(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    barrier = Barrier(2)

    def run():
        barrier.wait(timeout=5)
        return runtime.execute(db, settings, task_id, body.executor_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run) for _ in range(2)]
        assert sum(f.result() for f in futures) == 1
    assert len(model.calls) == 1 and state(db, task_id) == "SUCCEEDED"


@pytest.mark.parametrize("change", ["budget", "profile", "credential"])
def test_preflight_changes_prevent_network_send(platform, model, change):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    with db.sessions.begin() as session:
        if change == "budget":
            session.get(WorkBudget, body.work_id).limit_tokens = 1
        elif change == "profile":
            item = session.get(Executor, body.executor_id)
            item.api_config = {**item.api_config, "model": "different-model"}
        else:
            item = session.get(Executor, body.executor_id)
            session.delete(session.get(ProviderCredential, item.credential_id))
    runtime.run_model(db, settings)
    assert model.calls == [] and state(db, task_id) == "PAUSED"
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0


def test_public_and_non_test_execution_are_disabled(platform, model, monkeypatch):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    assert not runtime.execute(
        db, replace(settings, environment="development"), task_id, body.executor_id
    )
    development = PlatformDatabase(replace(settings, environment="development"))
    try:
        # The production/development gate is independent of endpoint configuration.
        with development.sessions() as session:
            assert session.info["model_api_test"] is False
        with db.sessions.begin() as session:
            item = session.get(Executor, body.executor_id)
            item.api_config = {
                **item.api_config,
                "endpoint": "https://example.com/v1/chat/completions",
            }
        runtime.run_model(db, settings)
        assert model.calls == [] and state(db, task_id) == "PAUSED"
    finally:
        development.engine.dispose()
    monkeypatch.setattr(
        "http.client.HTTPConnection.connect",
        lambda *_: pytest.fail("network must not be used"),
    )
    for endpoint, test_mode in [
        (model.endpoint, False),
        ("https://example.com/v1/chat/completions", True),
        ("http://localhost/v1/chat/completions", True),
    ]:
        with pytest.raises(ValueError):
            transport.send(endpoint, {}, "fixture", 1, test_mode=test_mode)


def test_owner_cannot_forge_model_usage_or_claim(platform, model):
    settings, db = platform
    body = setup(platform, model)
    task_id = tasks.create_task(db, body)
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        assert (
            client.post(f"/api/v1/tasks/{task_id}/claim", headers=headers).status_code
            == 403
        )
        envelope = tasks.try_claim(db, task_id, body.executor_id, provider=True)
        forged = result(envelope).model_dump(mode="json")
        forged["lease_token"] = envelope["lease_token"]
        forged["usage"] = {
            "accounting": "settled",
            "input_tokens": 0,
            "output_tokens": 0,
            "cost_microunits": 0,
        }
        assert (
            client.post(
                f"/api/v1/tasks/{task_id}/results", headers=headers, json=forged
            ).status_code
            == 422
        )
    assert model.calls == []


@pytest.mark.parametrize("block", ["insufficient_budget", "oversized_input"])
def test_task_creation_respects_reserved_ceiling(platform, model, block):
    _, db = platform
    body = setup(platform, model, tokens=1 if block == "insufficient_budget" else 20000)
    if block == "oversized_input":
        body = body.model_copy(update={"objective": "长" * 2000})
    with pytest.raises(HTTPException):
        tasks.create_task(db, body)
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(CreativeTask)) == 0
        assert session.scalar(select(func.count()).select_from(BudgetReservation)) == 0
    assert model.calls == []


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {"prompt_tokens": True, "completion_tokens": 0, "total_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1, "total_tokens": 0},
    ],
)
def test_invalid_usage_is_not_zero_charge(usage):
    response = completion()
    response["usage"] = usage
    with pytest.raises(ValueError):
        runtime.parse_usage(response, {"input_rate": 1, "output_rate": 1})
