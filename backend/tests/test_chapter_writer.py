"""能力 6：能否根据前文生成下一章（ChapterWriter）+ 写作前必须检索 Canon。"""

from __future__ import annotations

from sqlalchemy import select

from app.ai.base import AIResponse
from app.models import Chapter, GenerationRecord
from app.schemas import WriteChapterRequest
from app.services import extraction_service


def _writer():
    return extraction_service.build_agents("offline")["writer"]


def test_writer_retrieves_canon_before_writing(session, novel):
    writer = _writer()
    request = WriteChapterRequest(
        goals="林默离开青云镇前往断魂崖",
        must_include=["青霜剑"],
        forbidden=["赤霄剑"],
        characters=["林默", "苏月宁"],
        chapter_number=21,
        target_words=800,
    )
    response = writer.write(session, novel, request)
    session.commit()

    assert response.retrieved.canon_facts, "写作前必须检索到 Canon 事实"
    assert all(
        fact["status"] in ("CANON",) for fact in response.retrieved.canon_facts
    ), "写作上下文只能包含已确认的 CANON 事实"
    assert {item["name"] for item in response.retrieved.characters} >= {"林默", "苏月宁"}
    assert response.retrieved.foreshadowing, "应带上未回收伏笔"
    assert response.retrieved.timeline and response.retrieved.world_rules
    assert response.retrieved.recent_chapter_summaries, "应带上最近章节摘要"
    assert response.draft.word_count > 300


def test_writer_respects_must_include_and_forbidden(session, novel):
    writer = _writer()
    response = writer.write(
        session,
        novel,
        WriteChapterRequest(
            goals="林默在废祠外第一次正面遭遇血河教",
            must_include=["青霜剑", "废祠"],
            forbidden=["赤霄剑", "赵铁山"],
            characters=["林默"],
            chapter_number=21,
            target_words=900,
        ),
    )
    session.commit()
    body = response.draft.content
    assert "青霜剑" in body and "废祠" in body
    assert "赤霄剑" not in body
    assert "赵铁山" not in body
    assert any("赵铁山" in warning for warning in response.warnings), "剔除禁止内容应给出提示"


def test_must_include_only_checks_literal_items(session, novel):
    """规划给的「必须出现」多是描述性要求：拿去做字符串比较会让每章都报一堆「未找到」。"""
    from app.ai.agents.chapter_writer import _is_literal_requirement

    assert _is_literal_requirement("青霜剑") is True
    assert _is_literal_requirement("他不是没回来") is True
    assert _is_literal_requirement("老人先答前半句「他不是没回来」，被追问后才补上后半句") is False
    assert _is_literal_requirement("孩子围着老人听故事但要说清他没有名字") is False

    class _Stub:
        name = "stub"
        model = "stub"
        kind = "llm"
        supports_tools = False

        def __init__(self, body: str):
            self.body = body

        def generate(self, request):  # noqa: ANN001
            return AIResponse(text=self.body, parsed=None, provider=self.name, model=self.model)

    from app.ai.agents.chapter_writer import ChapterWriter

    request = WriteChapterRequest(
        goals="开篇",
        characters=["林默"],
        target_words=400,
        must_include=["青霜剑", "老人先答前半句，被追问后才补上后半句"],
    )
    present = ChapterWriter(_Stub("# 第1章 试\n\n他手里握着青霜剑。"))  # type: ignore[arg-type]
    response = present.write(session, novel, request)
    session.commit()
    assert not [w for w in response.warnings if "未找到必须出现的内容" in w], "字面项在正文里就不该报未找到"
    described = [w for w in response.warnings if "描述性要求" in w]
    assert described and "1 条" in described[0], "描述性项要单独说明，不逐字校验"

    missing = ChapterWriter(_Stub("# 第1章 试\n\n他空着手。"))  # type: ignore[arg-type]
    absent = missing.write(session, novel, request)
    session.commit()
    assert any("未找到必须出现的内容「青霜剑」" in w for w in absent.warnings), "字面项缺了要报"


def test_writer_does_not_write_canon_directly(session, novel):
    from app.models import CanonFact, CanonStatus

    before = len(
        list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)))
    )
    writer = _writer()
    writer.write(
        session,
        novel,
        WriteChapterRequest(goals="测试写作不落 Canon", characters=["林默"], target_words=400),
    )
    session.commit()
    after = list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)))
    assert len(after) == before, "写作不得直接产生 Canon 事实"
    assert all(fact.status != CanonStatus.CANON or fact.origin == "SEED" for fact in after)


def test_writer_can_save_draft_chapter(session, novel):
    writer = _writer()
    response = writer.write(
        session,
        novel,
        WriteChapterRequest(
            goals="保存草稿测试",
            must_include=["青云镇"],
            characters=["林默"],
            chapter_number=22,
            target_words=400,
            save=True,
        ),
    )
    session.commit()
    assert response.saved_chapter_id
    chapter = session.get(Chapter, response.saved_chapter_id)
    assert chapter is not None
    assert chapter.status == "DRAFT"
    assert chapter.chapter_number == 22
    assert "青云镇" in chapter.content

    record = session.scalar(
        select(GenerationRecord).where(GenerationRecord.id == response.generation_id)
    )
    assert record is not None
    assert record.retrieved, "生成记录应保留检索到的 Canon 快照"
    assert record.request["goals"] == "保存草稿测试"


def test_writer_next_chapter_number_matches_novel(session, novel):
    writer = _writer()
    response = writer.write(
        session,
        novel,
        WriteChapterRequest(goals="自动章号", characters=["林默"], target_words=400),
    )
    session.commit()
    assert response.draft.chapter_number == 21
