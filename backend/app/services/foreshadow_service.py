"""伏笔调度：算「伏笔欠账」，并给出回收窗口建议。

欠账 = 伏笔自最近一次强化/首现以来已过多少章仍未推进；越久越该在近几章回应。
给作者的建议都带证据（首现章、最近强化章、相关人物），不做事后诸葛式的空口提示。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Chapter, ForeshadowStatus, Foreshadowing, Novel

#: 超过这么多章没回应就算欠账
DEBT_GAP = 8
#: 回收建议的时间窗（接下来多少章之内）
DEFAULT_HORIZON = 5


def frontier_chapter(session: Session, novel_id: str) -> int:
    return int(
        session.scalar(select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel_id))
        or 0
    )


def debt(session: Session, novel: Novel, *, gap_threshold: int = DEBT_GAP) -> list[dict[str, Any]]:
    frontier = frontier_chapter(session, novel.id)
    rows = list(session.scalars(select(Foreshadowing).where(Foreshadowing.novel_id == novel.id)))
    items: list[dict[str, Any]] = []
    for item in rows:
        if item.status == ForeshadowStatus.RESOLVED:
            continue
        anchor = item.last_reinforced_chapter or item.first_chapter or 0
        age = max(0, frontier - anchor)
        items.append(
            {
                "id": item.id,
                "name": item.name,
                "status": item.status,
                "description": item.description,
                "first_chapter": item.first_chapter,
                "last_reinforced_chapter": item.last_reinforced_chapter,
                "related_characters": item.related_characters or [],
                "expected_payoff": item.expected_payoff,
                "age": age,
                "overdue": age >= gap_threshold,
                "evidence": f"首现第{item.first_chapter}章，最近强化第{anchor}章，已 {age} 章未推进",
            }
        )
    items.sort(key=lambda item: (-item["age"], item["first_chapter"] or 0))
    return items


def suggest(
    session: Session, novel: Novel, *, horizon: int = DEFAULT_HORIZON, gap_threshold: int = DEBT_GAP
) -> dict[str, Any]:
    """给每个未回收伏笔一个建议回收窗口，按紧迫度排序。"""
    frontier = frontier_chapter(session, novel.id)
    items = debt(session, novel, gap_threshold=gap_threshold)
    suggestions: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        # 越久未回应的越靠前，同一章最多安排一条主伏笔 + 一条次要伏笔，避免堆在一起
        slot = frontier + 1 + min(index // 2, max(horizon - 1, 0))
        suggestions.append(
            {
                "foreshadowing_id": item["id"],
                "name": item["name"],
                "status": item["status"],
                "age": item["age"],
                "overdue": item["overdue"],
                "suggested_chapter": slot,
                "urgency": (
                    "HIGH"
                    if item["age"] >= gap_threshold + gap_threshold // 2
                    else "MEDIUM"
                    if item["age"] >= gap_threshold
                    else "LOW"
                ),
                "related_characters": item["related_characters"],
                "reason": (
                    f"{item['evidence']}；"
                    + (
                        "已明显拖欠，建议尽快安排一次正面回应"
                        if item["overdue"]
                        else "可在接下来几章自然带出"
                    )
                ),
                "expected_payoff": item["expected_payoff"],
            }
        )
    return {
        "frontier_chapter": frontier,
        "horizon": horizon,
        "overdue_count": sum(1 for item in items if item["overdue"]),
        "open_count": len(items),
        "suggestions": suggestions,
    }
