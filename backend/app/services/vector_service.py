"""向量索引与相似度检索。

- 章节按段落切成 ~400 字的片段入库（带重叠，避免跨段语义被切断）；
- Canon 事实、时间线、事件、伏笔、人物档各作为一条记录；
- 向量以 float32 二进制保存，检索时一次性读出做余弦相似度；
  装了 numpy 走矩阵运算（百万字量级仍在毫秒级），没装则用纯 Python 回退。
"""

from __future__ import annotations

import re
from array import array
from typing import Any, Iterable

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.ai.embeddings import EmbeddingProvider, build_embedding_provider
from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Character,
    EmbeddingRecord,
    Event,
    Foreshadowing,
    Fragment,
    TimelineEntry,
)

CHUNK_SIZE = 400
CHUNK_OVERLAP = 80
ENTITY_REF_TYPES = (
    "CANON_FACT",
    "TIMELINE",
    "EVENT",
    "FORESHADOWING",
    "CHARACTER",
    "FRAGMENT",
)

try:  # pragma: no cover - 取决于环境是否装了 numpy
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None


def chunk_content(content: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """按段落聚合切块，段落过长时按句号切分。"""
    paragraphs = [text.strip() for text in re.split(r"\n+", content or "") if text.strip()]
    chunks: list[str] = []
    buffer = ""
    for paragraph in paragraphs:
        candidate = f"{buffer}\n{paragraph}".strip() if buffer else paragraph
        if len(candidate) <= size:
            buffer = candidate
            continue
        if buffer:
            chunks.append(buffer)
        if len(paragraph) <= size:
            buffer = paragraph
            continue
        sentences = re.split(r"(?<=[。！？!?…])", paragraph)
        piece = ""
        for sentence in sentences:
            if len(piece) + len(sentence) > size and piece:
                chunks.append(piece)
                piece = piece[-overlap:] if overlap else ""
            piece += sentence
        buffer = piece.strip()
    if buffer:
        chunks.append(buffer)
    return [chunk for chunk in chunks if chunk.strip()]


def pack_vector(vector: Iterable[float]) -> bytes:
    return array("f", [float(value) for value in vector]).tobytes()


def unpack_vector(blob: bytes | None) -> list[float]:
    if not blob:
        return []
    values = array("f")
    values.frombytes(blob)
    return list(values)


def cosine(query: list[float], vector: list[float]) -> float:
    if not query or not vector or len(query) != len(vector):
        return 0.0
    dot = sum(left * right for left, right in zip(query, vector))
    return max(0.0, dot)  # 两侧都已归一化


def _provider(name: str | None = None) -> tuple[EmbeddingProvider, list[str]]:
    return build_embedding_provider(name)


def _write_records(
    session: Session,
    novel_id: str,
    ref_type: str,
    ref_id: str,
    texts: list[str],
    provider: EmbeddingProvider,
    chapter_number: int | None,
) -> int:
    session.execute(
        delete(EmbeddingRecord).where(
            EmbeddingRecord.novel_id == novel_id,
            EmbeddingRecord.ref_type == ref_type,
            EmbeddingRecord.ref_id == ref_id,
        )
    )
    if not texts:
        return 0
    vectors = provider.embed(texts)
    for index, (text, vector) in enumerate(zip(texts, vectors)):
        session.add(
            EmbeddingRecord(
                novel_id=novel_id,
                ref_type=ref_type,
                ref_id=ref_id,
                chunk_index=index,
                chapter_number=chapter_number,
                text=text[:2000],
                vector=pack_vector(vector),
                dim=len(vector),
                provider=provider.name,
            )
        )
    return len(texts)


def index_records(
    session: Session,
    novel_id: str,
    ref_type: str,
    ref_id: str,
    texts: list[str],
    chapter_number: int | None = None,
    provider: EmbeddingProvider | None = None,
) -> int:
    """把任意一类记录的文本片段写进向量索引（对既有片段是替换语义）。"""
    provider = provider or _provider()[0]
    return _write_records(session, novel_id, ref_type, ref_id, texts, provider, chapter_number)


def index_chapter(
    session: Session, novel_id: str, chapter: Chapter, provider: EmbeddingProvider | None = None
) -> int:
    provider = provider or _provider()[0]
    return _write_records(
        session,
        novel_id,
        "CHAPTER",
        chapter.id,
        chunk_content(chapter.content or ""),
        provider,
        chapter.chapter_number,
    )


def remove_ref(session: Session, novel_id: str, ref_type: str, ref_id: str) -> None:
    session.execute(
        delete(EmbeddingRecord).where(
            EmbeddingRecord.novel_id == novel_id,
            EmbeddingRecord.ref_type == ref_type,
            EmbeddingRecord.ref_id == ref_id,
        )
    )


def index_entities(session: Session, novel_id: str, provider: EmbeddingProvider | None = None) -> dict[str, int]:
    """重建设定类向量（事实、时间线、事件、伏笔、人物）。数量不大，整体重建最简单可靠。"""
    provider = provider or _provider()[0]
    counts: dict[str, int] = {}

    facts = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel_id,
                CanonFact.status.in_((CanonStatus.CANON, CanonStatus.SUPERSEDED)),
            )
        )
    )
    for fact in facts:
        text = f"{fact.subject}的{fact.predicate}是{fact.object}"
        if fact.note:
            text += f"（{fact.note}）"
        _write_records(session, novel_id, "CANON_FACT", fact.id, [text], provider, fact.source_chapter)
    counts["CANON_FACT"] = len(facts)

    timeline = list(session.scalars(select(TimelineEntry).where(TimelineEntry.novel_id == novel_id)))
    for entry in timeline:
        text = f"{entry.story_time}｜{entry.event}｜{entry.location}｜{entry.description}"
        _write_records(session, novel_id, "TIMELINE", entry.id, [text], provider, entry.chapter_number)
    counts["TIMELINE"] = len(timeline)

    events = list(session.scalars(select(Event).where(Event.novel_id == novel_id)))
    for event in events:
        text = (
            f"{event.time or ''}｜{event.location}｜{'、'.join(event.characters or [])}｜"
            f"{event.description}｜{event.consequences}"
        )
        _write_records(session, novel_id, "EVENT", event.id, [text], provider, event.chapter_number)
    counts["EVENT"] = len(events)

    foreshadowings = list(
        session.scalars(select(Foreshadowing).where(Foreshadowing.novel_id == novel_id))
    )
    for item in foreshadowings:
        text = f"伏笔：{item.name}｜{item.description}｜期望回收：{item.expected_payoff}"
        _write_records(
            session, novel_id, "FORESHADOWING", item.id, [text], provider, item.first_chapter
        )
    counts["FORESHADOWING"] = len(foreshadowings)

    characters = list(session.scalars(select(Character).where(Character.novel_id == novel_id)))
    for character in characters:
        text = (
            f"{character.name}｜{character.description}｜性格：{character.personality}｜"
            f"背景：{character.background}｜状态：{character.current_status}｜"
            f"位置：{character.current_location}"
        )
        _write_records(
            session,
            novel_id,
            "CHARACTER",
            character.id,
            [text],
            provider,
            character.first_appearance,
        )
    counts["CHARACTER"] = len(characters)

    fragments = list(session.scalars(select(Fragment).where(Fragment.novel_id == novel_id)))
    for fragment in fragments:
        text = "｜".join(
            part for part in (fragment.title, fragment.text, fragment.intent) if part
        )
        _write_records(
            session, novel_id, "FRAGMENT", fragment.id, [text], provider, fragment.target_chapter
        )
    counts["FRAGMENT"] = len(fragments)
    return counts


