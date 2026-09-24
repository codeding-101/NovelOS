"""查询服务：人物、关系、事件、时间线、Canon、伏笔、世界规则的统一读取入口。

AITools 与 REST 路由都调用这里，保证「模型看到的」与「界面看到的」是同一套数据。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import (
    CanonFact,
    CanonStatus,
    Character,
    CharacterState,
    Event,
    Foreshadowing,
    Relationship,
    TimelineEntry,
    Visibility,
    WorldRule,
)
from app.timeutil import story_time_sort_key


def get_character(session: Session, novel_id: str, name: str) -> Character | None:
    return session.scalar(
        select(Character).where(Character.novel_id == novel_id, Character.name == name)
    )


def character_to_dict(character: Character) -> dict[str, Any]:
    return {
        "id": character.id,
        "name": character.name,
        "description": character.description,
        "personality": character.personality,
        "background": character.background,
        "goals": character.goals or [],
        "fears": character.fears or [],
        "current_location": character.current_location,
        "current_status": character.current_status,
        "known_facts": character.known_facts or [],
        "unknown_facts": character.unknown_facts or [],
        "first_appearance": character.first_appearance,
        "last_appearance": character.last_appearance,
    }


def get_character_state(
    session: Session, novel_id: str, name: str, chapter_number: int | None = None
) -> dict[str, Any]:
    """返回人物当前状态；给定 chapter_number 时额外返回该章时的历史状态。"""
    character = get_character(session, novel_id, name)
    if character is None:
        return {"found": False, "name": name, "status": "UNKNOWN"}

    history = sorted(character.states, key=lambda item: (item.chapter_number or 0))
    at_chapter = None
    if chapter_number is not None:
        candidates = [
            state
            for state in history
            if (state.chapter_number or 0) <= chapter_number and (state.status or state.location)
        ]
        if candidates:
            at_chapter = _state_to_dict(candidates[-1])
    return {
        "found": True,
        "character_id": character.id,
        "name": character.name,
        "current_status": character.current_status,
        "current_location": character.current_location,
        "first_appearance": character.first_appearance,
        "last_appearance": character.last_appearance,
        "state_at_chapter": at_chapter,
        "history": [_state_to_dict(state) for state in history],
    }


def _state_to_dict(state: CharacterState) -> dict[str, Any]:
    return {
        "id": state.id,
        "character_id": state.character_id,
        "chapter_number": state.chapter_number,
        "status": state.status,
        "location": state.location,
        "note": state.note,
        "source": state.source,
        "created_at": state.created_at.isoformat() if state.created_at else "",
    }


def record_character_state(
    session: Session,
    character: Character,
    *,
    status: str | None = None,
    location: str | None = None,
    note: str = "",
    chapter_number: int | None = None,
    chapter_id: str | None = None,
    source: str = "MANUAL",
) -> CharacterState:
    """写入一条状态历史并同步人物当前状态（后端统一完成，模型无法直接改库）。"""
    if status:
        character.current_status = status
    if location:
        character.current_location = location
    if chapter_number:
        if character.last_appearance is None or chapter_number > character.last_appearance:
            character.last_appearance = chapter_number
        if character.first_appearance is None or chapter_number < character.first_appearance:
            character.first_appearance = chapter_number
    state = CharacterState(
        character_id=character.id,
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        status=status or character.current_status,
        location=location or character.current_location,
        note=note,
        source=source,
    )
    session.add(state)
    session.flush()
    return state


def list_relationships(
    session: Session, novel_id: str, character_a: str | None = None, character_b: str | None = None
) -> list[dict[str, Any]]:
    names = {c.id: c.name for c in session.scalars(select(Character).where(Character.novel_id == novel_id))}
    rows = list(session.scalars(select(Relationship).where(Relationship.novel_id == novel_id)))
    results: list[dict[str, Any]] = []
    for row in rows:
        a_name = names.get(row.character_a_id, "")
        b_name = names.get(row.character_b_id, "")
        pair = {a_name, b_name}
        if character_a and character_a not in pair:
            continue
        if character_b and character_b not in pair:
            continue
        results.append(
            {
                "id": row.id,
                "character_a_id": row.character_a_id,
                "character_b_id": row.character_b_id,
                "character_a": a_name,
                "character_b": b_name,
                "relation": row.relation,
                "description": row.description,
                "status": row.status,
                "source_chapter": row.source_chapter,
            }
        )
    return results


def list_events(
    session: Session,
    novel_id: str,
    chapter_number: int | None = None,
    character: str | None = None,
    limit: int = 50,
    as_of_chapter: int | None = None,
) -> list[dict[str, Any]]:
    stmt = select(Event).where(Event.novel_id == novel_id)
    if chapter_number is not None:
        stmt = stmt.where(Event.chapter_number == chapter_number)
    if as_of_chapter is not None:
        stmt = stmt.where(
            or_(Event.chapter_number.is_(None), Event.chapter_number <= as_of_chapter)
        )
    rows = list(session.scalars(stmt.order_by(Event.chapter_number)))
    results: list[dict[str, Any]] = []
    for row in rows:
        if character and character not in (row.characters or []):
            continue
        results.append(
            {
                "id": row.id,
                "chapter_number": row.chapter_number,
                "chapter_id": row.chapter_id,
                "time": row.time,
                "location": row.location,
                "characters": row.characters or [],
                "description": row.description,
                "consequences": row.consequences,
                "status": row.status,
            }
        )
    return results[:limit]


def list_timeline(
    session: Session,
    novel_id: str,
    status: str | None = CanonStatus.CANON,
    limit: int = 100,
    as_of_chapter: int | None = None,
) -> list[dict[str, Any]]:
    stmt = select(TimelineEntry).where(TimelineEntry.novel_id == novel_id)
    if status:
        stmt = stmt.where(TimelineEntry.status == status)
    if as_of_chapter is not None:
        stmt = stmt.where(
            or_(
                TimelineEntry.chapter_number.is_(None),
                TimelineEntry.chapter_number <= as_of_chapter,
            )
        )
    rows = list(session.scalars(stmt.order_by(TimelineEntry.story_time_sort, TimelineEntry.chapter_number)))
    return [
        {
            "id": row.id,
            "story_time": row.story_time,
            "story_time_sort": row.story_time_sort,
            "chapter_number": row.chapter_number,
            "chapter_id": row.chapter_id,
            "event": row.event,
            "location": row.location,
            "description": row.description,
            "status": row.status,
        }
        for row in rows[:limit]
    ]


def list_canon_facts(
    session: Session,
    novel_id: str,
    subject: str | None = None,
    predicate: str | None = None,
    status: str | None = CanonStatus.CANON,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """默认只返回 CANON：PROPOSED 不能进入 Canon 上下文（核心安全原则）。"""
    stmt = select(CanonFact).where(CanonFact.novel_id == novel_id)
    if subject:
        stmt = stmt.where(CanonFact.subject == subject)
    if predicate:
        stmt = stmt.where(CanonFact.predicate == predicate)
    if status:
        stmt = stmt.where(CanonFact.status == status)
    rows = list(session.scalars(stmt.order_by(CanonFact.source_chapter, CanonFact.created_at)))
    return [canon_fact_to_dict(row) for row in rows[:limit]]


def canon_fact_to_dict(fact: CanonFact) -> dict[str, Any]:
    return {
        "id": fact.id,
        "subject": fact.subject,
        "predicate": fact.predicate,
        "object": fact.object,
        "source_chapter": fact.source_chapter,
        "valid_from_chapter": fact.valid_from_chapter,
        "valid_until_chapter": fact.valid_until_chapter,
        "status": fact.status,
        "confidence": fact.confidence,
        "visibility": fact.visibility,
        "known_by": fact.known_by or [],
        "origin": fact.origin,
        "note": fact.note,
        "superseded_by": fact.superseded_by,
    }


def canon_as_of(session: Session, novel_id: str, chapter_number: int) -> list[dict[str, Any]]:
    """时点视图：第 N 章时成立的 Canon 事实。

    包含当时仍生效、后来被取代的事实（它在那时确实成立），排除当时还没出现的、
    以及从未被确认的 PROPOSED / REJECTED 条目。这是长篇小说里「读第 6 章该按哪版设定判」
    的唯一答案，也让回溯检查早期章节不再被后来的设定变更污染。
    """
    stmt = (
        select(CanonFact)
        .where(
            CanonFact.novel_id == novel_id,
            CanonFact.status.in_((CanonStatus.CANON, CanonStatus.SUPERSEDED)),
            or_(
                CanonFact.valid_from_chapter.is_(None),
                CanonFact.valid_from_chapter <= chapter_number,
            ),
            or_(
                CanonFact.valid_until_chapter.is_(None),
                CanonFact.valid_until_chapter > chapter_number,
            ),
        )
        .order_by(CanonFact.valid_from_chapter, CanonFact.created_at)
    )
    return [canon_fact_to_dict(fact) for fact in session.scalars(stmt)]


def find_facts_by_subject_predicate(
    session: Session, novel_id: str, subject: str, predicate: str, statuses: tuple[str, ...]
) -> list[CanonFact]:
    return list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel_id,
                CanonFact.subject == subject,
                CanonFact.predicate == predicate,
                CanonFact.status.in_(statuses),
            )
        )
    )


def list_foreshadowing(
    session: Session,
    novel_id: str,
    status: str | None = None,
    character: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(Foreshadowing).where(Foreshadowing.novel_id == novel_id)
    if status:
        stmt = stmt.where(Foreshadowing.status == status)
    rows = list(session.scalars(stmt.order_by(Foreshadowing.first_chapter)))
    results: list[dict[str, Any]] = []
    for row in rows:
        if character and character not in (row.related_characters or []):
            continue
        results.append(
            {
                "id": row.id,
                "name": row.name,
                "description": row.description,
                "first_chapter": row.first_chapter,
                "related_characters": row.related_characters or [],
                "expected_payoff": row.expected_payoff,
                "status": row.status,
                "last_reinforced_chapter": row.last_reinforced_chapter,
            }
        )
    return results


def list_world_rules(session: Session, novel_id: str, enabled_only: bool = True) -> list[dict[str, Any]]:
    stmt = select(WorldRule).where(WorldRule.novel_id == novel_id)
    if enabled_only:
        stmt = stmt.where(WorldRule.enabled.is_(True))
    rows = list(session.scalars(stmt))
    return [
        {
            "id": row.id,
            "name": row.name,
            "rule_type": row.rule_type,
            "subject": row.subject,
            "params": row.params or {},
            "description": row.description,
            "source_chapter": row.source_chapter,
        }
        for row in rows
    ]


def propose_canon_fact(
    session: Session,
    novel_id: str,
    *,
    subject: str,
    predicate: str,
    object_value: str,
    source_chapter: int | None = None,
    confidence: float = 0.5,
    visibility: str = Visibility.PUBLIC,
    known_by: list[str] | None = None,
    origin: str = "AI",
    note: str = "",
) -> CanonFact:
    """任何来源（AI 工具或接口）提出的事实都只能以 PROPOSED 落库。

    这是核心安全原则 1、2 的唯一写入口：不存在「直接写 CANON」的代码路径。
    """
    fact = CanonFact(
        novel_id=novel_id,
        subject=subject.strip(),
        predicate=predicate.strip(),
        object=object_value.strip() or "UNKNOWN",
        source_chapter=source_chapter,
        valid_from_chapter=source_chapter or 1,
        status=CanonStatus.PROPOSED,
        confidence=max(0.0, min(1.0, confidence)),
        visibility=visibility if visibility in Visibility.ALL else Visibility.PUBLIC,
        known_by=known_by or [],
        origin=origin,
        note=note,
    )
    session.add(fact)
    session.flush()
    return fact


def add_timeline_entry(
    session: Session,
    novel_id: str,
    *,
    story_time: str,
    event: str = "",
    location: str = "",
    description: str = "",
    chapter_id: str | None = None,
    chapter_number: int | None = None,
    status: str = CanonStatus.CANON,
) -> TimelineEntry:
    entry = TimelineEntry(
        novel_id=novel_id,
        story_time=story_time.strip(),
        story_time_sort=story_time_sort_key(story_time),
        event=event.strip(),
        location=location.strip(),
        description=description,
        chapter_id=chapter_id,
        chapter_number=chapter_number,
        status=status,
    )
    session.add(entry)
    session.flush()
    return entry


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
