"""Bounded lexical retrieval of current, source-valid canonical memory."""

import re
import unicodedata
from fastapi import HTTPException
from sqlalchemy import exists, or_, select
from app.platform import memory_service
from app.platform.models import (
    Asset,
    AssetRevision,
    MemoryEntry,
    MemoryProposal,
    MemorySource,
    MemoryStoryState,
)
from app.platform.works import require_work

SCAN_LIMIT = 1000


def normalize(value):
    return unicodedata.normalize("NFKC", value).casefold()


def terms(value):
    result = set()
    for part in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", normalize(value)):
        if "\u3400" <= part[0] <= "\u9fff":
            result.update(part[i : i + 2] for i in range(len(part) - 1))
            if len(part) == 1:
                result.add(part)
        else:
            result.add(part)
    return result


def score(query, title, content, key):
    wanted = terms(query)
    title_hits = wanted & terms(title + " " + key)
    content_hits = wanted & terms(content)
    # Presence, not frequency: repeated prose cannot inflate lexical relevance.
    value = 3 * len(title_hits) + len(content_hits)
    phrase = normalize(query).strip()
    if wanted and phrase in normalize(title + " " + key):
        value += 6
    return value, sorted(title_hits | content_hits)


def retrieve(session, work_id, query, *, limit=20, at=None):
    require_work(session, work_id)
    query = query.strip()
    if not query or len(query) > 256 or not terms(query):
        raise HTTPException(
            422, "请输入包含中文、英文字母或数字的检索词，最多 256 字符"
        )
    if not 1 <= limit <= 50:
        raise HTTPException(422, "结果数量须为 1 至 50")
    invalid_source = exists(
        select(MemorySource.proposal_id)
        .join(AssetRevision, AssetRevision.id == MemorySource.revision_id)
        .join(Asset, Asset.id == AssetRevision.asset_id)
        .where(
            MemorySource.proposal_id == MemoryProposal.id,
            Asset.head_version != AssetRevision.number,
        )
    )
    statement = (
        select(MemoryProposal)
        .join(MemoryEntry, MemoryEntry.proposal_id == MemoryProposal.id)
        .where(MemoryEntry.work_id == work_id, ~invalid_source)
    )
    if at is not None:
        statement = statement.outerjoin(
            MemoryStoryState, MemoryStoryState.proposal_id == MemoryProposal.id
        ).where(
            or_(
                MemoryStoryState.proposal_id.is_(None),
                (MemoryStoryState.valid_from <= at)
                & or_(
                    MemoryStoryState.valid_until.is_(None),
                    MemoryStoryState.valid_until > at,
                ),
            )
        )
    rows = list(
        session.scalars(
            statement.order_by(MemoryProposal.entry_key).limit(SCAN_LIMIT + 1)
        )
    )
    if len(rows) > SCAN_LIMIT:
        raise HTTPException(
            409, "有效记忆超过当前检索上限，请使用字面检索；本次未返回不完整排名"
        )
    ranked = []
    for row in rows:
        value, matched = score(query, row.title, row.content, row.entry_key)
        if value:
            ranked.append((value, row.entry_key, row, matched))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return {
        "algorithm": "lexical_zh_bigram_v1",
        "query": query,
        "scanned": len(rows),
        "scan_limit": SCAN_LIMIT,
        "matched": len(ranked),
        "items": [
            {
                "memory": memory_service.data(session, row),
                "score": value,
                "matched_terms": matched,
            }
            for value, _, row, matched in ranked[:limit]
        ],
    }
