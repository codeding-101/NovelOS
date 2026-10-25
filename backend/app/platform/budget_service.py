"""Transactional accounting. Callers lock task first, then budget; no network I/O."""

from fastapi import HTTPException
from sqlalchemy import func, select, update, case

from app.platform.models import (
    AuditEvent,
    BudgetEvent,
    BudgetReservation,
    WorkBudget,
    Work,
    Executor,
    TaskAttempt,
    TaskRoutingDecision,
    now,
)
from app.platform.security import utc
from app.platform.budget_protocol import BudgetInput, ReservationInput, SettlementInput
import hashlib
import json


def signature(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def lock_budget(session, work_id):
    result = session.execute(
        update(WorkBudget)
        .where(WorkBudget.work_id == work_id)
        .values(version=WorkBudget.version)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(409, "请先配置作品预算；未配置时不允许外部调用")
    return session.get(WorkBudget, work_id, populate_existing=True)


def record(session, budget, kind, reservation=None, actor="runtime", **details):
    snapshot = {
        key: getattr(budget, key)
        for key in (
            "currency",
            "limit_microunits",
            "limit_tokens",
            "spent_microunits",
            "held_microunits",
            "spent_tokens",
            "held_tokens",
            "version",
        )
    }
    session.add(
        BudgetEvent(
            work_id=budget.work_id,
            reservation_id=reservation.id if reservation else None,
            kind=kind,
            actor=actor,
            details={**details, "balance": snapshot},
        )
    )
    session.add(
        AuditEvent(
            actor=actor,
            action="budget." + kind.lower(),
            target_id=budget.work_id,
            details={"reservation_id": reservation.id if reservation else None},
        )
    )


def configure(db, work_id, body: BudgetInput):
    with db.sessions.begin() as session:
        # Serializes first-time creation as well as subsequent limit edits.
        result = session.execute(
            update(Work).where(Work.id == work_id).values(title=Work.title)
        )
        if result.rowcount != 1:
            raise HTTPException(404, "作品不存在")
        budget = session.get(WorkBudget, work_id)
        if budget is None:
            if body.expected_version != 0:
                raise HTTPException(409, "预算版本已变化，请刷新")
            budget = WorkBudget(
                work_id=work_id, **body.model_dump(exclude={"expected_version"})
            )
            session.add(budget)
            session.flush()
        else:
            budget = lock_budget(session, work_id)
            if budget.version != body.expected_version:
                # A lost response may be retried with exactly the same configuration.
                if budget.version == body.expected_version + 1 and all(
                    getattr(budget, k) == v
                    for k, v in body.model_dump(exclude={"expected_version"}).items()
                ):
                    return budget.version
                raise HTTPException(409, "预算版本已变化，请刷新后再保存")
            if budget.currency != body.currency:
                raise HTTPException(409, "预算币种不可更换，当前不支持自动换汇")
            # Lowering the limit is allowed: it blocks new reservations without hiding debt.
            budget.limit_microunits, budget.limit_tokens = (
                body.limit_microunits,
                body.limit_tokens,
            )
            budget.version += 1
        record(session, budget, "CONFIGURED", actor="owner")
        return budget.version


def unknown_count(session, work_id, task_id=None):
    query = (
        select(func.count())
        .select_from(BudgetReservation)
        .where(
            BudgetReservation.work_id == work_id, BudgetReservation.state == "UNKNOWN"
        )
    )
    if task_id:
        query = query.where(BudgetReservation.task_id == task_id)
    return session.scalar(query)


def require_active(task, attempt):
    if (
        attempt is None
        or attempt.task_id != task.id
        or task.active_attempt_id != attempt.id
        or task.state != "RUNNING"
        or attempt.state != "RUNNING"
        or min(
            utc(task.deadline_at), utc(attempt.deadline_at), utc(attempt.lease_until)
        )
        <= now()
    ):
        raise HTTPException(409, "执行已结束或过期，不能预留或发起调用")


class TaskBudgetExceeded(HTTPException):
    def __init__(self, message="任务累计预算不足，请在任务详情核对上限与已有占用"):
        super().__init__(409, message)


def task_summary(session, task):
    held = BudgetReservation.state.in_(["RESERVED", "DISPATCHED", "UNKNOWN"])
    settled = BudgetReservation.state == "SETTLED"
    totals = session.execute(
        select(
            func.coalesce(
                func.sum(case((settled, BudgetReservation.actual_microunits), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(
                    case(
                        (
                            settled,
                            BudgetReservation.input_tokens
                            + BudgetReservation.output_tokens,
                        ),
                        else_=0,
                    )
                ),
                0,
            ),
            func.coalesce(
                func.sum(case((held, BudgetReservation.reserved_microunits), else_=0)),
                0,
            ),
            func.coalesce(
                func.sum(case((held, BudgetReservation.reserved_tokens), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((BudgetReservation.state == "UNKNOWN", 1), else_=0)), 0
            ),
        ).where(BudgetReservation.task_id == task.id)
    ).one()
    spent_money, spent_tokens, held_money, held_tokens, unknown = totals
    executor = session.get(Executor, task.executor_id)
    configured = (
        task.budget_limit_microunits is not None
        and task.budget_limit_tokens is not None
    )
    currency = task.budget_currency
    if not currency:
        decision = session.get(TaskRoutingDecision, task.id)
        if decision:
            candidate = next(
                (
                    row
                    for row in decision.snapshot["candidates"]
                    if row["id"] == task.executor_id
                ),
                {},
            )
            currency = candidate.get("currency")
    return {
        "supported": executor.mode == "api" and executor.api_config is not None,
        "configured": configured,
        "currency": currency,
        "limit_microunits": task.budget_limit_microunits,
        "limit_tokens": task.budget_limit_tokens,
        "spent_microunits": spent_money,
        "spent_tokens": spent_tokens,
        "held_microunits": held_money,
        "held_tokens": held_tokens,
        "unknown_count": unknown,
        "remaining_microunits": max(
            0, task.budget_limit_microunits - spent_money - held_money
        )
        if configured
        else None,
        "remaining_tokens": max(
            0, task.budget_limit_tokens - spent_tokens - held_tokens
        )
        if configured
        else None,
        "over_limit": configured
        and (
            spent_money + held_money > task.budget_limit_microunits
            or spent_tokens + held_tokens > task.budget_limit_tokens
        ),
    }


def check_task_limit(session, task, money=0, tokens=0, currency=None):
    summary = task_summary(session, task)
    if not summary["configured"]:
        if summary["supported"]:
            raise TaskBudgetExceeded("任务尚未配置累计预算，请在任务详情设置后恢复")
        return
    if currency is not None and currency != task.budget_currency:
        raise TaskBudgetExceeded("调用币种与任务预算不一致")
    if (
        summary["spent_microunits"] + summary["held_microunits"] + money
        > task.budget_limit_microunits
        or summary["spent_tokens"] + summary["held_tokens"] + tokens
        > task.budget_limit_tokens
    ):
        raise TaskBudgetExceeded()


def check_next_task_call(session, task):
    executor = session.get(Executor, task.executor_id)
    if executor.mode != "api" or executor.api_config is None:
        return
    decision = session.get(TaskRoutingDecision, task.id)
    if decision is None:
        raise TaskBudgetExceeded("任务缺少调用计划，请重新创建任务")
    candidate = next(
        row for row in decision.snapshot["candidates"] if row["id"] == task.executor_id
    )
    check_task_limit(
        session,
        task,
        candidate["estimated_microunits"],
        candidate["estimated_tokens"],
        candidate["currency"],
    )


def configure_task(db, task_id, body):
    from app.platform import task_service as tasks

    with db.sessions.begin() as session:
        task = tasks.locked_task(session, task_id)
        summary = task_summary(session, task)
        if not summary["supported"] or task.state in tasks.TERMINAL:
            raise HTTPException(409, "只可调整未结束的模型任务预算")
        if task.version != body.expected_version:
            raise HTTPException(
                409, "任务版本已变化，请刷新后核对；输入未覆盖服务器配置"
            )
        if body.currency != summary["currency"]:
            raise HTTPException(409, "任务预算币种必须与创建时的调用计划一致")
        before = {
            "currency": task.budget_currency,
            "limit_microunits": task.budget_limit_microunits,
            "limit_tokens": task.budget_limit_tokens,
        }
        task.budget_currency = body.currency
        task.budget_limit_microunits = body.limit_microunits
        task.budget_limit_tokens = body.limit_tokens
        task.version += 1
        tasks.event(
            session,
            task,
            "budget_configured",
            before=before,
            after=body.model_dump(exclude={"expected_version"}),
        )
        return {"version": task.version, "budget": task_summary(session, task)}


def reserve(session, task, attempt_id, body: ReservationInput):
    """Internal adapter hook, not an HTTP endpoint. Caller holds the task lock."""
    attempt = session.get(TaskAttempt, attempt_id)
    require_active(task, attempt)
    executor = session.get(Executor, task.executor_id)
    if executor.mode not in {"api", "agent"} or executor.agent_protocol == "offline_v1":
        raise HTTPException(409, "人工、本地校验与离线 Agent 不使用外部调用预算")
    budget = lock_budget(session, task.work_id)
    previous = session.scalar(
        select(BudgetReservation).where(
            BudgetReservation.attempt_id == attempt_id,
            BudgetReservation.operation_key == body.operation_key,
        )
    )
    if previous:
        if any(
            getattr(previous, key) != value for key, value in body.model_dump().items()
        ):
            raise HTTPException(409, "同一调用标识不能改变预留金额或用量")
        return previous
    if budget.currency != body.currency:
        raise HTTPException(409, "调用币种与作品预算不一致")
    if unknown_count(session, task.work_id):
        raise HTTPException(409, "作品存在待对账调用，确认费用前不能新增调用")
    check_task_limit(
        session, task, body.reserved_microunits, body.reserved_tokens, body.currency
    )
    if (
        budget.spent_microunits + budget.held_microunits + body.reserved_microunits
        > budget.limit_microunits
        or budget.spent_tokens + budget.held_tokens + body.reserved_tokens
        > budget.limit_tokens
    ):
        raise HTTPException(409, "作品预算不足，未预留额度，也不会发起调用")
    item = BudgetReservation(
        work_id=task.work_id,
        task_id=task.id,
        attempt_id=attempt_id,
        executor_id=task.executor_id,
        **body.model_dump(),
    )
    session.add(item)
    session.flush()
    budget.held_microunits += item.reserved_microunits
    budget.held_tokens += item.reserved_tokens
    budget.version += 1
    record(session, budget, "RESERVED", item)
    return item


def dispatch(session, task, reservation_id):
    """Persist BEFORE network I/O; True authorizes exactly this first dispatch only."""
    budget = lock_budget(session, task.work_id)
    item = session.get(BudgetReservation, reservation_id, populate_existing=True)
    if item is None or item.task_id != task.id:
        raise HTTPException(404, "调用预留不存在")
    require_active(task, session.get(TaskAttempt, item.attempt_id))
    if item.state != "RESERVED":
        return False
    if unknown_count(session, task.work_id):
        raise HTTPException(409, "作品存在待对账调用，不能发起新调用")
    check_task_limit(session, task, currency=item.currency)
    if (
        budget.spent_microunits + budget.held_microunits > budget.limit_microunits
        or budget.spent_tokens + budget.held_tokens > budget.limit_tokens
    ):
        raise HTTPException(409, "预算已降低或发生超支，不能发起调用")
    item.state, item.version = "DISPATCHED", item.version + 1
    budget.version += 1
    record(session, budget, "DISPATCHED", item)
    return True


def abandon_attempt(session, task, attempt_id):
    """Cancel/recovery never releases a call that may already have reached a provider."""
    items = list(
        session.scalars(
            select(BudgetReservation).where(
                BudgetReservation.attempt_id == attempt_id,
                BudgetReservation.state.in_(["RESERVED", "DISPATCHED"]),
            )
        )
    )
    if not items:
        return
    budget = lock_budget(session, task.work_id)
    for item in items:
        if item.state == "RESERVED":
            item.state = "RELEASED"
            budget.held_microunits -= item.reserved_microunits
            budget.held_tokens -= item.reserved_tokens
        else:
            item.state = "UNKNOWN"
        item.version += 1
        budget.version += 1
        record(session, budget, item.state, item)


def settle(
    session,
    task,
    reservation_id,
    body: SettlementInput,
    *,
    actor="runtime",
    expected_version=None,
):
    """May accept late usage evidence, but never modifies task/candidate success."""
    budget = lock_budget(session, task.work_id)
    item = session.get(BudgetReservation, reservation_id, populate_existing=True)
    if item is None or item.task_id != task.id:
        raise HTTPException(404, "调用预留不存在")
    payload = body.model_dump(exclude={"expected_version"})
    fingerprint = signature(payload)
    if item.state == "SETTLED":
        if item.settlement_hash != fingerprint:
            raise HTTPException(409, "已结算回执不能替换为不同内容")
        return item
    if expected_version is not None and (
        item.version != expected_version or item.state != "UNKNOWN"
    ):
        raise HTTPException(409, "对账状态已变化，请刷新；只有未知费用可以人工对账")
    if item.state not in {"DISPATCHED", "UNKNOWN"}:
        raise HTTPException(409, "未发起或已释放的调用不能结算")
    duplicate = session.scalar(
        select(BudgetReservation.id).where(
            BudgetReservation.executor_id == item.executor_id,
            BudgetReservation.receipt_key == body.receipt_key,
        )
    )
    if duplicate:
        raise HTTPException(409, "此执行者回执已用于其他调用，不能重复入账")
    budget.held_microunits -= item.reserved_microunits
    budget.held_tokens -= item.reserved_tokens
    budget.spent_microunits += body.actual_microunits
    budget.spent_tokens += body.input_tokens + body.output_tokens
    budget.version += 1
    item.actual_microunits, item.input_tokens, item.output_tokens = (
        body.actual_microunits,
        body.input_tokens,
        body.output_tokens,
    )
    item.receipt_key, item.settlement_hash = body.receipt_key, fingerprint
    item.state, item.version = "SETTLED", item.version + 1
    record(
        session,
        budget,
        "SETTLED",
        item,
        actor=actor,
        **payload,
        over_reservation=body.actual_microunits > item.reserved_microunits
        or body.input_tokens + body.output_tokens > item.reserved_tokens,
    )
    session.flush()  # Unique provider receipt also protects cross-work concurrency.
    return item
