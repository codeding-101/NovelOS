"""小说服务：项目统计与重算。"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Character,
    ContinuityReport,
    Event,
    ExtractionItem,
    ExtractionRun,
    Foreshadowing,
    ForeshadowStatus,
    ItemReviewStatus,
    Novel,
    TimelineEntry,
)

_SLUG_BAD_CHARS = re.compile(r'[\\/:*?"<>|\s]+')


def make_slug(title: str, novel_id: str) -> str:
    """用标题生成可读的目录名；纯符号标题回退为 novel-<id>。"""
    cleaned = _SLUG_BAD_CHARS.sub("_", (title or "").strip()).strip("_")
    cleaned = cleaned[:40]
    return cleaned or f"novel-{novel_id[-6:]}"


def unique_slug(session: Session, base: str) -> str:
    slug, index = base, 2
    while session.scalar(select(Novel.id).where(Novel.slug == slug)) is not None:
        slug = f"{base}-{index}"
        index += 1
    return slug


def has_book_setting(novel: Novel) -> bool:
    """作者有没有写过简介／世界观／大纲（书名与进度不算）。"""
    return any(
        (value or "").strip() for value in (novel.synopsis, novel.worldview, novel.outline)
    )


def novel_context(novel: Novel) -> str:
    """把这本书的设定与大纲渲染成一段可放进提示词的上下文。

    规划、写作、碎片成文都要用它：作者写在这里的东西必须真的被模型读到，
    否则「重新选材、重编大纲」就只是存在数据库里的文字。
    """
    lines: list[str] = []
    header = novel.title or ""
    meta = [item for item in (novel.genre, novel.author) if item]
    if meta:
        header = f"{header}（{'，'.join(meta)}）" if header else "，".join(meta)
    if header:
        lines.append(f"书名：{header}")
    if novel.word_count or novel.target_word_count:
        lines.append(f"进度：{novel.word_count} / {novel.target_word_count} 字")
    for label, value in (
        ("简介", novel.synopsis),
        ("世界观", novel.worldview),
        ("全书大纲", novel.outline),
    ):
        text = (value or "").strip()
        if text:
            lines.append(f"{label}：\n{text}")
    if not lines:
        return ""
    return "【本书设定（写作与规划必须服从）】\n" + "\n".join(lines)


def recount(session: Session, novel: Novel) -> Novel:
    word_count = session.scalar(
        select(func.coalesce(func.sum(Chapter.word_count), 0)).where(Chapter.novel_id == novel.id)
    )
    chapter_count = session.scalar(
        select(func.count(Chapter.id)).where(Chapter.novel_id == novel.id)
    )
    novel.word_count = int(word_count or 0)
    novel.chapter_count = int(chapter_count or 0)
    session.flush()
    return novel


def build_stats(session: Session, novel: Novel) -> dict[str, Any]:
    proposed = session.scalar(
        select(func.count(CanonFact.id)).where(
            CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.PROPOSED
        )
    )
    canon = session.scalar(
        select(func.count(CanonFact.id)).where(
            CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.CANON
        )
    )
    pending_items = session.scalar(
        select(func.count(ExtractionItem.id))
        .join(ExtractionRun, ExtractionRun.id == ExtractionItem.run_id)
        .where(
            ExtractionRun.novel_id == novel.id,
            ExtractionItem.review_status == ItemReviewStatus.PENDING,
        )
    )
    latest_report = session.scalar(
        select(ContinuityReport)
        .where(ContinuityReport.novel_id == novel.id)
        .order_by(ContinuityReport.created_at.desc())
    )
    return {
        "novel_id": novel.id,
        "word_count": novel.word_count,
        "chapter_count": novel.chapter_count,
        "target_word_count": novel.target_word_count,
        "progress": round(novel.word_count / novel.target_word_count, 4)
        if novel.target_word_count
        else 0.0,
        "character_count": int(
            session.scalar(
                select(func.count(Character.id)).where(Character.novel_id == novel.id)
            )
            or 0
        ),
        "event_count": int(
            session.scalar(select(func.count(Event.id)).where(Event.novel_id == novel.id)) or 0
        ),
        "canon_fact_count": int(canon or 0),
        "proposed_fact_count": int(proposed or 0),
        "foreshadowing_open_count": int(
            session.scalar(
                select(func.count(Foreshadowing.id)).where(
                    Foreshadowing.novel_id == novel.id,
                    Foreshadowing.status != ForeshadowStatus.RESOLVED,
                )
            )
            or 0
        ),
        "timeline_entry_count": int(
            session.scalar(
                select(func.count(TimelineEntry.id)).where(TimelineEntry.novel_id == novel.id)
            )
            or 0
        ),
        "pending_review_items": int(pending_items or 0),
        "latest_continuity_errors": len(latest_report.errors or []) if latest_report else 0,
        "latest_continuity_warnings": len(latest_report.warnings or []) if latest_report else 0,
    }
