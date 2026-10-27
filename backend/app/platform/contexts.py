from fastapi import APIRouter, Query, Request
from sqlalchemy import select
from app.platform import context_service as service
from app.platform.context_protocol import SnapshotInput
from app.platform.models import ContextSnapshot
from app.platform.works import require_work

router = APIRouter(prefix="/api/v1/works/{work_id}/contexts", tags=["contexts"])


@router.post("", status_code=201)
def create(work_id: str, body: SnapshotInput, request: Request):
    return {"id": service.create(request.app.state.database, work_id, body)}


@router.get("")
def listing(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            service.data(session, row)
            for row in session.scalars(
                select(ContextSnapshot)
                .where(ContextSnapshot.work_id == work_id)
                .order_by(ContextSnapshot.created_at.desc(), ContextSnapshot.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.get("/{snapshot_id}")
def detail(work_id: str, snapshot_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        return service.data(
            session,
            service.require_snapshot(session, work_id, snapshot_id),
            detail=True,
        )


@router.post("/{snapshot_id}/check")
def check(work_id: str, snapshot_id: str, request: Request):
    return service.check(request.app.state.database, work_id, snapshot_id)
