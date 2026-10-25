"""Local routing and external declaration preflight; no network or live adapters."""

import hashlib
import json
from fastapi import HTTPException
from sqlalchemy import func, select, update
from app.platform.models import (
    AuditEvent,
    Executor,
    ExecutionPolicy,
    Owner,
    ProviderCredential,
    TaskRoutingDecision,
    Work,
    WorkBudget,
)
from app.platform import budget_service

LOCAL_MODES = {"human", "local_test"}
FIELDS = (
    "agent_protocol",
    "name",
    "capabilities",
    "enabled",
    "available",
    "priority",
    "currency",
    "input_rate",
    "output_rate",
    "context_tokens",
    "credential_id",
)


def lock_work(session, work_id):
    if (
        session.execute(
            update(Work).where(Work.id == work_id).values(title=Work.title)
        ).rowcount
        != 1
    ):
        raise HTTPException(404, "作品不存在")


def lock_executor(session, executor_id):
    if (
        session.execute(
            update(Executor)
            .where(Executor.id == executor_id)
            .values(version=Executor.version)
            .execution_options(synchronize_session=False)
        ).rowcount
        != 1
    ):
        raise HTTPException(404, "执行者不存在")
    return session.get(Executor, executor_id, populate_existing=True)


def data(item):
    return {
        "id": item.id,
        "mode": item.mode,
        "version": item.version,
        **{key: getattr(item, key) for key in FIELDS},
        "adapter_ready": item.mode in LOCAL_MODES or offline_agent(item),
        "external_calls": False,
        "paid_calls": False,
    }


def audit(session, action, target, details):
    session.add(
        AuditEvent(
            actor="owner",
            action="executor." + action,
            target_id=target,
            details=details,
        )
    )


def validate_settings(session, mode, body):
    if body.agent_protocol and mode != "agent":
        raise HTTPException(422, "离线 Agent 协议仅适用于 Agent 执行者")
    if body.agent_protocol and (body.currency or body.credential_id):
        raise HTTPException(422, "离线 Agent 不关联供应商凭证或价格，请清空这些字段")
    if mode in LOCAL_MODES:
        if body.credential_id or body.currency or body.context_tokens:
            raise HTTPException(422, "内置执行者不使用供应商凭证、定价或上下文额度")
        if mode == "local_test" and body.capabilities != ["analyze"]:
            raise HTTPException(422, "本地校验只支持分析，不能声明创作能力")
    if body.credential_id:
        credential = session.get(ProviderCredential, body.credential_id)
        expected = "model_api" if mode == "api" else "external_agent"
        if credential is None or credential.provider_type != expected:
            raise HTTPException(422, "凭证不存在或类型与执行者不匹配")


def register(db, body):
    signature = hashlib.sha256(
        json.dumps(
            body.model_dump(
                exclude={"agent_protocol"} if body.agent_protocol is None else set()
            ),
            sort_keys=True,
        ).encode()
    ).hexdigest()
    with db.sessions.begin() as session:
        session.execute(
            update(Owner).where(Owner.id == "owner").values(username=Owner.username)
        )
        previous = session.scalar(
            select(Executor).where(Executor.registration_key == body.request_key)
        )
        if previous:
            if previous.registration_hash != signature:
                raise HTTPException(409, "同一登记标识不能提交不同配置")
            return previous.id
        if session.scalar(select(func.count()).select_from(Executor)) >= 100:
            raise HTTPException(409, "执行者登记已达到 100 个上限，请复用已有配置")
        validate_settings(session, body.mode, body)
        from app.platform.models import identifier

        item = Executor(
            id=identifier(),
            mode=body.mode,
            registration_key=body.request_key,
            registration_hash=signature,
            **body.model_dump(include=set(FIELDS)),
        )
        session.add(item)
        session.flush()
        audit(
            session, "registered", item.id, {"mode": item.mode, "external_calls": False}
        )
        return item.id


def configure(db, executor_id, body):
    with db.sessions.begin() as session:
        item = lock_executor(session, executor_id)
        values = body.model_dump(include=set(FIELDS))
        if item.version != body.expected_version:
            if item.version == body.expected_version + 1 and all(
                getattr(item, key) == value for key, value in values.items()
            ):
                return item.version
            raise HTTPException(409, "执行者配置已变化，请刷新后核对")
        validate_settings(session, item.mode, body)
        for key, value in values.items():
            setattr(item, key, value)
        item.version += 1
        audit(
            session,
            "configured",
            item.id,
            {
                "version": item.version,
                "enabled": item.enabled,
                "available": item.available,
            },
        )
        return item.version


def policy(session, work_id):
    item = session.get(ExecutionPolicy, work_id, populate_existing=True)
    if item is None:
        return {
            "version": 0,
            "allowed_ids": ["human", "local_test"],
            "preferred_ids": ["human"],
            "auto_local_diagnostics": False,
        }
    return {
        key: getattr(item, key)
        for key in ("version", "allowed_ids", "preferred_ids", "auto_local_diagnostics")
    }


def configure_policy(db, work_id, body):
    with db.sessions.begin() as session:
        lock_work(session, work_id)
        current = policy(session, work_id)
        values = body.model_dump(exclude={"expected_version"})
        if current["version"] != body.expected_version:
            if current["version"] == body.expected_version + 1 and all(
                current[key] == value for key, value in values.items()
            ):
                return current["version"]
            raise HTTPException(409, "作品路由规则已变化，请刷新后核对")
        ids = set(session.scalars(select(Executor.id)))
        if set(body.allowed_ids) - ids:
            raise HTTPException(422, "规则包含未登记的执行者")
        item = session.get(ExecutionPolicy, work_id)
        if item is None:
            item = ExecutionPolicy(work_id=work_id, **values)
            session.add(item)
            session.flush()
        else:
            for key, value in values.items():
                setattr(item, key, value)
            item.version += 1
        audit(session, "policy_changed", work_id, {"version": item.version, **values})
        return item.version


