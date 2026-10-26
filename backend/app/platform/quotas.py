from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import select

from app.platform import quota_service as service
from app.platform.budgets import reservation_data
from app.platform.models import (
    AuditEvent,
    BudgetReservation,
    MonthlyQuota,
    QuotaBooking,
    Work,
)
from app.platform.task_protocol import Contract
from app.platform.security import utc

router = APIRouter(prefix="/api/v1/quotas", tags=["quotas"])


class QuotaCreate(Contract):
    name: str = Field(min_length=1, max_length=128)
    timezone: Literal["UTC", "Asia/Shanghai"]
    limit_tokens: int = Field(strict=True, ge=0, le=10**12)
    request_key: str = Field(min_length=1, max_length=128)


class QuotaUpdate(Contract):
    expected_version: int = Field(strict=True, ge=1)
    limit_tokens: int = Field(strict=True, ge=0, le=10**12)


class QuotaBind(Contract):
    expected_version: int = Field(strict=True, ge=1)
    executor_id: str = Field(min_length=1, max_length=32)


@router.get("")
def listing(
    request: Request, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)
):
    with request.app.state.database.sessions() as session:
        return [
            service.summary(session, quota)
            for quota in session.scalars(
                select(MonthlyQuota)
                .order_by(MonthlyQuota.created_at, MonthlyQuota.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.post("", status_code=201)
def create(body: QuotaCreate, request: Request):
    return {"id": service.create(request.app.state.database, body)}


@router.get("/{quota_id}")
def detail(quota_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        return service.summary(session, service.require_quota(session, quota_id))


@router.put("/{quota_id}")
def configure(quota_id: str, body: QuotaUpdate, request: Request):
    return service.configure(request.app.state.database, quota_id, body)


@router.post("/{quota_id}/bindings")
def bind(quota_id: str, body: QuotaBind, request: Request):
    return service.bind(request.app.state.database, quota_id, body)


@router.get("/{quota_id}/calls")
def calls(
    quota_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        service.require_quota(session, quota_id)
        rows = session.execute(
            select(BudgetReservation, QuotaBooking.period)
            .join(QuotaBooking, QuotaBooking.reservation_id == BudgetReservation.id)
            .where(QuotaBooking.quota_id == quota_id)
            .order_by(BudgetReservation.created_at.desc(), BudgetReservation.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            {
                **reservation_data(row),
                "work_id": row.work_id,
                "work_title": session.get(Work, row.work_id).title,
                "period": period,
            }
            for row, period in rows
        ]


@router.get("/{quota_id}/events")
def events(
    quota_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        service.require_quota(session, quota_id)
        return [
            {
                "id": row.id,
                "action": row.action,
                "details": row.details,
                "created_at": utc(row.created_at),
            }
            for row in session.scalars(
                select(AuditEvent)
                .where(
                    AuditEvent.target_id == quota_id, AuditEvent.action.like("quota.%")
                )
                .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
                .offset(offset)
                .limit(limit)
            )
        ]
