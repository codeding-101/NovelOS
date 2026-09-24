"""AI 路由：抽取、一致性检查、记忆问答、章节写作、审校与工具清单。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app import __version__
from app.ai.base import AIError
from app.ai.factory import describe_providers
from app.ai.tools import AIToolKit
from app.database import get_session
from app.models import Chapter, ExtractionItem, ExtractionRun, Novel
from app.schemas import (
    ApplyRunRequest,
    ApplyRunResult,
    AskRequest,
    AskResponse,
    ContinuityReportModel,
    ExtractionRunDetail,
    ItemReviewRequest,
    ToolSpec,
    WriteChapterRequest,
    WriteChapterResponse,
)
from app.services import continuity_service, extraction_service, search_service

router = APIRouter(prefix="/api", tags=["AI"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


def _chapter(session: Session, chapter_id: str) -> Chapter:
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    return chapter


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "database": "sqlite",
        "search_engine": "fts5-trigram" if search_service.fts_available(session) else "like",
        "default_provider": describe_providers()[0]["name"],
        "providers": describe_providers(),
    }


@router.get("/ai/providers")
def providers() -> list[dict]:
    return describe_providers()


@router.get("/ai/tools", response_model=list[ToolSpec])
def list_tools() -> list[ToolSpec]:
    """AITool 清单与参数 Schema（模型可直接阅读，未来可切换为原生 function calling）。"""
    return [
        ToolSpec(name=item.name, description=item.description, parameters=item.parameters)
        for item in AIToolKit.definitions()
    ]


@router.post("/ai/tools/{tool_name}")
def call_tool(
    tool_name: str,
    arguments: dict | None = None,
    novel_id: str = Query(description="工具作用的小说"),
    session: Session = Depends(get_session),
) -> dict:
    novel = _novel(session, novel_id)
    return AIToolKit(session, novel).call(tool_name, arguments or {})


# --------------------------------------------------------------------------- 抽取
@router.post("/chapters/{chapter_id}/extract", response_model=ExtractionRunDetail)
def extract_chapter(
    chapter_id: str,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> ExtractionRunDetail:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    try:
        result, run, warnings, _, _ = extraction_service.run_extraction(
            session, novel, chapter, provider_name=provider, persist=True
        )
    except AIError as exc:
        raise HTTPException(status_code=502, detail=f"抽取失败：{exc}") from exc
    if run is None:
        raise HTTPException(status_code=500, detail="抽取未持久化")
    session.refresh(run)
    return extraction_service.run_to_detail(run)


# --------------------------------------------------------------------------- 一致性
@router.post("/chapters/{chapter_id}/continuity", response_model=ContinuityReportModel)
def check_continuity(
    chapter_id: str,
    provider: str | None = Query(default=None),
    narrative_pass: bool = Query(default=True),
    session: Session = Depends(get_session),
) -> ContinuityReportModel:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    report, _ = continuity_service.run_check(
        session, novel, chapter, provider_name=provider, narrative_pass=narrative_pass
    )
    return report


# --------------------------------------------------------------------------- 记忆问答
@router.post("/novels/{novel_id}/ai/ask", response_model=AskResponse)
def ask(
    novel_id: str,
    payload: AskRequest,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> AskResponse:
    novel = _novel(session, novel_id)
    agents = extraction_service.build_agents(provider or payload.provider)
    memory = agents["memory"]
    if payload.mode == "agent":
        response = memory.ask_agent(
            session, novel, payload.question, max_evidence=payload.max_evidence, persist=payload.persist
        )
    else:
        response = memory.ask(
            session, novel, payload.question, max_evidence=payload.max_evidence, persist=payload.persist
        )
    response.warnings.extend(agents["warnings"])
    return response


# --------------------------------------------------------------------------- 写作
@router.post("/novels/{novel_id}/ai/write-chapter", response_model=WriteChapterResponse)
def write_chapter(
    novel_id: str,
    payload: WriteChapterRequest,
    provider: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> WriteChapterResponse:
    novel = _novel(session, novel_id)
    agents = extraction_service.build_agents(provider or payload.provider)
    response = agents["writer"].write(session, novel, payload)
    response.warnings.extend(agents["warnings"])
    return response


# --------------------------------------------------------------------------- 审校
@router.get("/novels/{novel_id}/extraction-runs", response_model=list[ExtractionRunDetail])
def list_runs(
    novel_id: str,
    chapter_id: str | None = Query(default=None),
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> list[ExtractionRunDetail]:
    _novel(session, novel_id)
    runs = extraction_service.list_runs(session, novel_id, chapter_id, limit)
    return [extraction_service.run_to_detail(run) for run in runs]


@router.get("/extraction-runs/{run_id}", response_model=ExtractionRunDetail)
def get_run(run_id: str, session: Session = Depends(get_session)) -> ExtractionRunDetail:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="抽取任务不存在")
    return extraction_service.run_to_detail(run)


@router.post("/extraction-items/{item_id}/review")
def review_item(
    item_id: str, payload: ItemReviewRequest, session: Session = Depends(get_session)
) -> dict:
    item = session.get(ExtractionItem, item_id)
    if item is None:
        raise HTTPException(status_code=404, detail="抽取项不存在")
    item = extraction_service.review_item(session, item, payload.review_status)
    return {"item_id": item.id, "kind": item.kind, "review_status": item.review_status}


@router.post("/extraction-runs/{run_id}/apply", response_model=ApplyRunResult)
def apply_run(
    run_id: str, payload: ApplyRunRequest, session: Session = Depends(get_session)
) -> ApplyRunResult:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="抽取任务不存在")
    novel = session.get(Novel, run.novel_id)
    return extraction_service.apply_run(
        session, novel, run, kinds=payload.kinds, accept_pending=payload.accept_pending
    )