def offline_agent(item):
    return item.mode == "agent" and item.agent_protocol == "offline_v1"


def evaluate(
    session, work_id, task_type, input_tokens=0, max_output_tokens=1, *, automatic=True
):
    rules = policy(session, work_id)
    budget = session.get(WorkBudget, work_id, populate_existing=True)
    unknown = budget_service.unknown_count(session, work_id) if budget else 0
    candidates = []
    preferred = rules["preferred_ids"]
    executors = list(
        session.scalars(
            select(Executor)
            .order_by(Executor.id)
            .execution_options(populate_existing=True)
        )
    )
    executors.sort(
        key=lambda item: (
            preferred.index(item.id) if item.id in preferred else len(preferred),
            -item.priority,
            item.id,
        )
    )
    for item in executors:
        reasons = []
        cost = None
        if item.id not in rules["allowed_ids"]:
            reasons.append("NOT_ALLOWED")
        if not item.enabled:
            reasons.append("DISABLED")
        if not item.available:
            reasons.append("UNAVAILABLE")
        if task_type not in item.capabilities:
            reasons.append("CAPABILITY_MISMATCH")
        if (
            automatic
            and item.mode == "local_test"
            and not rules["auto_local_diagnostics"]
        ):
            reasons.append("DIAGNOSTIC_OPT_IN_REQUIRED")
        if item.mode in LOCAL_MODES or offline_agent(item):
            cost = 0
        else:
            reasons.append("ADAPTER_NOT_READY")
            credential = (
                session.get(ProviderCredential, item.credential_id)
                if item.credential_id
                else None
            )
            if credential is None or credential.provider_type != (
                "model_api" if item.mode == "api" else "external_agent"
            ):
                reasons.append("CREDENTIAL_MISSING")
            if item.context_tokens is None:
                reasons.append("CONTEXT_LIMIT_UNKNOWN")
            elif input_tokens + max_output_tokens > item.context_tokens:
                reasons.append("CONTEXT_EXCEEDED")
            if (
                item.currency is None
                or item.input_rate is None
                or item.output_rate is None
            ):
                reasons.append("PRICE_UNKNOWN")
            else:
                cost = (input_tokens * item.input_rate + 999999) // 1000000 + (
                    max_output_tokens * item.output_rate + 999999
                ) // 1000000
            if budget is None:
                reasons.append("BUDGET_MISSING")
            else:
                if unknown:
                    reasons.append("BUDGET_UNCERTAIN")
                if item.currency is not None and item.currency != budget.currency:
                    reasons.append("CURRENCY_MISMATCH")
                if (
                    cost is not None
                    and budget.spent_microunits + budget.held_microunits + cost
                    > budget.limit_microunits
                ):
                    reasons.append("MONEY_LIMIT")
                if (
                    budget.spent_tokens
                    + budget.held_tokens
                    + input_tokens
                    + max_output_tokens
                    > budget.limit_tokens
                ):
                    reasons.append("TOKEN_LIMIT")
        candidates.append(
            {
                "id": item.id,
                "name": item.name,
                "mode": item.mode,
                "executor_version": item.version,
                "agent_protocol": item.agent_protocol,
                "enabled": item.enabled,
                "available": item.available,
                "priority": item.priority,
                "capabilities": item.capabilities,
                "input_rate": item.input_rate,
                "output_rate": item.output_rate,
                "context_tokens": item.context_tokens,
                "reasons": reasons,
                "eligible": not reasons,
                "estimated_microunits": cost,
                "currency": item.currency,
                "estimated_tokens": 0
                if item.mode in LOCAL_MODES or offline_agent(item)
                else input_tokens + max_output_tokens,
            }
        )
    selected = next((item["id"] for item in candidates if item["eligible"]), None)
    return {
        "selected_id": selected,
        "policy_version": rules["version"],
        "rules": rules,
        "task_type": task_type,
        "input_tokens": input_tokens,
        "max_output_tokens": max_output_tokens,
        "budget_version": budget.version if budget else None,
        "candidates": candidates,
        "external_execution": False,
        "reservation_created": False,
    }


def select_for_task(session, work_id, task_type, requested):
    lock_work(session, work_id)
    decision = evaluate(session, work_id, task_type, automatic=requested == "auto")
    selected = decision["selected_id"] if requested == "auto" else requested
    if selected is None:
        raise HTTPException(
            409, "没有符合当前规则的可执行者，请检查执行者与作品路由规则"
        )
    lock_executor(session, selected)
    # Recheck after the lock: disabling an executor must not admit new work.
    decision = evaluate(session, work_id, task_type, automatic=requested == "auto")
    candidate = next(
        (row for row in decision["candidates"] if row["id"] == selected), None
    )
    if candidate is None or not candidate["eligible"]:
        raise HTTPException(409, "执行者不符合当前规则或已停用，请核对配置后重试")
    if requested == "auto" and decision["selected_id"] != selected:
        raise HTTPException(409, "执行者排序已变化，请重新提交任务")
    decision["selected_id"] = selected
    return selected, decision


def ready_for_claim(session, task):
    lock_work(session, task.work_id)
    lock_executor(session, task.executor_id)
    original = session.get(TaskRoutingDecision, task.id)
    decision = evaluate(
        session,
        task.work_id,
        task.task_type,
        automatic=original is not None and original.requested_executor_id == "auto",
    )
    candidate = next(
        (row for row in decision["candidates"] if row["id"] == task.executor_id), None
    )
    return candidate is not None and candidate["eligible"]
