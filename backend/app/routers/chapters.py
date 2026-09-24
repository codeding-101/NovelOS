"""章节路由：CRUD、全文搜索、章节完成工作流、测试小说装载。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Chapter, Novel
from app.schemas import (
    ChapterCompletionReport,
    ChapterCreate,
    ChapterOut,
    ChapterSearchResult,
    ChapterSummaryOut,
    ChapterUpdate,
    ContinuityReportModel,
)
from app.services import chapter_service, continuity_service, search_service, workflow_service

novel_router = APIRouter(prefix="/api/novels", tags=["章节"])
chapter_router = APIRouter(prefix="/api/chapters", tags=["章节"])


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


@novel_router.get("/{novel_id}/chapters", response_model=list[ChapterSummaryOut])
def list_chapters(
    novel_id: str,
    response: Response,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    session: Session = Depends(get_session),
) -> list[Chapter]:
    _novel(session, novel_id)
    total = session.scalar(select(func.count(Chapter.id)).where(Chapter.novel_id == novel_id)) or 0
    response.headers["X-Total-Count"] = str(total)
    return list(
        session.scalars(
            select(Chapter)
            .where(Chapter.novel_id == novel_id)
            .order_by(Chapter.chapter_number)
            .offset(offset)
            .limit(limit)
        )
    )


@novel_router.post(
    "/{novel_id}/chapters", response_model=ChapterOut, status_code=status.HTTP_201_CREATED
)
def create_chapter(
    novel_id: str, payload: ChapterCreate, session: Session = Depends(get_session)
) -> Chapter:
    novel = _novel(session, novel_id)
    try:
        return chapter_service.create_chapter(session, novel, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@novel_router.get("/{novel_id}/search", response_model=ChapterSearchResult)
def search_chapters(
    novel_id: str,
    q: str = Query(min_length=1, description="检索词"),
    limit: int = Query(default=10, ge=1, le=50),
    session: Session = Depends(get_session),
) -> dict:
    _novel(session, novel_id)
    return search_service.search_chapters(session, novel_id, q, limit=limit)


@novel_router.post("/{novel_id}/seed")
def seed_test_novel(
    novel_id: str,
    reset: bool = Query(default=False, description="是否清空已有内容后重新装载"),
    session: Session = Depends(get_session),
) -> dict:
    """装载内置测试小说《剑起青云》：5 人物 / 10 事件 / 20 章 / 5 伏笔 / Canon 事实 / 2 处故意矛盾。"""
    from app.services import seed_service

    novel = _novel(session, novel_id)
    try:
        return seed_service.load_seed(session, novel, reset=reset)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@chapter_router.get("/{chapter_id}", response_model=ChapterOut)
def get_chapter(chapter_id: str, session: Session = Depends(get_session)) -> Chapter:
    return _chapter(session, chapter_id)


@chapter_router.put("/{chapter_id}", response_model=ChapterOut)
def save_chapter(
    chapter_id: str, payload: ChapterUpdate, session: Session = Depends(get_session)
) -> Chapter:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    try:
        chapter_service.update_chapter(session, novel, chapter, payload)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return chapter


@chapter_router.delete("/{chapter_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_chapter(chapter_id: str, session: Session = Depends(get_session)) -> None:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    chapter_service.delete_chapter(session, novel, chapter)


@chapter_router.get("/{chapter_id}/continuity", response_model=ContinuityReportModel | None)
def latest_continuity(chapter_id: str, session: Session = Depends(get_session)):
    """返回该章最近一次一致性检查报告（界面刷新后仍能看到上次结果）。"""
    chapter = _chapter(session, chapter_id)
    record = continuity_service.latest_report(session, chapter.novel_id, chapter_id)
    if record is None:
        return None
    return ContinuityReportModel(
        chapter_id=record.chapter_id,
        chapter_number=record.chapter_number,
        errors=record.errors,
        warnings=record.warnings,
        dropped_issues=record.dropped_issues,
        provider=record.provider,
        model=record.model,
        report_id=record.id,
        created_at=record.created_at,
    )


@chapter_router.post("/{chapter_id}/complete", response_model=ChapterCompletionReport)
def complete_chapter(
    chapter_id: str,
    provider: str | None = Query(default=None, description="AI 提供者：deepseek / offline"),
    narrative_pass: bool = Query(default=True, description="是否运行模型叙事审校通道"),
    session: Session = Depends(get_session),
) -> ChapterCompletionReport:
    chapter = _chapter(session, chapter_id)
    novel = session.get(Novel, chapter.novel_id)
    return workflow_service.complete_chapter(
        session, novel, chapter, provider_name=provider, narrative_pass=narrative_pass
    )
