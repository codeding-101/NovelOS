"""承诺账本：把「谁答应/威胁/约定了什么」记成可检查的对象。

长篇小说里最常见的逻辑硬伤不是设定冲突，而是**承诺被写忘**：约在听雨楼见、
三日内回山、扬言三日后来取命……在几十章之后没人记得。
这里用故事时间推算到期日，只要有一章的故事时间越过了期限而承诺还没兑现，就报警。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, Commitment, Novel
from app.timeutil import (
    day_to_story_time,
    parse_deadline,
    parse_story_time,
    story_time_era,
    story_time_to_day,
)

KINDS = ("APPOINTMENT", "DEADLINE", "THREAT", "PROMISE", "QUESTION")
STATUSES = ("OPEN", "FULFILLED", "OVERDUE", "ABANDONED")


def add_commitment(
    session: Session,
    novel: Novel,
    *,
    source_chapter: int | None,
    what: str,
    quote: str = "",
    who: str = "",
    counterpart: str = "",
    kind: str = "APPOINTMENT",
    deadline_text: str = "",
    story_time: str | None = None,
    origin: str = "EXTRACTOR",
    note: str = "",
) -> Commitment:
    """写入一条承诺；能算出到期时间就算出来，算不出来就留空（不猜）。"""
    if not story_time and source_chapter:
        chapter = session.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id, Chapter.chapter_number == source_chapter
            )
        )
        story_time = chapter.story_time if chapter else None

    days, raw = parse_deadline(deadline_text or quote)
    deadline_text = deadline_text or raw
    due_story_time: str | None = None
    due_sort: int | None = None
    base_day = story_time_to_day(story_time)
    if base_day is not None and days is not None:
        due_sort = base_day + days
        due_story_time = day_to_story_time(due_sort, era=story_time_era(story_time))

    commitment = Commitment(
        novel_id=novel.id,
        source_chapter=source_chapter,
        kind=kind if kind in KINDS else "APPOINTMENT",
        who=who,
        counterpart=counterpart,
        what=what[:500],
        quote=quote[:500],
        deadline_text=deadline_text[:64],
        due_story_time=due_story_time,
        due_sort=due_sort,
        status="OPEN",
        note=note,
        origin=origin,
    )
    session.add(commitment)
    session.flush()
    return commitment


def chapter_days(session: Session, novel_id: str) -> list[tuple[int, int | None]]:
    """[(章号, 绝对天数)]，按章号升序（故事时间解析不出来时为 None）。"""
    rows = list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel_id).order_by(Chapter.chapter_number)
        )
    )
    return [(chapter.chapter_number, story_time_to_day(chapter.story_time)) for chapter in rows]


def evaluate(
    session: Session, novel: Novel, *, update: bool = False
) -> list[dict[str, Any]]:
    """评估每条承诺的状态：未到期 / 已逾期 / 已兑现。

    update=True 时把逾期的 OPEN 承诺在库里标成 OVERDUE（只改 OPEN，不动作者的确认结果）。
    """
    rows = list(
        session.scalars(
            select(Commitment)
            .where(Commitment.novel_id == novel.id)
            .order_by(Commitment.source_chapter, Commitment.created_at)
        )
    )
    timeline = chapter_days(session, novel.id)
    latest_number = max((number for number, _ in timeline), default=0)
    latest_day = max((day for _, day in timeline if day is not None), default=None)

    results: list[dict[str, Any]] = []
    for item in rows:
        state = item.status
        breach_chapter: int | None = None
        breach_quote: str | None = None
        remaining: int | None = None
        if item.status in ("OPEN", "OVERDUE") and item.due_sort is not None:
            # 到期当天算「今天到期」，越过了才叫逾期：不把时间还没走完的一天判成违约
            crossing = [
                (number, day)
                for number, day in timeline
                if day is not None and day > item.due_sort and number > (item.source_chapter or 0)
            ]
            crossing.sort(key=lambda pair: pair[1])
            if crossing:
                state = "OVERDUE"
                breach_chapter = crossing[0][0]
                breach_quote = next(
                    (
                        chapter.story_time
                        for chapter in session.scalars(
                            select(Chapter).where(
                                Chapter.novel_id == novel.id,
                                Chapter.chapter_number == breach_chapter,
                            )
                        )
                    ),
                    None,
                )
            elif latest_day is not None:
                remaining = max(0, item.due_sort - latest_day)
                state = "OPEN"
        if update and state == "OVERDUE" and item.status == "OPEN":
            item.status = "OVERDUE"
        results.append(
            {
                "id": item.id,
                "kind": item.kind,
                "who": item.who,
                "counterpart": item.counterpart,
                "what": item.what,
                "quote": item.quote,
                "source_chapter": item.source_chapter,
                "deadline_text": item.deadline_text,
                "due_story_time": item.due_story_time,
                "status": state,
                "stored_status": item.status,
                "fulfilled_chapter": item.fulfilled_chapter,
                "days_remaining": remaining,
                "breach_chapter": breach_chapter,
                "frontier_chapter": latest_number,
                "evidence": _evidence(item, breach_chapter, breach_quote, latest_number),
            }
        )
    if update:
        session.flush()
    return results


def _evidence(
    item: Commitment, breach_chapter: int | None, breach_time: str | None, frontier: int
) -> list[dict[str, Any]]:
    evidence: list[dict[str, Any]] = [
        {
            "source_chapter": f"第{item.source_chapter}章" if item.source_chapter else "来源未知",
            "quote": item.quote or item.what,
            "detail": f"承诺：{item.what[:80]}（{item.deadline_text or '未写明期限'}）",
        }
    ]
    if breach_chapter:
        evidence.append(
            {
                "source_chapter": f"第{breach_chapter}章",
                "quote": breach_time,
                "detail": f"这一章的故事时间（{breach_time}）已经越过期限 {item.due_story_time}",
            }
        )
    return evidence


def overdue(session: Session, novel: Novel) -> list[dict[str, Any]]:
    return [item for item in evaluate(session, novel) if item["status"] == "OVERDUE"]


def open_items(session: Session, novel: Novel) -> list[dict[str, Any]]:
    return [item for item in evaluate(session, novel) if item["status"] in ("OPEN", "OVERDUE")]


def fulfil(
    session: Session, commitment: Commitment, *, chapter_number: int | None = None, note: str = ""
) -> Commitment:
    commitment.status = "FULFILLED"
    commitment.fulfilled_chapter = chapter_number
    if note:
        commitment.note = note
    session.flush()
    return commitment


def abandon(session: Session, commitment: Commitment, *, note: str = "") -> Commitment:
    commitment.status = "ABANDONED"
    if note:
        commitment.note = note
    session.flush()
    return commitment


def extract_deadline_hint(text: str) -> tuple[int | None, str]:
    """给抽取器用的薄封装：返回 (天数, 原文片段)。"""
    return parse_deadline(text)


def describe_day(value: str | None) -> str:
    year, month, day = parse_story_time(value)
    if year is None:
        return value or ""
    return f"{year}年{month or 1}月{day or 1}日"
