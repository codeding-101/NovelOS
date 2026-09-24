"""章节全文检索。

优先使用 SQLite FTS5 的 trigram 分词器（对中文子串检索友好，SQLite >= 3.34），
当索引不可用或查询词短于 3 字时回退为 LIKE 扫描。接口固定，后续替换为
向量检索 / RAG 时只需替换本模块实现。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.models import Chapter

FTS_TABLE = "chapter_search_fts"
_TOKEN_SPLIT = re.compile(r"[\s,，。？?！!、；;：:]+")


def _fts_ddl() -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
        "USING fts5(chapter_id UNINDEXED, novel_id UNINDEXED, chapter_number UNINDEXED, "
        "title, content, tokenize='trigram')"
    )


def fts_available(session: Session) -> bool:
    try:
        session.execute(text(_fts_ddl()))
        return True
    except Exception:  # noqa: BLE001 - 任何 DDL 失败都退回 LIKE
        return False


def tokenize(query: str) -> list[str]:
    return [token for token in _TOKEN_SPLIT.split(query or "") if token]


def index_chapter(session: Session, chapter: Chapter) -> None:
    if not fts_available(session):
        return
    session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE chapter_id = :cid"), {"cid": chapter.id})
    session.execute(
        text(
            f"INSERT INTO {FTS_TABLE}(chapter_id, novel_id, chapter_number, title, content) "
            "VALUES (:cid, :nid, :num, :title, :content)"
        ),
        {
            "cid": chapter.id,
            "nid": chapter.novel_id,
            "num": chapter.chapter_number,
            "title": chapter.title or "",
            "content": chapter.content or "",
        },
    )


def remove_chapter(session: Session, chapter_id: str) -> None:
    if not fts_available(session):
        return
    session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE chapter_id = :cid"), {"cid": chapter_id})


def rebuild_index(session: Session, novel_id: str | None = None) -> int:
    if not fts_available(session):
        return 0
    if novel_id:
        session.execute(text(f"DELETE FROM {FTS_TABLE} WHERE novel_id = :nid"), {"nid": novel_id})
        stmt = select(Chapter).where(Chapter.novel_id == novel_id)
    else:
        session.execute(text(f"DELETE FROM {FTS_TABLE}"))
        stmt = select(Chapter)
    chapters = list(session.scalars(stmt))
    for chapter in chapters:
        index_chapter(session, chapter)
    return len(chapters)


def _snippet(content: str, needles: list[str], width: int = 60) -> str:
    for needle in needles:
        index = content.find(needle)
        if index >= 0:
            start = max(0, index - width // 2)
            end = min(len(content), index + len(needle) + width)
            prefix = "…" if start > 0 else ""
            suffix = "…" if end < len(content) else ""
            return f"{prefix}{content[start:end]}{suffix}"
    return content[:width] + ("…" if len(content) > width else "")


def _score(chapter: Chapter, tokens: list[str], raw_query: str) -> float:
    title = chapter.title or ""
    content = chapter.content or ""
    score = 0.0
    if raw_query and raw_query in title:
        score += 8.0
    for token in tokens:
        score += title.count(token) * 5.0
        score += min(content.count(token), 20) * 1.0
    if raw_query and raw_query in content:
        score += 3.0
    return score


def _fts_candidates(session: Session, novel_id: str, tokens: list[str]) -> list[str] | None:
    if not all(len(token) >= 3 for token in tokens):
        return None
    if not fts_available(session):
        return None
    phrase = " AND ".join('"' + token.replace('"', "") + '"' for token in tokens)
    try:
        rows = session.execute(
            text(
                f"SELECT chapter_id FROM {FTS_TABLE} "
                f"WHERE {FTS_TABLE} MATCH :phrase AND novel_id = :nid LIMIT 200"
            ),
            {"phrase": phrase, "nid": novel_id},
        ).fetchall()
    except Exception:  # noqa: BLE001 - FTS 查询语法异常时退回 LIKE
        return None
    return [row[0] for row in rows]


def search_chapters(
    session: Session, novel_id: str, query: str, limit: int = 10
) -> dict[str, Any]:
    """返回 {query, engine, hits:[{chapter_id, chapter_number, title, snippet, score, match_source}]}"""
    tokens = tokenize(query)
    if not tokens:
        return {"query": query, "engine": "none", "hits": []}
    raw_query = query.strip()

    candidate_ids = _fts_candidates(session, novel_id, tokens)
    engine = "fts5-trigram" if candidate_ids is not None else "like"

    if candidate_ids is not None:
        if not candidate_ids:
            return {"query": query, "engine": engine, "hits": []}
        stmt = select(Chapter).where(Chapter.id.in_(candidate_ids))
    else:
        stmt = select(Chapter).where(Chapter.novel_id == novel_id)
        for token in tokens:
            stmt = stmt.where(Chapter.content.like(f"%{token}%"))
    chapters = list(session.scalars(stmt))

    hits: list[dict[str, Any]] = []
    for chapter in chapters:
        if chapter.novel_id != novel_id:
            continue
        score = _score(chapter, tokens, raw_query)
        if score <= 0:
            continue
        match_source = "title" if any(token in (chapter.title or "") for token in tokens) else "content"
        hits.append(
            {
                "chapter_id": chapter.id,
                "chapter_number": chapter.chapter_number,
                "title": chapter.title or "",
                "snippet": _snippet(chapter.content or "", [raw_query, *tokens]),
                "score": round(score, 2),
                "match_source": match_source,
            }
        )
    hits.sort(key=lambda item: (-item["score"], item["chapter_number"]))
    return {"query": query, "engine": engine, "hits": hits[:limit]}
