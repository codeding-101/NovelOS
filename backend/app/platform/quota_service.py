"""Local calendar quotas. No supplier balance fetch or network capability."""

from datetime import timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import case, func, select, update
from sqlalchemy.exc import IntegrityError

from app.platform.models import (
    AuditEvent,
    BudgetReservation,
    MonthlyQuota,
    QuotaBinding,
    QuotaBooking,
    now,
)
from app.platform.security import utc


class QuotaExceeded(HTTPException):
    def __init__(self, message="月度配额不足，请核对共享用量及占用"):
        super().__init__(409, message)


def period_for(quota, instant=None):
    zone = (
        timezone(timedelta(hours=8))
        if quota.timezone == "Asia/Shanghai"
        else timezone.utc
    )
    local = utc(instant or now()).astimezone(zone)
    return local.strftime("%Y-%m")


def require_quota(session, quota_id, *, lock=False):
    if lock:
        session.execute(
            update(MonthlyQuota)
            .where(MonthlyQuota.id == quota_id)
            .values(version=MonthlyQuota.version)
            .execution_options(synchronize_session=False)
        )
    quota = session.get(MonthlyQuota, quota_id, populate_existing=lock)
    if quota is None:
        raise HTTPException(404, "月度配额不存在")
    return quota


def for_executor(session, executor_id, *, lock=False):
    if lock:
        # The executor lock serializes the first binding with its first reservation.
        from app.platform.executor_service import lock_executor

        lock_executor(session, executor_id)
    binding = session.get(QuotaBinding, executor_id)
    return require_quota(session, binding.quota_id, lock=lock) if binding else None


def summary(session, quota, period=None):
    period = period or period_for(quota)
    held = BudgetReservation.state.in_(["RESERVED", "DISPATCHED", "UNKNOWN"])
    settled = (BudgetReservation.state == "SETTLED") & (QuotaBooking.period == period)
    spent, occupied, unknown = session.execute(
        select(
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
                func.sum(case((held, BudgetReservation.reserved_tokens), else_=0)), 0
            ),
            func.coalesce(
                func.sum(case((BudgetReservation.state == "UNKNOWN", 1), else_=0)), 0
            ),
        )
        .select_from(QuotaBooking)
        .join(BudgetReservation, BudgetReservation.id == QuotaBooking.reservation_id)
        .where(QuotaBooking.quota_id == quota.id)
    ).one()
    return {
        "id": quota.id,
        "name": quota.name,
        "timezone": quota.timezone,
        "version": quota.version,
        "limit_tokens": quota.limit_tokens,
        "period": period,
        "spent_tokens": spent,
        "held_tokens": occupied,
        "remaining_tokens": max(0, quota.limit_tokens - spent - occupied),
        "unknown_count": unknown,
        "over_limit": spent + occupied > quota.limit_tokens,
        "warning": (spent + occupied) * 100 >= quota.limit_tokens * 80,
        "executor_ids": list(
            session.scalars(
                select(QuotaBinding.executor_id)
                .where(QuotaBinding.quota_id == quota.id)
                .order_by(QuotaBinding.executor_id)
            )
        ),
        "meter": "input_plus_output_tokens",
        "external_execution": False,
    }


def check(session, quota, tokens=0):
    if quota is None:
        return
    value = summary(session, quota)
    if value["unknown_count"]:
        raise QuotaExceeded("共享月度配额存在未知用量，须先在对应作品预算中心对账")
    if value["spent_tokens"] + value["held_tokens"] + tokens > quota.limit_tokens:
        raise QuotaExceeded()


def check_next_call(session, task):
    from app.platform.models import TaskRoutingDecision

    quota = for_executor(session, task.executor_id, lock=True)
    if quota:
        decision = session.get(TaskRoutingDecision, task.id)
        if decision is None:
            raise QuotaExceeded("任务缺少调用计划，请重新创建")
        candidate = next(
            row
            for row in decision.snapshot["candidates"]
            if row["id"] == task.executor_id
        )
        check(session, quota, candidate["estimated_tokens"])


def book(session, quota, item):
    if quota:
        session.add(
            QuotaBooking(
                reservation_id=item.id, quota_id=quota.id, period=period_for(quota)
            )
        )
        quota.version += 1


def check_dispatch(session, quota, item):
    if quota:
        booking = session.get(QuotaBooking, item.id)
        if booking is None or booking.quota_id != quota.id:
            raise QuotaExceeded("调用缺少月度归属，不能发送")
        if booking.period != period_for(quota):
            raise QuotaExceeded("预留所属月份已结束，请暂停释放未发送预留后恢复")
        check(session, quota)


def audit(session, quota, action, details):
    session.add(
        AuditEvent(
            actor="owner", action="quota." + action, target_id=quota.id, details=details
        )
    )


def create(db, body):
    from app.platform.budget_service import signature

    signature_value = signature(body.model_dump())

    def existing(session):
        value = session.scalar(
            select(MonthlyQuota).where(MonthlyQuota.request_key == body.request_key)
        )
        if value and value.request_hash != signature_value:
            raise HTTPException(409, "同一创建标识不能修改配额配置")
        return value

    try:
        with db.sessions.begin() as session:
            previous = existing(session)
            if previous:
                return previous.id
            quota = MonthlyQuota(**body.model_dump(), request_hash=signature_value)
            session.add(quota)
            session.flush()
            audit(session, quota, "created", body.model_dump(exclude={"request_key"}))
            return quota.id
    except IntegrityError:
        with db.sessions() as session:
            previous = existing(session)
            if previous:
                return previous.id
        raise


def configure(db, quota_id, body):
    with db.sessions.begin() as session:
        quota = require_quota(session, quota_id, lock=True)
        if quota.version != body.expected_version:
            raise HTTPException(
                409, "配额版本已变化，请载入最新配额后核对；当前输入已保留"
            )
        before = quota.limit_tokens
        quota.limit_tokens = body.limit_tokens
        quota.version += 1
        audit(
            session,
            quota,
            "configured",
            {"before": before, "after": quota.limit_tokens},
        )
        return summary(session, quota)


def bind(db, quota_id, body):
    from app.platform.executor_service import lock_executor, model_api

    with db.sessions.begin() as session:
        executor = lock_executor(session, body.executor_id)
        quota = require_quota(session, quota_id, lock=True)
        previous = session.get(QuotaBinding, executor.id)
        if previous:
            if previous.quota_id == quota.id:
                return summary(session, quota)
            raise HTTPException(409, "执行者已有固定配额归属，不可改绑")
        if quota.version != body.expected_version:
            raise HTTPException(409, "配额版本已变化，请载入最新配额后重试")
        if not model_api(executor):
            raise HTTPException(409, "仅可绑定已登记模型配置的 API 执行者")
        if session.scalar(
            select(BudgetReservation.id)
            .where(BudgetReservation.executor_id == executor.id)
            .limit(1)
        ):
            raise HTTPException(
                409, "执行者已有调用账目，不能补造月度归属；请使用尚未调用的执行者"
            )
        session.add(QuotaBinding(executor_id=executor.id, quota_id=quota.id))
        quota.version += 1
        audit(session, quota, "bound", {"executor_id": executor.id})
        session.flush()
        return summary(session, quota)
