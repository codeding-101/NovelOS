"""想法碎片服务：录入、整理、安排、检索。

产品定位决定了这里的设计：碎片是**作者的原始素材**，系统只做三件事——
把它存好、让它能被检索到、在成文后如实回填「哪句话变成了哪段正文」。
系统不会替作者润色碎片本身（那正是要保留的个性）。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Chapter, Fragment, Novel
from app.schemas import FragmentCreate, FragmentUpdate

KINDS = (
    "WHIM",       # 奇思妙想
    "IMAGE",      # 画面 / 意象
    "LINE",       # 想写的一句对白或句子
    "SCENE",      # 场景设想
    "THEME",      # 对世界的看法 / 主题
    "CHARACTER",  # 人物瞬间或弧光
    "MECHANIC",   # 桥段 / 设定机制
    "OTHER",
)
STATUSES = ("INBOX", "PLACED", "REALIZED", "ARCHIVED")


def create_fragment(session: Session, novel: Novel, payload: FragmentCreate) -> Fragment:
    fragment = Fragment(
        novel_id=novel.id,
        kind=payload.kind if payload.kind in KINDS else "WHIM",
        title=payload.title or _auto_title(payload.text),
        text=payload.text,
        intent=payload.intent,
        tags=payload.tags,
        related_characters=payload.related_characters,
        target_chapter=payload.target_chapter,
        status=payload.status if payload.status in STATUSES else "INBOX",
        priority=max(1, min(5, payload.priority)),
        origin=payload.origin,
        prompted_by=payload.prompted_by,
        notes=payload.notes,
    )
    session.add(fragment)
    session.flush()
    _index(session, novel, fragment)
    return fragment


def update_fragment(session: Session, novel: Novel, fragment: Fragment, payload: FragmentUpdate) -> Fragment:
    data = payload.model_dump(exclude_unset=True)
    for key, value in data.items():
        if key == "kind" and value not in KINDS:
            continue
        if key == "status" and value not in STATUSES:
            continue
        setattr(fragment, key, value)
    session.flush()
    _index(session, novel, fragment)
    return fragment


def delete_fragment(session: Session, novel: Novel, fragment: Fragment) -> None:
    from app.services import vector_service

    vector_service.remove_ref(session, novel.id, "FRAGMENT", fragment.id)
    session.delete(fragment)
    session.flush()


def _auto_title(text: str) -> str:
    head = (text or "").strip().splitlines()[0] if text else ""
    return head[:40]


def _index(session: Session, novel: Novel, fragment: Fragment) -> None:
    """碎片同时进全文与向量索引：写作/规划/问答都能召回「作者当初的想法」。"""
    from app.services import vector_service

    text = "｜".join(part for part in (fragment.title, fragment.text, fragment.intent) if part)
    vector_service.index_records(
        session, novel.id, "FRAGMENT", fragment.id, [text], fragment.target_chapter
    )


def list_fragments(
    session: Session,
    novel_id: str,
    *,
    status: str | None = None,
    kind: str | None = None,
    character: str | None = None,
    target_chapter: int | None = None,
    unplaced_only: bool = False,
    limit: int = 200,
) -> list[Fragment]:
    stmt = select(Fragment).where(Fragment.novel_id == novel_id)
    if status:
        stmt = stmt.where(Fragment.status == status)
    if kind:
        stmt = stmt.where(Fragment.kind == kind)
    if target_chapter is not None:
        stmt = stmt.where(Fragment.target_chapter == target_chapter)
    if unplaced_only:
        stmt = stmt.where(Fragment.target_chapter.is_(None), Fragment.status == "INBOX")
    rows = list(session.scalars(stmt.order_by(Fragment.priority, Fragment.created_at)))
    if character:
        rows = [row for row in rows if character in (row.related_characters or [])]
    return rows[:limit]


def stats(session: Session, novel_id: str) -> dict[str, Any]:
    rows = list(session.scalars(select(Fragment).where(Fragment.novel_id == novel_id)))
    by_status: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    for row in rows:
        by_status[row.status] = by_status.get(row.status, 0) + 1
        by_kind[row.kind] = by_kind.get(row.kind, 0) + 1
    return {
        "total": len(rows),
        "by_status": by_status,
        "by_kind": by_kind,
        "inbox": by_status.get("INBOX", 0),
        "unplaced": sum(1 for row in rows if row.target_chapter is None and row.status == "INBOX"),
        "realized": by_status.get("REALIZED", 0),
        "realization_rate": round(
            by_status.get("REALIZED", 0) / len(rows), 3
        )
        if rows
        else 0.0,
    }


def fragments_for_chapter(session: Session, novel_id: str, chapter_number: int) -> list[Fragment]:
    """与某章相关的碎片：明确安排给这一章的 + 还没安排的（可选素材）。"""
    placed = list_fragments(session, novel_id, target_chapter=chapter_number)
    inbox = list_fragments(session, novel_id, unplaced_only=True, limit=20)
    seen = {fragment.id for fragment in placed}
    return placed + [fragment for fragment in inbox if fragment.id not in seen]


def relevant_fragments(
    session: Session, novel: Novel, query: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """按语义/关键词召回相关碎片（写作与规划都会用到）。"""
    from app.services import retrieval_service

    result = retrieval_service.hybrid_search(
        session, novel, query, ref_types=("FRAGMENT",), limit=limit
    )
    return result["hits"]


def mark_realized(
    session: Session,
    fragment: Fragment,
    *,
    chapter_id: str | None,
    excerpt: str,
    treatment: str,
    status: str = "REALIZED",
) -> Fragment:
    fragment.realized_chapter_id = chapter_id
    fragment.realized_excerpt = excerpt[:1000]
    fragment.realized_treatment = treatment
    fragment.status = status if status in STATUSES else "REALIZED"
    session.flush()
    return fragment


def place(session: Session, fragment: Fragment, chapter_number: int) -> Fragment:
    fragment.target_chapter = chapter_number
    if fragment.status == "INBOX":
        fragment.status = "PLACED"
    session.flush()
    return fragment


def fragment_to_dict(fragment: Fragment) -> dict[str, Any]:
    return {
        "id": fragment.id,
        "novel_id": fragment.novel_id,
        "kind": fragment.kind,
        "title": fragment.title,
        "text": fragment.text,
        "intent": fragment.intent,
        "tags": fragment.tags or [],
        "related_characters": fragment.related_characters or [],
        "target_chapter": fragment.target_chapter,
        "status": fragment.status,
        "priority": fragment.priority,
        "origin": fragment.origin,
        "prompted_by": fragment.prompted_by,
        "realized_chapter_id": fragment.realized_chapter_id,
        "realized_excerpt": fragment.realized_excerpt,
        "realized_treatment": fragment.realized_treatment,
        "notes": fragment.notes,
        "created_at": fragment.created_at.isoformat() if fragment.created_at else "",
        "updated_at": fragment.updated_at.isoformat() if fragment.updated_at else "",
    }


def chapter_intent(session: Session, novel_id: str, chapter_number: int) -> dict[str, Any]:
    """一章的「意图」= 安排给它的碎片 + 作者写的意图说明（评审只对照意图查执行）。"""
    fragments = list_fragments(session, novel_id, target_chapter=chapter_number)
    return {
        "chapter_number": chapter_number,
        "fragments": [fragment_to_dict(fragment) for fragment in fragments],
        "intents": [fragment.intent for fragment in fragments if fragment.intent],
        "count": len(fragments),
    }


def unplaced_fragments(session: Session, novel_id: str, limit: int = 10) -> list[dict[str, Any]]:
    return [
        fragment_to_dict(fragment)
        for fragment in list_fragments(session, novel_id, unplaced_only=True, limit=limit)
    ]


def total_fragments(session: Session, novel_id: str) -> int:
    return int(
        session.scalar(select(func.count(Fragment.id)).where(Fragment.novel_id == novel_id)) or 0
    )


def chapter_exists(session: Session, novel_id: str, chapter_number: int) -> bool:
    return (
        session.scalar(
            select(Chapter.id).where(
                Chapter.novel_id == novel_id, Chapter.chapter_number == chapter_number
            )
        )
        is not None
    )
