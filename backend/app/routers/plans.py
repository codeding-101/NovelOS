"""章节计划接口：PlannerAgent 生成、作者维护、一键转成写作任务。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import ChapterPlan, Novel
from app.schemas import (
    ChapterPlanCreate,
    ChapterPlanOut,
    PlanGenerateRequest,
    PlanGenerateResponse,
    WriteChapterResponse,
)
from app.services import extraction_service, plan_service


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


router = APIRouter(prefix="/api/novels", tags=["章节规划"])
item_router = APIRouter(prefix="/api/plans", tags=["章节规划"])


@router.get("/{novel_id}/plans", response_model=list[ChapterPlanOut])
def list_plans(
    novel_id: str,
    status: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[ChapterPlan]:
    novel = _novel(session, novel_id)
    plan_service.sync_status(session, novel)
    return plan_service.list_plans(session, novel_id, status=status)


@router.post("/{novel_id}/plans/generate", response_model=PlanGenerateResponse)
def generate_plans(
    novel_id: str, payload: PlanGenerateRequest, session: Session = Depends(get_session)
) -> PlanGenerateResponse:
    """让规划 Agent 给出接下来几章的计划（含要推进的伏笔）。"""
    novel = _novel(session, novel_id)
    result = plan_service.generate_plans(session, novel, payload)
    return PlanGenerateResponse(
        plans=[ChapterPlanOut.model_validate(plan) for plan in result["plans"]],
        provider=result["provider"],
        model=result["model"],
        warnings=result["warnings"],
        context_summary=result["context_summary"],
    )


@router.post("/{novel_id}/plans", response_model=ChapterPlanOut, status_code=201)
def upsert_plan(
    novel_id: str, payload: ChapterPlanCreate, session: Session = Depends(get_session)
) -> ChapterPlan:
    novel = _novel(session, novel_id)
    return plan_service.upsert_plan(session, novel, payload)


@item_router.delete("/{plan_id}", status_code=204)
def delete_plan(plan_id: str, session: Session = Depends(get_session)) -> None:
    plan = session.get(ChapterPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    plan_service.delete_plan(session, plan)


@item_router.post("/{plan_id}/write", response_model=WriteChapterResponse)
def write_from_plan(
    plan_id: str,
    provider: str | None = Query(default=None),
    target_words: int = Query(default=1500, ge=300, le=8000),
    save: bool = Query(default=False, description="是否直接存成草稿章节"),
    session: Session = Depends(get_session),
) -> WriteChapterResponse:
    """按计划写作：计划里的必须/禁止/人物直接变成写作约束。"""
    plan = session.get(ChapterPlan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="计划不存在")
    novel = session.get(Novel, plan.novel_id)
    request = plan_service.plan_to_write_request(plan, target_words=target_words)
    request.save = save
    agents = extraction_service.build_agents(provider or request.provider)
    response = agents["writer"].write(session, novel, request)
    response.warnings.extend(agents["warnings"])
    if save and response.saved_chapter_id:
        plan.status = "WRITTEN"
        session.flush()
    return response
