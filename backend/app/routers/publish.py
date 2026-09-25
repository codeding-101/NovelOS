"""发布前检查与导出（面向番茄免费小说的按章发布）。"""

from __future__ import annotations

import re
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Chapter, Novel
from app.schemas import PublishCheckOut, PublishCheckRequest, StructureViewOut
from app.services import craft_rules, publish_service, structure_service, style_service

novel_router = APIRouter(prefix="/api/novels", tags=["发布"])
chapter_router = APIRouter(prefix="/api/chapters", tags=["发布"])

#: 写作规则清单不挂在某一本书下面
rules_router = APIRouter(prefix="/api", tags=["发布"])


@rules_router.get("/craft-rules")
def craft_rules_catalog() -> list[dict[str, str]]:
    """列出检查所依据的写作规则与出处（平台作家课堂原文）。"""
    return craft_rules.catalog()


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


@chapter_router.post("/{chapter_id}/publish-check", response_model=PublishCheckOut)
def publish_check_chapter(chapter_id: str, session: Session = Depends(get_session)) -> dict:
    """按平台口径检查一章：字数、审核风险词、格式、开篇、章末钩子，以及四条低质规则。"""
    chapter = session.get(Chapter, chapter_id)
    if chapter is None:
        raise HTTPException(status_code=404, detail="章节不存在")
    return publish_service.check_chapter(session, chapter)


@novel_router.post("/{novel_id}/publish-check", response_model=PublishCheckOut)
def publish_check_text(
    novel_id: str, payload: PublishCheckRequest, session: Session = Depends(get_session)
) -> dict:
    """对一段草稿做发布前检查（不落库）。chapter_number 给了就按那一章查，否则用 text。"""
    novel = _novel(session, novel_id)
    chapter: Chapter | None = None
    if payload.chapter_number:
        chapter = session.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_number == payload.chapter_number,
            )
        )
    text = payload.text or (chapter.content if chapter else "")
    if not (text or "").strip():
        raise HTTPException(status_code=400, detail="请提供正文（text 或 chapter_number）")
    title = (
        f"第{chapter.chapter_number}章 {chapter.title or ''}".strip()
        if chapter is not None
        else (payload.title or "")
    )
    target = (
        (payload.target_words_min, payload.target_words_max)
        if payload.target_words_min and payload.target_words_max
        else publish_service.WORDS_PER_CHAPTER
    )
    report = publish_service.check_text(
        text,
        novel_id=novel.id,
        chapter_number=chapter.chapter_number if chapter else payload.chapter_number,
        title=title,
        target_words=target,
        style_profile=style_service.default_profile(session, novel.id),
        voice_profile=style_service.default_voice_profile(session, novel.id),
    )
    payload_out = report.to_dict()
    if chapter is not None:
        payload_out["chapter_id"] = chapter.id
    payload_out["novel_title"] = novel.title
    return payload_out


@novel_router.get("/{novel_id}/structure", response_model=StructureViewOut)
def structure_view(novel_id: str, session: Session = Depends(get_session)) -> dict:
    """全书结构视图：逐章节奏信号、连续弱区、每 5 章的节奏窗口。

    面向平台点名的「结构失常」「大段内容未能推动情节发展」，以及读者会在哪一段掉队。
    """
    novel = _novel(session, novel_id)
    return structure_service.structure_view(session, novel)


@novel_router.get("/{novel_id}/export")
def export_novel(
    novel_id: str,
    fmt: str = Query(default="txt", pattern="^(txt|md)$"),
    from_chapter: int | None = Query(default=None, ge=1),
    to_chapter: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
) -> Response:
    """导出整本或一段：txt 是去掉了 Markdown 的纯文本，可以直接粘到平台后台。"""
    novel = _novel(session, novel_id)
    text = publish_service.export_text(
        session,
        novel,
        from_chapter=from_chapter,
        to_chapter=to_chapter,
        strip_markdown=fmt == "txt",
    )
    if not text.strip():
        raise HTTPException(status_code=404, detail="这个范围里没有正文")
    suffix = "txt" if fmt == "txt" else "md"
    # 中文书名会生成中文目录名，而 HTTP 头只能是 latin-1：
    # ASCII 回退（没有可用字符就退回小说 id）+ RFC 5987 的 UTF-8 形式，两种客户端都认
    ascii_core = re.sub(r"[^A-Za-z0-9._-]+", "_", novel.slug or "").strip("_-.")
    ascii_name = ascii_core or novel.id
    filename = f"{novel.slug or novel.id}.{suffix}"
    media = "text/plain" if fmt == "txt" else "text/markdown"
    disposition = (
        f'attachment; filename="{ascii_name}.{suffix}"; '
        f"filename*=UTF-8''{quote(filename)}"
    )
    return Response(
        content=text,
        media_type=f"{media}; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )
