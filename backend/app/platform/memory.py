from typing import Literal
from fastapi import APIRouter, Query, Request
from sqlalchemy import or_, select
from app.platform import memory_service as service
from app.platform import story_service
from app.platform.memory_protocol import DecisionInput, MemoryKind, ProposalInput
from app.platform.models import MemoryDecision, MemoryEntry, MemoryProposal
from app.platform.works import require_work

router = APIRouter(prefix="/api/v1/works/{work_id}/memory", tags=["memory"])


@router.get("/retrieve")
def retrieve(
    work_id: str,
    request: Request,
    q: str = Query(min_length=1, max_length=256),
    limit: int = Query(20, ge=1, le=50),
    at: int | None = Query(None, ge=0, le=1000000000000),
):
    from app.platform.memory_retrieval import retrieve as search

    with request.app.state.database.sessions() as session:
        return search(session, work_id, q, limit=limit, at=at)


@router.get("/timeline")
def timeline(
    work_id: str,
    request: Request,
    at: int = Query(ge=0, le=1000000000000),
    subject_key: str | None = Query(None, min_length=1, max_length=128),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = (
            story_service.at_query(work_id, at, subject_key).offset(offset).limit(limit)
        )
        return [service.data(session, row) for row in session.scalars(query)]


@router.get("")
def canonical(
    work_id: str,
    request: Request,
    q: str = Query("", max_length=256),
    kind: MemoryKind | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = (
            select(MemoryProposal)
            .join(MemoryEntry, MemoryEntry.proposal_id == MemoryProposal.id)
            .where(MemoryEntry.work_id == work_id)
        )
        if q:
            query = query.where(
                or_(
                    MemoryProposal.title.contains(q, autoescape=True),
                    MemoryProposal.content.contains(q, autoescape=True),
                    MemoryProposal.entry_key.contains(q, autoescape=True),
                )
            )
        if kind:
            query = query.where(MemoryProposal.kind == kind)
        return [
            service.data(session, row)
            for row in session.scalars(
                query.order_by(MemoryProposal.entry_key).offset(offset).limit(limit)
            )
        ]


@router.get("/proposals")
def proposals(
    work_id: str,
    request: Request,
    status: Literal["pending", "approved", "rejected", "all"] = "pending",
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = (
            select(MemoryProposal)
            .outerjoin(MemoryDecision, MemoryDecision.proposal_id == MemoryProposal.id)
            .where(MemoryProposal.work_id == work_id)
        )
        if status == "pending":
            query = query.where(MemoryDecision.proposal_id.is_(None))
        elif status != "all":
            query = query.where(MemoryDecision.outcome == status)
        return [
            service.data(session, row, include_content=False)
            for row in session.scalars(
                query.order_by(MemoryProposal.created_at.desc(), MemoryProposal.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.post("/proposals", status_code=201)
def create(work_id: str, body: ProposalInput, request: Request):
    return {"id": service.create(request.app.state.database, work_id, body)}


@router.get("/proposals/{proposal_id}")
def detail(work_id: str, proposal_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        return service.data(
            session, service.require_proposal(session, work_id, proposal_id)
        )


@router.post("/proposals/{proposal_id}/decision")
def decide(work_id: str, proposal_id: str, body: DecisionInput, request: Request):
    return service.decide(request.app.state.database, work_id, proposal_id, body)


@router.get("/history")
def history(
    work_id: str,
    request: Request,
    entry_key: str = Query(min_length=1, max_length=128),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = (
            select(MemoryProposal)
            .join(MemoryDecision, MemoryDecision.proposal_id == MemoryProposal.id)
            .where(
                MemoryProposal.work_id == work_id,
                MemoryProposal.entry_key == entry_key,
                MemoryDecision.outcome == "approved",
            )
            .order_by(MemoryDecision.version.desc())
            .offset(offset)
            .limit(limit)
        )
        return [service.data(session, row) for row in session.scalars(query)]