def index_novel(
    session: Session, novel_id: str, provider_name: str | None = None
) -> dict[str, Any]:
    provider, warnings = _provider(provider_name)
    chapters = list(session.scalars(select(Chapter).where(Chapter.novel_id == novel_id)))
    total_chunks = 0
    for chapter in chapters:
        total_chunks += index_chapter(session, novel_id, chapter, provider)
    counts = index_entities(session, novel_id, provider)
    counts["CHAPTER"] = len(chapters)
    session.flush()
    return {
        "provider": provider.name,
        "dim": provider.dim,
        "chapters": len(chapters),
        "chapter_chunks": total_chunks,
        "entities": counts,
        "warnings": warnings,
    }


def index_stats(session: Session, novel_id: str) -> dict[str, Any]:
    rows = list(session.scalars(select(EmbeddingRecord).where(EmbeddingRecord.novel_id == novel_id)))
    by_type: dict[str, int] = {}
    providers: set[str] = set()
    for row in rows:
        by_type[row.ref_type] = by_type.get(row.ref_type, 0) + 1
        providers.add(row.provider)
    return {
        "records": len(rows),
        "by_ref_type": by_type,
        "providers": sorted(providers),
        "dim": rows[0].dim if rows else 0,
    }


def vector_search(
    session: Session,
    novel_id: str,
    query: str,
    *,
    ref_types: tuple[str, ...] | None = None,
    limit: int = 10,
    provider_name: str | None = None,
) -> list[dict[str, Any]]:
    if not (query or "").strip():
        return []
    provider, warnings = _provider(provider_name)
    if not provider.describe().get("available", True):
        return []
    query_vector = provider.embed([query])[0]

    stmt = select(EmbeddingRecord).where(EmbeddingRecord.novel_id == novel_id)
    if ref_types:
        stmt = stmt.where(EmbeddingRecord.ref_type.in_(ref_types))
    rows = list(session.scalars(stmt))
    if not rows:
        return []

    results: list[dict[str, Any]] = []
    if _np is not None:
        usable = [row for row in rows if len(row.vector or b"") == len(query_vector) * 4]
        if usable:
            matrix = _np.frombuffer(b"".join(row.vector for row in usable), dtype=_np.float32)
            matrix = matrix.reshape(len(usable), -1)
            query_array = _np.asarray(query_vector, dtype=_np.float32)
            scores = matrix @ query_array
            order = _np.argsort(-scores)[:limit]
            for index in order:
                row = usable[int(index)]
                results.append(_hit(row, float(scores[int(index)])))
    else:
        scored = [
            (cosine(query_vector, unpack_vector(row.vector)), row)
            for row in rows
            if len(row.vector or b"") == len(query_vector) * 4
        ]
        scored.sort(key=lambda item: -item[0])
        results = [_hit(row, score) for score, row in scored[:limit]]

    for item in results:
        item["warnings"] = warnings
    return results


def _hit(row: EmbeddingRecord, score: float) -> dict[str, Any]:
    return {
        "ref_type": row.ref_type,
        "ref_id": row.ref_id,
        "chapter_number": row.chapter_number,
        "chunk_index": row.chunk_index,
        "excerpt": row.text,
        "vector_score": round(float(score), 4),
    }
