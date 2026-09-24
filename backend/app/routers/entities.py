"""人物 / 关系 / 事件 / Canon / 伏笔 / 时间线 / 世界观规则 路由。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import (
    CanonFact,
    Character,
    Event,
    Foreshadowing,
    Novel,
    Relationship,
    TimelineEntry,
    Visibility,
    WorldRule,
)
from app.schemas import (
    CanonFactCreate,
    CanonFactOut,
    CanonFactUpdate,
    CharacterCreate,
    CharacterOut,
    CharacterStateIn,
    CharacterStateOut,
    CharacterUpdate,
    EventCreate,
    EventOut,
    FactDecision,
    ForeshadowingCreate,
    ForeshadowingOut,
    ForeshadowingUpdate,
    RelationshipCreate,
    RelationshipView,
    TimelineEntryCreate,
    TimelineEntryOut,
    WorldRuleCreate,
    WorldRuleOut,
)
from app.services import extraction_service, query_service
from app.timeutil import story_time_sort_key

novel_router = APIRouter(prefix="/api/novels", tags=["设定库"])
item_router = APIRouter(prefix="/api", tags=["设定库"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


# --------------------------------------------------------------------------- 人物
@novel_router.get("/{novel_id}/characters", response_model=list[CharacterOut])
def list_characters(novel_id: str, session: Session = Depends(get_session)) -> list[Character]:
    _novel(session, novel_id)
    return list(
        session.scalars(
            select(Character).where(Character.novel_id == novel_id).order_by(Character.name)
        )
    )


@novel_router.post(
    "/{novel_id}/characters", response_model=CharacterOut, status_code=status.HTTP_201_CREATED
)
def create_character(
    novel_id: str, payload: CharacterCreate, session: Session = Depends(get_session)
) -> Character:
    _novel(session, novel_id)
    exists = session.scalar(
        select(Character).where(Character.novel_id == novel_id, Character.name == payload.name)
    )
    if exists is not None:
        raise HTTPException(status_code=409, detail=f"人物「{payload.name}」已存在")
    character = Character(novel_id=novel_id, **payload.model_dump())
    session.add(character)
    session.flush()
    return character


@item_router.get("/characters/{character_id}", response_model=CharacterOut)
def get_character(character_id: str, session: Session = Depends(get_session)) -> Character:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return character


@item_router.patch("/characters/{character_id}", response_model=CharacterOut)
def update_character(
    character_id: str, payload: CharacterUpdate, session: Session = Depends(get_session)
) -> Character:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(character, key, value)
    session.flush()
    return character


@item_router.delete("/characters/{character_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_character(character_id: str, session: Session = Depends(get_session)) -> None:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    session.delete(character)
    session.flush()


@item_router.get("/characters/{character_id}/states", response_model=list[CharacterStateOut])
def list_character_states(
    character_id: str, session: Session = Depends(get_session)
) -> list[CharacterStateOut]:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    return [
        CharacterStateOut.model_validate(state)
        for state in sorted(character.states, key=lambda item: (item.chapter_number or 0, item.created_at))
    ]


@item_router.post(
    "/characters/{character_id}/states",
    response_model=CharacterStateOut,
    status_code=status.HTTP_201_CREATED,
)
def add_character_state(
    character_id: str, payload: CharacterStateIn, session: Session = Depends(get_session)
) -> CharacterStateOut:
    character = session.get(Character, character_id)
    if character is None:
        raise HTTPException(status_code=404, detail="人物不存在")
    state = query_service.record_character_state(
        session,
        character,
        status=payload.status,
        location=payload.location,
        note=payload.note,
        chapter_number=payload.chapter_number,
        source=payload.source,
    )
    return CharacterStateOut.model_validate(state)


# --------------------------------------------------------------------------- 关系
@novel_router.get("/{novel_id}/relationships", response_model=list[RelationshipView])
def list_relationships(
    novel_id: str,
    character: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[dict]:
    _novel(session, novel_id)
    rows = query_service.list_relationships(session, novel_id, character)
    return [
        RelationshipView(
            id=row["id"],
            novel_id=novel_id,
            character_a_id=row["character_a_id"],
            character_b_id=row["character_b_id"],
            relation=row["relation"],
            description=row["description"],
            status=row["status"],
            source_chapter=row["source_chapter"],
            character_a_name=row["character_a"],
            character_b_name=row["character_b"],
        )
        for row in rows
    ]


@novel_router.post(
    "/{novel_id}/relationships", response_model=RelationshipView, status_code=status.HTTP_201_CREATED
)
def create_relationship(
    novel_id: str, payload: RelationshipCreate, session: Session = Depends(get_session)
) -> RelationshipView:
    _novel(session, novel_id)
    character_a = query_service.get_character(session, novel_id, payload.character_a)
    character_b = query_service.get_character(session, novel_id, payload.character_b)
    if character_a is None or character_b is None:
        raise HTTPException(status_code=404, detail="关系双方必须都已建档")
    record = Relationship(
        novel_id=novel_id,
        character_a_id=character_a.id,
        character_b_id=character_b.id,
        relation=payload.relation,
        description=payload.description,
        status=payload.status,
        source_chapter=payload.source_chapter,
    )
    session.add(record)
    session.flush()
    return RelationshipView(
        id=record.id,
        novel_id=novel_id,
        character_a_id=character_a.id,
        character_b_id=character_b.id,
        relation=record.relation,
        description=record.description,
        status=record.status,
        source_chapter=record.source_chapter,
        character_a_name=character_a.name,
        character_b_name=character_b.name,
    )


# --------------------------------------------------------------------------- 事件
@novel_router.get("/{novel_id}/events", response_model=list[EventOut])
def list_events(
    novel_id: str,
    chapter_number: int | None = Query(default=None),
    character: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[Event]:
    _novel(session, novel_id)
    stmt = select(Event).where(Event.novel_id == novel_id)
    if chapter_number is not None:
        stmt = stmt.where(Event.chapter_number == chapter_number)
    rows = list(session.scalars(stmt.order_by(Event.chapter_number)))
    if character:
        rows = [row for row in rows if character in (row.characters or [])]
    return rows


@novel_router.post(
    "/{novel_id}/events", response_model=EventOut, status_code=status.HTTP_201_CREATED
)
def create_event(
    novel_id: str, payload: EventCreate, session: Session = Depends(get_session)
) -> Event:
    _novel(session, novel_id)
    event = Event(novel_id=novel_id, **payload.model_dump())
    session.add(event)
    session.flush()
    return event


@item_router.delete("/events/{event_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_event(event_id: str, session: Session = Depends(get_session)) -> None:
    event = session.get(Event, event_id)
    if event is None:
        raise HTTPException(status_code=404, detail="事件不存在")
    session.delete(event)
    session.flush()


# --------------------------------------------------------------------------- Canon 事实
@novel_router.get("/{novel_id}/canon-facts", response_model=list[CanonFactOut])
def list_canon_facts(
    novel_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    subject: str | None = Query(default=None),
    session: Session = Depends(get_session),
) -> list[CanonFact]:
    _novel(session, novel_id)
    stmt = select(CanonFact).where(CanonFact.novel_id == novel_id)
    if status_filter:
        stmt = stmt.where(CanonFact.status == status_filter)
    if subject:
        stmt = stmt.where(CanonFact.subject == subject)
    return list(session.scalars(stmt.order_by(CanonFact.subject, CanonFact.created_at)))


@novel_router.post(
    "/{novel_id}/canon-facts", response_model=CanonFactOut, status_code=status.HTTP_201_CREATED
)
def create_canon_fact(
    novel_id: str, payload: CanonFactCreate, session: Session = Depends(get_session)
) -> CanonFact:
    """作者手工录入事实。这是唯一可以一次性写入 CANON 的接口（人工确认通道）。"""
    _novel(session, novel_id)
    fact = CanonFact(
        novel_id=novel_id,
        subject=payload.subject,
        predicate=payload.predicate,
        object=payload.object,
        source_chapter=payload.source_chapter,
        valid_from_chapter=payload.source_chapter or 1,
        status=payload.status,
        confidence=payload.confidence,
        visibility=payload.visibility if payload.visibility in Visibility.ALL else Visibility.PUBLIC,
        known_by=payload.known_by,
        origin="USER",
        note=payload.note,
    )
    session.add(fact)
    session.flush()
    return fact


@item_router.patch("/canon-facts/{fact_id}", response_model=CanonFactOut)
def update_canon_fact(
    fact_id: str, payload: CanonFactUpdate, session: Session = Depends(get_session)
) -> CanonFact:
    fact = session.get(CanonFact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(fact, key, value)
    session.flush()
    return fact


@item_router.delete("/canon-facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_canon_fact(fact_id: str, session: Session = Depends(get_session)) -> None:
    fact = session.get(CanonFact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    session.delete(fact)
    session.flush()


@item_router.post("/canon-facts/{fact_id}/confirm")
def confirm_canon_fact(
    fact_id: str, payload: FactDecision, session: Session = Depends(get_session)
) -> dict:
    """作者确认：PROPOSED → CANON（安全原则 1 的唯一升级入口）。"""
    fact = session.get(CanonFact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    return extraction_service.promote_fact(
        session, fact, supersede_previous=payload.supersede_previous, note=payload.note
    )


@item_router.post("/canon-facts/{fact_id}/reject")
def reject_canon_fact(
    fact_id: str, payload: FactDecision, session: Session = Depends(get_session)
) -> dict:
    fact = session.get(CanonFact, fact_id)
    if fact is None:
        raise HTTPException(status_code=404, detail="事实不存在")
    try:
        return extraction_service.reject_fact(session, fact, payload.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------------------- 伏笔
@novel_router.get("/{novel_id}/foreshadowings", response_model=list[ForeshadowingOut])
def list_foreshadowings(
    novel_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    session: Session = Depends(get_session),
) -> list[Foreshadowing]:
    _novel(session, novel_id)
    stmt = select(Foreshadowing).where(Foreshadowing.novel_id == novel_id)
    if status_filter:
        stmt = stmt.where(Foreshadowing.status == status_filter)
    return list(session.scalars(stmt.order_by(Foreshadowing.first_chapter)))


@novel_router.post(
    "/{novel_id}/foreshadowings",
    response_model=ForeshadowingOut,
    status_code=status.HTTP_201_CREATED,
)
def create_foreshadowing(
    novel_id: str, payload: ForeshadowingCreate, session: Session = Depends(get_session)
) -> Foreshadowing:
    _novel(session, novel_id)
    record = Foreshadowing(novel_id=novel_id, **payload.model_dump())
    session.add(record)
    session.flush()
    return record


@item_router.patch("/foreshadowings/{foreshadowing_id}", response_model=ForeshadowingOut)
def update_foreshadowing(
    foreshadowing_id: str, payload: ForeshadowingUpdate, session: Session = Depends(get_session)
) -> Foreshadowing:
    record = session.get(Foreshadowing, foreshadowing_id)
    if record is None:
        raise HTTPException(status_code=404, detail="伏笔不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(record, key, value)
    session.flush()
    return record


@item_router.delete("/foreshadowings/{foreshadowing_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_foreshadowing(foreshadowing_id: str, session: Session = Depends(get_session)) -> None:
    record = session.get(Foreshadowing, foreshadowing_id)
    if record is None:
        raise HTTPException(status_code=404, detail="伏笔不存在")
    session.delete(record)
    session.flush()


# --------------------------------------------------------------------------- 时间线
@novel_router.get("/{novel_id}/timeline", response_model=list[TimelineEntryOut])
def list_timeline(
    novel_id: str,
    status_filter: str | None = Query(default=None, alias="status"),
    session: Session = Depends(get_session),
) -> list[TimelineEntry]:
    _novel(session, novel_id)
    stmt = select(TimelineEntry).where(TimelineEntry.novel_id == novel_id)
    if status_filter:
        stmt = stmt.where(TimelineEntry.status == status_filter)
    return list(
        session.scalars(
            stmt.order_by(TimelineEntry.story_time_sort, TimelineEntry.chapter_number)
        )
    )


@novel_router.post(
    "/{novel_id}/timeline", response_model=TimelineEntryOut, status_code=status.HTTP_201_CREATED
)
def create_timeline_entry(
    novel_id: str, payload: TimelineEntryCreate, session: Session = Depends(get_session)
) -> TimelineEntry:
    _novel(session, novel_id)
    entry = TimelineEntry(
        novel_id=novel_id,
        story_time=payload.story_time,
        story_time_sort=story_time_sort_key(payload.story_time),
        chapter_id=payload.chapter_id,
        chapter_number=payload.chapter_number,
        event=payload.event,
        location=payload.location,
        description=payload.description,
        status=payload.status,
    )
    session.add(entry)
    session.flush()
    return entry


@item_router.delete("/timeline/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_timeline_entry(entry_id: str, session: Session = Depends(get_session)) -> None:
    entry = session.get(TimelineEntry, entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="时间线条目不存在")
    session.delete(entry)
    session.flush()


# --------------------------------------------------------------------------- 世界观规则
@novel_router.get("/{novel_id}/world-rules", response_model=list[WorldRuleOut])
def list_world_rules(novel_id: str, session: Session = Depends(get_session)) -> list[WorldRule]:
    _novel(session, novel_id)
    return list(session.scalars(select(WorldRule).where(WorldRule.novel_id == novel_id)))


@novel_router.post(
    "/{novel_id}/world-rules", response_model=WorldRuleOut, status_code=status.HTTP_201_CREATED
)
def create_world_rule(
    novel_id: str, payload: WorldRuleCreate, session: Session = Depends(get_session)
) -> WorldRule:
    _novel(session, novel_id)
    rule = WorldRule(novel_id=novel_id, **payload.model_dump())
    session.add(rule)
    session.flush()
    return rule


@item_router.delete("/world-rules/{rule_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_world_rule(rule_id: str, session: Session = Depends(get_session)) -> None:
    rule = session.get(WorldRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    session.delete(rule)
    session.flush()


# --------------------------------------------------------------------------- 概览
@novel_router.get("/{novel_id}/bible")
def novel_bible(novel_id: str, session: Session = Depends(get_session)) -> dict:
    """一次性返回设定库全貌，供底部面板与问答面板初始化使用。"""
    _novel(session, novel_id)
    return {
        "characters": [
            query_service.character_to_dict(character)
            for character in session.scalars(
                select(Character).where(Character.novel_id == novel_id).order_by(Character.name)
            )
        ],
        "relationships": query_service.list_relationships(session, novel_id),
        "events": query_service.list_events(session, novel_id, limit=200),
        "canon_facts": query_service.list_canon_facts(session, novel_id, status=None, limit=500),
        "foreshadowings": query_service.list_foreshadowing(session, novel_id),
        "timeline": query_service.list_timeline(session, novel_id, status=None, limit=300),
        "world_rules": query_service.list_world_rules(session, novel_id, enabled_only=False),
    }
