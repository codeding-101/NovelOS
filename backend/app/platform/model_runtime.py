"""Budgeted model protocol rehearsal. Execution is confined to test-mode loopback."""

import hashlib
from fastapi import HTTPException
from sqlalchemy import select
from app.platform import (
    task_service as tasks,
    executor_service as executors,
    budget_service as budgets,
)
from app.platform import model_transport
from app.platform.model_protocol import messages, input_bound, cost
from app.platform.models import (
    CreativeTask,
    Executor,
    TaskAttempt,
    TaskRoutingDecision,
    ProviderCredential,
    now,
)
from app.platform.security import CredentialVault
from app.platform.task_protocol import ResultEnvelope, Usage
from app.platform.budget_protocol import ReservationInput, SettlementInput


def plan(session, task):
    decision = session.get(TaskRoutingDecision, task.id)
    if decision is None:
        raise ValueError("Missing model plan")
    return next(
        row for row in decision.snapshot["candidates"] if row["id"] == task.executor_id
    )


def pause(db, task_id, attempt_id, reason):
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        budgets.abandon_attempt(session, task, attempt_id)
        if budgets.unknown_count(session, task.work_id, task.id):
            reason = "BUDGET_UNCERTAIN"
        if task.state == "RUNNING" and task.active_attempt_id == attempt_id:
            session.get(TaskAttempt, attempt_id).state = "PAUSED"
            tasks.transition(task, "PAUSED", reason)
            tasks.event(session, task, "model_paused", provider=True, reason=reason)


def prepare(db, settings, envelope):
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, envelope["task_id"])
        executor = session.get(Executor, task.executor_id)
        if not executors.model_test_allowed(executor, settings.environment == "test"):
            raise ValueError("Public model execution is disabled")
        if not executors.ready_for_claim(session, task) or not tasks.current_input(
            session, task
        ):
            raise ValueError("Model plan or input changed")
        chosen = plan(session, task)
        config = chosen["api_config"]
        items = messages(
            task.task_type,
            task.objective,
            envelope["input"]["title"],
            envelope["input"]["content"],
        )
        maximum_input = chosen["context_tokens"] - config["max_output_tokens"]
        if input_bound(items) > maximum_input:
            raise ValueError("Input exceeds declared capacity")
        credential = session.get(ProviderCredential, chosen["credential_id"])
        if credential is None or credential.provider_type != "model_api":
            raise ValueError("Missing fixture credential")
        secret = CredentialVault(settings.master_key).decrypt(credential.ciphertext)
        if (
            not secret
            or not secret.isascii()
            or any(ord(c) < 33 or ord(c) == 127 for c in secret)
        ):
            raise ValueError("Invalid fixture credential format")
        payload = {
            "model": config["model"],
            "messages": items,
            "stream": False,
            "n": 1,
            "max_completion_tokens": config["max_output_tokens"],
        }
        reservation = budgets.reserve(
            session,
            task,
            envelope["attempt_id"],
            ReservationInput(
                operation_key="chat_completion_v1",
                currency=chosen["currency"],
                reserved_tokens=chosen["context_tokens"],
                reserved_microunits=cost(
                    maximum_input,
                    config["max_output_tokens"],
                    chosen["input_rate"],
                    chosen["output_rate"],
                ),
            ),
        )
        if not budgets.dispatch(session, task, reservation.id):
            return None
        tasks.event(
            session,
            task,
            "model_dispatched",
            provider=True,
            reservation_id=reservation.id,
            request_sha256=tasks.fingerprint(payload),
        )
        return reservation.id, chosen, payload, secret


def parse_usage(raw, chosen):
    if (
        not isinstance(raw, dict)
        or not isinstance(raw.get("id"), str)
        or not raw["id"]
        or len(raw["id"]) > 1024
    ):
        raise ValueError("Missing provider receipt")
    usage = raw.get("usage")
    if not isinstance(usage, dict):
        raise ValueError("Missing usage")
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if type(usage.get(key)) is not int or not 0 <= usage[key] <= 10**9:
            raise ValueError("Invalid usage")
    if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
        raise ValueError("Inconsistent usage")
    if raw.get("service_tier") not in (None, "default", "auto"):
        raise ValueError("Undeclared service tier pricing")
    for detail in ("prompt_tokens_details", "completion_tokens_details"):
        fields = usage.get(detail) or {}
        if not isinstance(fields, dict) or any(
            fields.get(key, 0)
            for key in ("audio_tokens", "image_tokens", "cache_write_tokens")
        ):
            raise ValueError("Unsupported billing category")
    return SettlementInput(
        receipt_key="chat:" + hashlib.sha256(raw["id"].encode()).hexdigest(),
        input_tokens=usage["prompt_tokens"],
        output_tokens=usage["completion_tokens"],
        actual_microunits=cost(
            usage["prompt_tokens"],
            usage["completion_tokens"],
            chosen["input_rate"],
            chosen["output_rate"],
        ),
        evidence="Loopback fixture token usage priced at the immutable task declaration; not a real provider invoice.",
    )


