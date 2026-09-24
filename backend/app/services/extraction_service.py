"""抽取编排：跑 ExtractorAgent → 落库为待审项 → 由作者确认后写库。

角色分工（对应需求第六节与第八节）：
- 抽取阶段：FACT 类先以 PROPOSED 写入 canon_facts；其余类别（事件/时间线/伏笔/
  人物状态/关系）只作为 ExtractionItem 暂存，不碰正式表。
- 确认阶段：apply_run 只在作者把条目置为 ACCEPTED 后才写入正式表；
  升级 Canon 时会自动把同主谓的旧事实标记为 SUPERSEDED。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.agents import (
    ChapterWriter,
    ClaimVerifier,
    ContinuityChecker,
    ExtractorAgent,
    FragmentRealizer,
    MemorySearch,
    RevisionLoop,
    StyleCritic,
)
from app.ai.factory import resolve_provider
from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Event,
    ExtractionItem,
    ExtractionRun,
    Foreshadowing,
    ForeshadowStatus,
    ItemReviewStatus,
    Novel,
    Relationship,
    RunStatus,
)
from app.schemas import (
    ApplyRunResult,
    ExtractionItemOut,
    ExtractionResult,
    ExtractionRunDetail,
    ExtractionRunOut,
)
from app.services import commitment_service, query_service, vector_service


def build_agents(provider_name: str | None = None) -> dict[str, Any]:
    """按需组装 Agent。provider 未指定时用配置默认值，缺失密钥自动降级并给出警告。"""
    provider, warnings = resolve_provider(provider_name)
    return {
        "provider": provider,
        "warnings": warnings,
        "extractor": ExtractorAgent(provider),
        "continuity": ContinuityChecker(provider),
        "memory": MemorySearch(provider),
        "writer": ChapterWriter(provider),
        "style": StyleCritic(provider),
        "revision": RevisionLoop(provider),
        "realizer": FragmentRealizer(provider),
        "claims": ClaimVerifier(provider),
    }


def _item(
    kind: str,
    payload: dict[str, Any],
    review_status: str = ItemReviewStatus.PENDING,
    linked_fact_id: str | None = None,
) -> ExtractionItem:
    return ExtractionItem(
        kind=kind,
        payload=payload,
        review_status=review_status,
        linked_fact_id=linked_fact_id,
    )


def run_extraction(
    session: Session,
    novel: Novel,
    chapter: Chapter,
    *,
    provider_name: str | None = None,
    persist: bool = True,
) -> tuple[ExtractionResult, ExtractionRun | None, list[str], str, str]:
    """执行一次抽取。返回 (结果, 运行记录, 警告, provider, model)。"""
    agents = build_agents(provider_name)
    result, meta = agents["extractor"].run(session, novel, chapter)
    warnings = list(agents["warnings"]) + list(meta["warnings"])

    if not persist:
        return result, None, warnings, meta["provider"], meta["model"]

    run = ExtractionRun(
        novel_id=novel.id,
        chapter_id=chapter.id,
        chapter_number=chapter.chapter_number,
        status=RunStatus.PENDING_REVIEW,
        provider=meta["provider"],
        model=meta["model"],
        payload=result.model_dump(),
        warnings=warnings,
        raw_response=meta.get("raw_response", ""),
    )
    session.add(run)
    session.flush()

    existing_canon = {
        (fact.subject, fact.predicate, fact.object)
        for fact in session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.CANON
            )
        )
    }
    for fact in result.new_facts:
        key = (fact.subject, fact.predicate, fact.object)
        note = ""
        if key in existing_canon:
            note = "与既有 Canon 完全一致，通常无需重复确认"
            if note not in run.warnings:
                run.warnings.append(note)
        canon_fact = query_service.propose_canon_fact(
            session,
            novel.id,
            subject=fact.subject,
            predicate=fact.predicate,
            object_value=fact.object,
            source_chapter=fact.source_chapter or chapter.chapter_number,
            confidence=fact.confidence,
            visibility=fact.visibility,
            known_by=fact.known_by,
            origin="EXTRACTOR",
            note=note,
        )
        run.items.append(
            _item(
                "FACT",
                {**fact.model_dump(), "canon_fact_id": canon_fact.id, "note": note},
                linked_fact_id=canon_fact.id,
            )
        )

    for change in result.characters_changed:
        run.items.append(_item("CHARACTER_STATE", change.model_dump()))
    for event in result.events:
        run.items.append(_item("EVENT", event.model_dump()))
    for entry in result.timeline:
        run.items.append(_item("TIMELINE", entry.model_dump()))
    for item in result.foreshadowing:
        run.items.append(_item("FORESHADOWING", item.model_dump()))
    for relationship in result.relationships_changed:
        run.items.append(_item("RELATIONSHIP", relationship.model_dump()))
    for commitment in result.commitments:
        run.items.append(_item("COMMITMENT", commitment.model_dump()))
    for location in result.locations:
        run.items.append(_item("LOCATION", {"location": location}))

    run.payload = {**result.model_dump(), "warnings": run.warnings}
    session.flush()
    return result, run, run.warnings, meta["provider"], meta["model"]


def run_to_detail(run: ExtractionRun) -> ExtractionRunDetail:
    return ExtractionRunDetail(
        **ExtractionRunOut.model_validate(run).model_dump(),
        items=[ExtractionItemOut.model_validate(item) for item in run.items],
    )


def review_item(session: Session, item: ExtractionItem, review_status: str) -> ExtractionItem:
    if review_status not in (ItemReviewStatus.ACCEPTED, ItemReviewStatus.REJECTED):
        raise ValueError("review_status 只能是 ACCEPTED 或 REJECTED")
    item.review_status = review_status
    if review_status == ItemReviewStatus.REJECTED and item.kind == "FACT" and item.linked_fact_id:
        fact = session.get(CanonFact, item.linked_fact_id)
        if fact is not None and fact.status == CanonStatus.PROPOSED:
            fact.status = CanonStatus.REJECTED
    session.flush()
    _refresh_run_status(session, item.run_id)
    return item


def promote_fact(session: Session, fact: CanonFact, *, supersede_previous: bool = True, note: str = "") -> dict[str, Any]:
    """把 PROPOSED 事实升级为 CANON（唯一的升级入口，只由作者确认触发）。

    升级同时维护时点信息：新事实的生效章号 = 来源章；被取代的旧事实的失效章号 = 新事实的生效章，
    这样「第 N 章时哪条设定有效」在数据库里就是可查的。
    """
    if fact.valid_from_chapter is None:
        fact.valid_from_chapter = fact.source_chapter or 1
    if fact.status == CanonStatus.CANON:
        return {"fact_id": fact.id, "status": fact.status, "superseded": [], "message": "该事实已是 CANON"}
    superseded: list[str] = []
    if supersede_previous:
        siblings = session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == fact.novel_id,
                CanonFact.subject == fact.subject,
                CanonFact.predicate == fact.predicate,
                CanonFact.status == CanonStatus.CANON,
                CanonFact.id != fact.id,
            )
        )
        for sibling in siblings:
            if sibling.object == fact.object:
                sibling.note = (sibling.note + " 与新确认事实重复").strip()
            else:
                sibling.superseded_by = fact.id
                sibling.note = (sibling.note + " 已被新确认事实取代").strip()
            sibling.valid_until_chapter = max(
                int(fact.valid_from_chapter or 1), int(sibling.valid_from_chapter or 1) + 1
            )
            sibling.status = CanonStatus.SUPERSEDED
            superseded.append(sibling.id)
    fact.status = CanonStatus.CANON
    fact.confidence = max(fact.confidence, 0.9)
    fact.valid_until_chapter = None
    if note:
        fact.note = note
    session.flush()
    return {
        "fact_id": fact.id,
        "status": fact.status,
        "valid_from_chapter": fact.valid_from_chapter,
        "superseded": superseded,
        "message": "已升级为 CANON" + (f"，同时取代 {len(superseded)} 条旧事实" if superseded else ""),
    }


def reject_fact(session: Session, fact: CanonFact, note: str = "") -> dict[str, Any]:
    if fact.status == CanonStatus.CANON:
        raise ValueError("已确认的 CANON 事实不能直接驳回；如确需变更，请先确认一条新事实将其取代")
    fact.status = CanonStatus.REJECTED
    if note:
        fact.note = note
    session.flush()
    return {"fact_id": fact.id, "status": fact.status, "message": "已驳回"}


def apply_run(
    session: Session,
    novel: Novel,
    run: ExtractionRun,
    *,
    kinds: list[str] | None = None,
    accept_pending: bool = False,
) -> ApplyRunResult:
    """把已确认（ACCEPTED，或 accept_pending=True 时的 PENDING）条目写入正式表。"""
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    chapter = session.get(Chapter, run.chapter_id)

    for item in run.items:
        if kinds and item.kind not in kinds:
            skipped.append({"item_id": item.id, "kind": item.kind, "reason": "kind 不在本次应用范围内"})
            continue
        if item.review_status == ItemReviewStatus.ACCEPTED:
            pass
        elif item.review_status == ItemReviewStatus.PENDING and accept_pending:
            item.review_status = ItemReviewStatus.ACCEPTED
        else:
            skipped.append(
                {
                    "item_id": item.id,
                    "kind": item.kind,
                    "reason": f"当前状态为 {item.review_status}，未确认条目不会写库",
                }
            )
            continue

        payload = item.payload or {}
        if item.kind == "FACT":
            fact = session.get(CanonFact, item.linked_fact_id) if item.linked_fact_id else None
            if fact is None:
                skipped.append({"item_id": item.id, "kind": item.kind, "reason": "关联的 PROPOSED 事实不存在"})
                continue
            result = promote_fact(session, fact)
            item.applied_ref_id = fact.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": fact.id, **result})
        elif item.kind == "CHARACTER_STATE":
            character = query_service.get_character(session, novel.id, payload.get("name", ""))
            if character is None:
                skipped.append(
                    {
                        "item_id": item.id,
                        "kind": item.kind,
                        "reason": f"人物「{payload.get('name')}」尚未建档，请先在人物面板创建",
                    }
                )
                continue
            state = query_service.record_character_state(
                session,
                character,
                status=payload.get("status_change"),
                location=payload.get("location"),
                note=(payload.get("notes") or "")[:200],
                chapter_number=chapter.chapter_number if chapter else None,
                chapter_id=chapter.id if chapter else None,
                source="EXTRACTOR",
            )
            item.applied_ref_id = state.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": state.id})
        elif item.kind == "EVENT":
            event = Event(
                novel_id=novel.id,
                chapter_id=chapter.id if chapter else None,
                chapter_number=chapter.chapter_number if chapter else None,
                time=payload.get("time"),
                location=payload.get("location") or "",
                characters=payload.get("characters") or [],
                description=payload.get("description") or "",
                consequences=payload.get("consequences") or "",
                status=CanonStatus.CANON,
            )
            session.add(event)
            session.flush()
            item.applied_ref_id = event.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": event.id})
        elif item.kind == "TIMELINE":
            entry = query_service.add_timeline_entry(
                session,
                novel.id,
                story_time=payload.get("story_time") or "",
                event=payload.get("event") or "",
                location=payload.get("location") or "",
                description=payload.get("description") or "",
                chapter_id=chapter.id if chapter else None,
                chapter_number=chapter.chapter_number if chapter else None,
            )
            item.applied_ref_id = entry.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": entry.id})
        elif item.kind == "FORESHADOWING":
            existing = session.scalar(
                select(Foreshadowing).where(
                    Foreshadowing.novel_id == novel.id,
                    Foreshadowing.name == payload.get("name", ""),
                )
            )
            if existing is not None:
                existing.last_reinforced_chapter = chapter.chapter_number if chapter else existing.last_reinforced_chapter
                if existing.status == ForeshadowStatus.OPEN:
                    existing.status = ForeshadowStatus.DEVELOPING
                item.applied_ref_id = existing.id
                applied.append(
                    {"item_id": item.id, "kind": item.kind, "ref_id": existing.id, "message": "同名伏笔已存在，改为强化"}
                )
                continue
            record = Foreshadowing(
                novel_id=novel.id,
                name=payload.get("name") or "未命名伏笔",
                description=payload.get("description") or "",
                first_chapter=chapter.chapter_number if chapter else None,
                related_characters=payload.get("related_characters") or [],
                expected_payoff=payload.get("expected_payoff") or "",
                status=ForeshadowStatus.OPEN,
                last_reinforced_chapter=chapter.chapter_number if chapter else None,
            )
            session.add(record)
            session.flush()
            item.applied_ref_id = record.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": record.id})
        elif item.kind == "RELATIONSHIP":
            name_a, name_b = payload.get("character_a", ""), payload.get("character_b", "")
            character_a = query_service.get_character(session, novel.id, name_a)
            character_b = query_service.get_character(session, novel.id, name_b)
            if character_a is None or character_b is None:
                skipped.append(
                    {
                        "item_id": item.id,
                        "kind": item.kind,
                        "reason": f"人物未建档：{[name for name, c in ((name_a, character_a), (name_b, character_b)) if c is None]}",
                    }
                )
                continue
            existing = session.scalar(
                select(Relationship).where(
                    Relationship.novel_id == novel.id,
                    Relationship.character_a_id == character_a.id,
                    Relationship.character_b_id == character_b.id,
                )
            )
            if existing is not None:
                existing.relation = payload.get("relation") or existing.relation
                existing.description = payload.get("change") or existing.description
                existing.source_chapter = chapter.chapter_number if chapter else existing.source_chapter
                item.applied_ref_id = existing.id
                applied.append({"item_id": item.id, "kind": item.kind, "ref_id": existing.id})
                continue
            record = Relationship(
                novel_id=novel.id,
                character_a_id=character_a.id,
                character_b_id=character_b.id,
                relation=payload.get("relation") or "",
                description=payload.get("change") or "",
                source_chapter=chapter.chapter_number if chapter else None,
            )
            session.add(record)
            session.flush()
            item.applied_ref_id = record.id
            applied.append({"item_id": item.id, "kind": item.kind, "ref_id": record.id})
        elif item.kind == "COMMITMENT":
            record = commitment_service.add_commitment(
                session,
                novel,
                source_chapter=chapter.chapter_number if chapter else None,
                kind=payload.get("kind") or "APPOINTMENT",
                who=payload.get("who") or "",
                counterpart=payload.get("counterpart") or "",
                what=payload.get("what") or payload.get("quote") or "",
                quote=payload.get("quote") or "",
                deadline_text=payload.get("deadline_text") or "",
                story_time=chapter.story_time if chapter else None,
                origin="EXTRACTOR",
            )
            item.applied_ref_id = record.id
            applied.append(
                {
                    "item_id": item.id,
                    "kind": item.kind,
                    "ref_id": record.id,
                    "due_story_time": record.due_story_time,
                }
            )
        elif item.kind == "LOCATION":
            location = payload.get("location") or ""
            if chapter is not None and location and not chapter.location:
                chapter.location = location
                applied.append({"item_id": item.id, "kind": item.kind, "ref_id": chapter.id, "message": "已补入章节地点"})
            else:
                skipped.append({"item_id": item.id, "kind": item.kind, "reason": "章节地点已存在，无需写入"})
        else:
            skipped.append({"item_id": item.id, "kind": item.kind, "reason": "未知条目类型"})

        item.applied_at = datetime.now(timezone.utc)

    session.flush()
    # 设定变了，向量索引里的设定条目要跟着更新（章节片段在保存时已经索引过）
    vector_service.index_entities(session, novel.id)
    _refresh_run_status(session, run.id)
    status = run.status
    message = {
        RunStatus.APPLIED: "全部确认条目已写库",
        RunStatus.PARTIALLY_APPLIED: "部分条目已写库，仍有待确认条目",
        RunStatus.PENDING_REVIEW: "没有可写入的条目（可能都未确认）",
        RunStatus.REJECTED: "全部条目已驳回",
    }.get(status, "")
    return ApplyRunResult(run_id=run.id, status=status, applied=applied, skipped=skipped, message=message)


def _refresh_run_status(session: Session, run_id: str) -> None:
    run = session.get(ExtractionRun, run_id)
    if run is None:
        return
    statuses = [item.review_status for item in run.items]
    applied = [item for item in run.items if item.applied_ref_id]
    if statuses and all(status == ItemReviewStatus.REJECTED for status in statuses):
        run.status = RunStatus.REJECTED
    elif applied and any(status == ItemReviewStatus.PENDING for status in statuses):
        run.status = RunStatus.PARTIALLY_APPLIED
    elif applied and not any(status == ItemReviewStatus.PENDING for status in statuses):
        run.status = RunStatus.APPLIED
    else:
        run.status = RunStatus.PENDING_REVIEW
    if run.status in (RunStatus.APPLIED, RunStatus.PARTIALLY_APPLIED):
        run.applied_at = datetime.now(timezone.utc)
    session.flush()


def list_runs(session: Session, novel_id: str, chapter_id: str | None = None, limit: int = 20) -> list[ExtractionRun]:
    stmt = select(ExtractionRun).where(ExtractionRun.novel_id == novel_id)
    if chapter_id:
        stmt = stmt.where(ExtractionRun.chapter_id == chapter_id)
    return list(session.scalars(stmt.order_by(ExtractionRun.created_at.desc()).limit(limit)))
