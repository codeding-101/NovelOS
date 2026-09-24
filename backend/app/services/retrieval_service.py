"""混合检索：关键词通道 + 向量通道，用 RRF 融合成统一的候选列表。

关键词通道负责精确命中（人名、地名、专有名词），向量通道负责「说法不一样但讲的是同一件事」；
两者互补，任何一路缺席（比如没做向量索引）其余仍可工作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Character,
    Event,
    Foreshadowing,
    Fragment,
    Novel,
    TimelineEntry,
)
from app.services import search_service, vector_service

RRF_K = 60
#: 正文片段的向量入榜门槛：本地 n-gram 向量对无关文本也会给出很小的正相似度，需要过滤
VECTOR_MIN_SCORE = 0.20
#: 设定类条目（事实/时间线/事件/人物/伏笔）只靠向量召回时的门槛，比正文片段更严
VECTOR_ENTITY_MIN_SCORE = 0.34
#: 只靠向量命中时，达到这个分数才算「结构化证据」，避免把泛泛相关的条目当成答案
VECTOR_STRUCTURED_SCORE = 0.34

TYPE_BONUS = {
    "CANON_FACT": 3.0,
    "TIMELINE": 2.0,
    "EVENT": 1.5,
    "CHARACTER": 1.5,
    "FORESHADOWING": 1.2,
    "FRAGMENT": 2.5,  # 作者自己的话优先级高：成文时首先应体现作者的意图
    "CHAPTER": 0.0,
}


@dataclass
class RetrievalHit:
    ref_type: str
    ref_id: str
    chapter_number: int | None
    title: str
    excerpt: str
    keyword_score: float = 0.0
    vector_score: float = 0.0
    term_weight: float = 0.0
    terms_matched: list[str] = field(default_factory=list)
    entity_term_count: int = 0
    status: str = ""
    channels: list[str] = field(default_factory=list)
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref_type": self.ref_type,
            "ref_id": self.ref_id,
            "chapter_number": self.chapter_number,
            "title": self.title,
            "excerpt": self.excerpt[:400],
            "keyword_score": round(self.keyword_score, 3),
            "vector_score": round(self.vector_score, 4),
            "term_weight": round(self.term_weight, 3),
            "terms_matched": self.terms_matched[:8],
            "entity_term_count": self.entity_term_count,
            "status": self.status,
            "channels": self.channels,
            "score": round(self.score, 3),
        }


def _excerpt(text: str, limit: int = 220) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _keyword_sources(session: Session, novel_id: str) -> dict[str, list[dict[str, Any]]]:
    """把设定类数据整理成可检索的条目（标题 + 摘要文本 + 原始字段）。"""
    sources: dict[str, list[dict[str, Any]]] = {key: [] for key in TYPE_BONUS if key != "CHAPTER"}

    for fact in session.scalars(select(CanonFact).where(CanonFact.novel_id == novel_id)):
        if fact.status in (CanonStatus.REJECTED,):
            continue
        sources["CANON_FACT"].append(
            {
                "ref_id": fact.id,
                "chapter_number": fact.source_chapter,
                "title": f"{fact.subject}的{fact.predicate}",
                "text": f"{fact.subject} {fact.predicate} {fact.object}",
                "excerpt": f"{fact.subject} {fact.predicate} {fact.object}［{fact.status}］",
                "status": fact.status,
            }
        )
    for entry in session.scalars(
        select(TimelineEntry).where(TimelineEntry.novel_id == novel_id)
    ):
        sources["TIMELINE"].append(
            {
                "ref_id": entry.id,
                "chapter_number": entry.chapter_number,
                "title": f"时间线：{entry.event}",
                "text": f"{entry.event} {entry.location} {entry.description}",
                "excerpt": f"{entry.story_time}｜{entry.event}｜{entry.location}",
                "status": entry.status,
            }
        )
    for event in session.scalars(select(Event).where(Event.novel_id == novel_id)):
        sources["EVENT"].append(
            {
                "ref_id": event.id,
                "chapter_number": event.chapter_number,
                "title": f"事件（第{event.chapter_number}章）",
                "text": f"{event.description} {event.location} {' '.join(event.characters or [])}",
                "excerpt": f"{event.time or ''}｜{event.location}｜{event.description[:80]}",
                "status": event.status,
            }
        )
    for item in session.scalars(
        select(Foreshadowing).where(Foreshadowing.novel_id == novel_id)
    ):
        sources["FORESHADOWING"].append(
            {
                "ref_id": item.id,
                "chapter_number": item.first_chapter,
                "title": f"伏笔：{item.name}",
                "text": f"{item.name} {item.description} {item.expected_payoff}",
                "excerpt": f"{item.description[:120]}｜状态：{item.status}",
                "status": item.status,
            }
        )
    for character in session.scalars(select(Character).where(Character.novel_id == novel_id)):
        sources["CHARACTER"].append(
            {
                "ref_id": character.id,
                "chapter_number": character.first_appearance,
                "title": f"人物：{character.name}",
                "text": (
                    f"{character.name} {character.description} {character.personality} "
                    f"{character.background} {character.current_status} {character.current_location}"
                ),
                "excerpt": (
                    f"状态={character.current_status}，位置={character.current_location}；"
                    f"{character.description[:60]}"
                ),
                "status": "CANON",
            }
        )
    for fragment in session.scalars(select(Fragment).where(Fragment.novel_id == novel_id)):
        sources["FRAGMENT"].append(
            {
                "ref_id": fragment.id,
                "chapter_number": fragment.target_chapter,
                "title": f"碎片：{fragment.title or fragment.kind}",
                "text": f"{fragment.title} {fragment.text} {fragment.intent}",
                "excerpt": (fragment.text or "")[:160],
                "status": fragment.status,
            }
        )
    return sources


def hybrid_search(
    session: Session,
    novel: Novel,
    query: str,
    *,
    terms: list[str] | None = None,
    weights: dict[str, float] | None = None,
    ref_types: tuple[str, ...] | None = None,
    limit: int = 10,
    use_vector: bool = True,
) -> dict[str, Any]:
    """返回 {query, engine, channels, hits:[RetrievalHit]}。"""
    terms = [term for term in (terms or [query]) if term]
    if not terms:
        return {"query": query, "engine": "none", "channels": [], "hits": []}
    weights = weights or {}

    hits: dict[tuple[str, str], RetrievalHit] = {}

    # ---- 关键词通道：设定类条目按命中词的权重计分
    if ref_types is None or any(kind != "CHAPTER" for kind in ref_types):
        for ref_type, rows in _keyword_sources(session, novel.id).items():
            if ref_types and ref_type not in ref_types:
                continue
            for row in rows:
                matched = [term for term in terms if term in row["text"]]
                if not matched:
                    continue
                weight = sum(weights.get(term, 0.4) for term in matched)
                entity_count = sum(1 for term in matched if weights.get(term, 0.4) >= 1.0)
                key = (ref_type, row["ref_id"])
                existing = hits.get(key)
                if existing is None:
                    hits[key] = RetrievalHit(
                        ref_type=ref_type,
                        ref_id=row["ref_id"],
                        chapter_number=row["chapter_number"],
                        title=row["title"],
                        excerpt=row["excerpt"],
                        keyword_score=weight,
                        term_weight=weight,
                        terms_matched=matched,
                        entity_term_count=entity_count,
                        status=row.get("status", ""),
                        channels=["keyword"],
                    )
                else:
                    existing.term_weight += weight
                    existing.terms_matched.extend(matched)

    # ---- 关键词通道：章节全文
    if ref_types is None or "CHAPTER" in ref_types:
        for term in terms:
            weight = weights.get(term, 0.4)
            result = search_service.search_chapters(session, novel.id, term, limit=5)
            for hit in result["hits"]:
                key = ("CHAPTER", hit["chapter_id"])
                existing = hits.get(key)
                contribution = min(hit["score"], 5.0) * 0.6 * weight
                if existing is None:
                    hits[key] = RetrievalHit(
                        ref_type="CHAPTER",
                        ref_id=hit["chapter_id"],
                        chapter_number=hit["chapter_number"],
                        title=f"第{hit['chapter_number']}章 {hit['title']}",
                        excerpt=hit["snippet"],
                        keyword_score=contribution,
                        term_weight=weight,
                        terms_matched=[term],
                        entity_term_count=1 if weight >= 1.0 else 0,
                        status="CHAPTER",
                        channels=["keyword"],
                    )
                else:
                    existing.keyword_score += contribution
                    existing.terms_matched.append(term)

    keyword_ranked = sorted(hits.values(), key=lambda item: -item.keyword_score)
    for rank, item in enumerate(keyword_ranked):
        item.score += 1.0 / (RRF_K + rank + 1)

    # ---- 向量通道
    warnings: list[str] = []
    engine = "none"
    if use_vector:
        vector_hits = vector_service.vector_search(
            session, novel.id, query, ref_types=ref_types, limit=max(limit * 3, 20)
        )
        best_per_ref: dict[tuple[str, str], dict[str, Any]] = {}
        for item in vector_hits:
            threshold = (
                VECTOR_MIN_SCORE if item["ref_type"] == "CHAPTER" else VECTOR_ENTITY_MIN_SCORE
            )
            if item["vector_score"] < threshold:
                continue
            key = (item["ref_type"], item["ref_id"])
            if key not in best_per_ref or item["vector_score"] > best_per_ref[key]["vector_score"]:
                best_per_ref[key] = item
        if best_per_ref:
            engine = "hybrid"
        vector_ranked = sorted(best_per_ref.values(), key=lambda item: -item["vector_score"])
        for rank, item in enumerate(vector_ranked):
            key = (item["ref_type"], item["ref_id"])
            existing = hits.get(key)
            if existing is None:
                existing = RetrievalHit(
                    ref_type=item["ref_type"],
                    ref_id=item["ref_id"],
                    chapter_number=item["chapter_number"],
                    title=item["ref_type"],
                    excerpt=_excerpt(item["excerpt"]),
                    status="",
                    channels=[],
                )
                hits[key] = existing
            existing.vector_score = item["vector_score"]
            if "vector" not in existing.channels:
                existing.channels.append("vector")
            existing.score += 1.0 / (RRF_K + rank + 1)
    if not hits:
        return {"query": query, "engine": "none", "channels": [], "hits": [], "warnings": warnings}
    if engine == "none":
        engine = "keyword"

    # ---- 融合打分：RRF 保底 + 类型加成（结构化证据优先于正文片段）
    for item in hits.values():
        item.score = item.score * 100 + TYPE_BONUS.get(item.ref_type, 0.0)
        if not item.title or item.title == item.ref_type:
            item.title = _title_from_ref(session, item)

    ranked = sorted(hits.values(), key=lambda item: -item.score)[:limit]
    used_channels = sorted({channel for item in ranked for channel in item.channels})
    return {
        "query": query,
        "engine": engine,
        "channels": used_channels,
        "hits": [item.to_dict() for item in ranked],
        "raw": ranked,
        "warnings": warnings,
    }


def _title_from_ref(session: Session, item: RetrievalHit) -> str:
    """向量通道单独命中时补一个可读标题。"""
    if item.ref_type == "CHAPTER":
        chapter = session.get(Chapter, item.ref_id)
        if chapter is not None:
            item.chapter_number = chapter.chapter_number
            return f"第{chapter.chapter_number}章 {chapter.title}"
        return "章节片段"
    if item.ref_type == "CANON_FACT":
        fact = session.get(CanonFact, item.ref_id)
        if fact is not None:
            item.chapter_number = fact.source_chapter
            item.status = fact.status
            item.excerpt = f"{fact.subject} {fact.predicate} {fact.object}［{fact.status}］"
            return f"{fact.subject}的{fact.predicate}"
    if item.ref_type == "TIMELINE":
        entry = session.get(TimelineEntry, item.ref_id)
        if entry is not None:
            item.chapter_number = entry.chapter_number
            item.status = entry.status
            return f"时间线：{entry.event}"
    if item.ref_type == "EVENT":
        event = session.get(Event, item.ref_id)
        if event is not None:
            item.chapter_number = event.chapter_number
            return f"事件（第{event.chapter_number}章）"
    if item.ref_type == "FORESHADOWING":
        record = session.get(Foreshadowing, item.ref_id)
        if record is not None:
            item.chapter_number = record.first_chapter
            return f"伏笔：{record.name}"
    if item.ref_type == "CHARACTER":
        character = session.get(Character, item.ref_id)
        if character is not None:
            item.chapter_number = character.first_appearance
            return f"人物：{character.name}"
    return item.ref_type


def semantic_chapters(
    session: Session, novel: Novel, query: str, *, limit: int = 5
) -> list[dict[str, Any]]:
    """只要正文片段（供 ChapterWriter 参考前文写法用）。"""
    hits = vector_service.vector_search(
        session, novel.id, query, ref_types=("CHAPTER",), limit=limit
    )
    results: list[dict[str, Any]] = []
    for item in hits:
        if item["vector_score"] < VECTOR_MIN_SCORE:
            continue
        chapter = session.get(Chapter, item["ref_id"])
        results.append(
            {
                "chapter_number": item["chapter_number"],
                "chapter_title": chapter.title if chapter else "",
                "excerpt": _excerpt(item["excerpt"], 300),
                "score": item["vector_score"],
            }
        )
    return results