def finish(db, envelope, reservation_id, chosen, raw):
    usage = parse_usage(raw, chosen)
    content, complete = "", False
    choices = raw.get("choices")
    if isinstance(choices, list) and len(choices) == 1 and isinstance(choices[0], dict):
        choice = choices[0]
        message = choice.get("message")
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            content = message["content"]
            complete = (
                choice.get("finish_reason") == "stop"
                and message.get("role") == "assistant"
                and not message.get("tool_calls")
                and not message.get("function_call")
                and not message.get("refusal")
            )
    if len(content.encode("utf-8")) > 1024 * 1024 or "\x00" in content:
        content, complete = "", False
    config = chosen["api_config"]
    exceeded = (
        usage.input_tokens > chosen["context_tokens"] - config["max_output_tokens"]
        or usage.output_tokens > config["max_output_tokens"]
    )
    complete = complete and bool(content.strip()) and not exceeded
    body = ResultEnvelope(
        task_id=envelope["task_id"],
        attempt_id=envelope["attempt_id"],
        executor_id=envelope["executor_id"],
        lease_token=envelope["lease_token"],
        submission_id="model:" + reservation_id,
        result_status="success" if complete else "partial",
        finish_reason="complete" if complete else "truncated",
        output_artifacts=[
            {
                "kind": envelope["task_type"],
                "title": "模型协议测试候选（模拟服务）",
                "content": content,
            }
        ]
        if content.strip()
        else [],
        evidence_revision_ids=[envelope["input"]["revision_id"]],
        warnings=["来自本机模拟服务，不是真实模型创作或检测报告。"]
        + (
            []
            if complete
            else ["输出不完整、超出声明用量或包含不支持的结果类型，请人工复查。"]
        ),
        usage=Usage(
            accounting="settled",
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_microunits=usage.actual_microunits,
        ),
    )
    with db.sessions.begin() as session:
        task = tasks.locked_task(session, envelope["task_id"])
        reservation = budgets.settle(
            session, task, reservation_id, usage, actor="worker:model_api"
        )
        return tasks.submit_in_session(
            session, task, body, provider_reservation=reservation
        )


def execute(db, settings, task_id, executor_id):
    if settings.environment != "test":
        return False
    envelope = tasks.try_claim(db, task_id, executor_id, provider=True)
    if envelope is None:
        return False
    dispatched = False
    try:
        prepared = prepare(db, settings, envelope)
        if prepared is None:
            return True
        reservation_id, chosen, payload, secret = prepared
        dispatched = True
        raw = model_transport.send(
            chosen["api_config"]["endpoint"],
            payload,
            secret,
            chosen["api_config"]["timeout_seconds"],
            test_mode=True,
        )
        finish(db, envelope, reservation_id, chosen, raw)
    except Exception:
        # Never log supplier exceptions/bodies: they may echo credentials or manuscripts.
        pause(
            db,
            task_id,
            envelope["attempt_id"],
            "BUDGET_UNCERTAIN" if dispatched else "MODEL_PREFLIGHT_FAILED",
        )
    return True


def run_model(db, settings):
    if settings.environment != "test":
        return False
    with db.sessions() as session:
        candidates = list(
            session.execute(
                select(CreativeTask.id, CreativeTask.executor_id)
                .join(Executor, Executor.id == CreativeTask.executor_id)
                .where(
                    Executor.mode == "api",
                    CreativeTask.state.in_(["QUEUED", "RETRY_WAIT"]),
                    CreativeTask.available_at <= now(),
                    ~tasks.dependency_condition(),
                )
                .order_by(
                    CreativeTask.priority.desc(),
                    CreativeTask.created_at,
                    CreativeTask.id,
                )
                .limit(50)
            )
        )
    for task_id, executor_id in candidates:
        try:
            if execute(db, settings, task_id, executor_id):
                return True
        except HTTPException:
            continue
    return False
