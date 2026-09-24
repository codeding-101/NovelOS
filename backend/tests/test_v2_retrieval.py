"""V0.2 语义记忆：切块、向量索引、混合检索。"""

from __future__ import annotations

import math

from sqlalchemy import select

from app.ai.embeddings import LocalHashingEmbedding
from app.models import Chapter, EmbeddingRecord
from app.services import retrieval_service, vector_service


def test_chunking_is_paragraph_aware():
    content = "\n\n".join(["第一段" * 40, "第二段" * 40, "第三段" * 40])
    chunks = vector_service.chunk_content(content, size=200, overlap=40)
    assert len(chunks) >= 3
    assert all(chunk.strip() for chunk in chunks)
    assert all(len(chunk) <= 320 for chunk in chunks), "切块不应远超声明的大小"
    long_paragraph = "句子。" * 200
    single = vector_service.chunk_content(long_paragraph, size=300, overlap=50)
    assert len(single) > 1, "超长段落应按句子继续切分"


def test_local_embedding_is_deterministic_and_normalized():
    provider = LocalHashingEmbedding(dim=128)
    first = provider.embed(["林默握着青霜剑"])[0]
    second = provider.embed(["林默握着青霜剑"])[0]
    assert first == second, "本地向量必须可复现（离线测试依赖这一点）"
    assert len(first) == 128
    assert math.isclose(math.sqrt(sum(value * value for value in first)), 1.0, rel_tol=1e-6)
    assert provider.embed(["完全不同的句子"])[0] != first


def test_index_novel_builds_chapter_and_entity_vectors(session, novel):
    stats = vector_service.index_stats(session, novel.id)
    assert stats["records"] > 0
    assert stats["by_ref_type"]["CHAPTER"] > 20, "20 章应切成多个片段"
    for ref_type in ("CANON_FACT", "TIMELINE", "EVENT", "FORESHADOWING", "CHARACTER"):
        assert stats["by_ref_type"].get(ref_type, 0) > 0
    assert stats["providers"] == ["local-ngram"]
    assert stats["dim"] == 384


def test_reindex_is_idempotent(session, novel):
    before = vector_service.index_stats(session, novel.id)["records"]
    vector_service.index_novel(session, novel.id)
    session.commit()
    after = vector_service.index_stats(session, novel.id)["records"]
    assert after == before, "重建索引不应重复堆积记录"


def test_editing_chapter_replaces_its_chunks(session, novel):
    chapter = session.scalar(
        select(Chapter).where(Chapter.novel_id == novel.id, Chapter.chapter_number == 3)
    )
    from app.schemas import ChapterUpdate
    from app.services import chapter_service

    vector_service.index_chapter(session, novel.id, chapter)
    before = vector_service.index_stats(session, novel.id)["records"]
    chapter_service.update_chapter(
        session, novel, chapter, ChapterUpdate(content="这一章被替换成了全新的内容，只提到玄铁令。")
    )
    session.commit()
    after = vector_service.index_stats(session, novel.id)["records"]
    assert after <= before + 1, "章节内容变更后旧片段应被替换而不是累积"
    hits = vector_service.vector_search(session, novel.id, "玄铁令", ref_types=("CHAPTER",), limit=3)
    assert hits, "新写入的正文应立刻可以被向量检索到"
    assert hits[0]["chapter_number"] == 3


def test_vector_search_scores_relevant_above_threshold(session, novel):
    hits = vector_service.vector_search(session, novel.id, "青霜剑", limit=5)
    assert hits
    assert hits[0]["vector_score"] >= retrieval_service.VECTOR_ENTITY_MIN_SCORE
    assert hits[0]["ref_type"] in ("CANON_FACT", "EVENT", "TIMELINE", "CHAPTER")

    weak = vector_service.vector_search(session, novel.id, "量子计算机与区块链", limit=5)
    assert all(item["vector_score"] < retrieval_service.VECTOR_ENTITY_MIN_SCORE for item in weak)


def test_hybrid_uses_both_channels_for_keyword_query(session, novel):
    result = retrieval_service.hybrid_search(session, novel, "青霜剑", limit=8)
    assert result["engine"] == "hybrid"
    assert "keyword" in result["channels"] and "vector" in result["channels"]
    dual = [hit for hit in result["hits"] if len(hit["channels"]) == 2]
    assert dual, "关键词与向量都应命中同一批设定条目"
    assert dual[0]["keyword_score"] > 0 and dual[0]["vector_score"] > 0
    for hit in result["hits"]:
        assert hit["score"] > 0


def test_hybrid_can_answer_with_vector_only(session, novel):
    """「他师傅送他那柄剑的来历」这种说法正文里没有，关键词通道会落空，向量通道仍能召回。"""
    query = "他师傅送他那柄剑的来历"
    keyword_only = retrieval_service.hybrid_search(session, novel, query, limit=6, use_vector=False)
    hybrid = retrieval_service.hybrid_search(session, novel, query, limit=6)
    assert not keyword_only["hits"], "该问法不应命中关键词通道"
    assert hybrid["hits"], "向量通道应召回相关正文片段"
    assert hybrid["channels"] == ["vector"]
    assert all(hit["keyword_score"] == 0 for hit in hybrid["hits"])


def test_structured_evidence_outranks_prose_snippets(session, novel):
    result = retrieval_service.hybrid_search(session, novel, "青霜剑", limit=8)
    types = [hit["ref_type"] for hit in result["hits"]]
    assert types[0] == "CANON_FACT", "设定类证据应排在正文片段之前"


def test_semantic_chapters_returns_excerpts(session, novel):
    hits = retrieval_service.semantic_chapters(session, novel, "师父赠剑与三条剑规", limit=4)
    assert hits
    assert all(item["chapter_number"] for item in hits)
    assert any("剑" in item["excerpt"] for item in hits)


def test_vector_records_stored_as_binary(session, novel):
    record = session.scalar(
        select(EmbeddingRecord).where(EmbeddingRecord.novel_id == novel.id).limit(1)
    )
    assert record is not None
    assert isinstance(record.vector, (bytes, bytearray))
    assert len(record.vector) == record.dim * 4, "向量按 float32 存储，长度应为 dim×4"
