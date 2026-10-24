from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.platform import budget_service as service
from app.platform.budget_protocol import BudgetInput, ReconcileInput
from app.platform.models import BudgetEvent, BudgetReservation, WorkBudget
from app.platform.security import utc
from app.platform.task_service import locked_task
from app.platform.works import require_work

router = APIRouter(prefix="/api/v1/works/{work_id}/budget", tags=["budgets"])


def reservation_data(item):
    return {
        **{
            key: getattr(item, key)
            for key in (
                "id",
                "task_id",
                "attempt_id",
                "executor_id",
                "operation_key",
                "currency",
                "state",
                "version",
                "reserved_microunits",
                "reserved_tokens",
                "actual_microunits",
                "input_tokens",
                "output_tokens",
                "receipt_key",
            )
        },
        "created_at": utc(item.created_at),
    }


@router.get("")
def summary(work_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        budget = session.get(WorkBudget, work_id)
        if budget is None:
            return {
                "configured": False,
                "version": 0,
                "external_execution": False,
                "scope": "work_lifetime",
            }
        unknown = service.unknown_count(session, work_id)
        money = (
            budget.limit_microunits - budget.spent_microunits - budget.held_microunits
        )
        tokens = budget.limit_tokens - budget.spent_tokens - budget.held_tokens
        return {
            "configured": True,
            "scope": "work_lifetime",
            "external_execution": False,
            **{
                key: getattr(budget, key)
                for key in (
                    "currency",
                    "version",
                    "limit_microunits",
                    "limit_tokens",
                    "spent_microunits",
                    "held_microunits",
                    "spent_tokens",
                    "held_tokens",
                )
            },
            "available_microunits": max(0, money),
            "available_tokens": max(0, tokens),
            "over_limit_microunits": max(0, -money),
            "over_limit_tokens": max(0, -tokens),
            "unknown_count": unknown,
        }


@router.put("")
def configure(work_id: str, body: BudgetInput, request: Request):
    return {"version": service.configure(request.app.state.database, work_id, body)}


@router.get("/reservations")
def reservations(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    unknown_only: bool = False,
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = select(BudgetReservation).where(BudgetReservation.work_id == work_id)
        if unknown_only:
            query = query.where(BudgetReservation.state == "UNKNOWN")
        return [
            reservation_data(item)
            for item in session.scalars(
                query.order_by(
                    BudgetReservation.created_at.desc(), BudgetReservation.id
                )
                .offset(offset)
                .limit(limit)
            )
        ]


@router.get("/events")
def events(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            {
                "id": item.id,
                "kind": item.kind,
                "actor": item.actor,
                "reservation_id": item.reservation_id,
                "details": item.details,
                "created_at": utc(item.created_at),
            }
            for item in session.scalars(
                select(BudgetEvent)
                .where(BudgetEvent.work_id == work_id)
                .order_by(BudgetEvent.created_at.desc(), BudgetEvent.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.post("/reservations/{reservation_id}/reconcile")
def reconcile(
    work_id: str, reservation_id: str, body: ReconcileInput, request: Request
):
    try:
        with request.app.state.database.sessions.begin() as session:
            item = session.get(BudgetReservation, reservation_id)
            if item is None or item.work_id != work_id:
                raise HTTPException(404, "调用预留不存在")
            task = locked_task(session, item.task_id)
            result = service.settle(
                session,
                task,
                reservation_id,
                body,
                actor="owner",
                expected_version=body.expected_version,
            )
            return reservation_data(result)
    except IntegrityError:
        raise HTTPException(409, "回执已用于其他调用，未重复入账") from None
