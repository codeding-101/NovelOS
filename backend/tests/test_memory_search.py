"""能力 2：能否正确查询前文（MemorySearch），以及 PROPOSED 不得被当成 CANON。"""

from __future__ import annotations

from app.services import extraction_service


def _memory(novel_id: str | None = None):
    agents = extraction_service.build_agents("offline")
    return agents["memory"]


def test_example_question_returns_evidence(session, novel):
    memory = _memory()
    response = memory.ask(session, novel, "林默什么时候第一次见到王烈？")
    session.commit()

    assert response.confidence == "HIGH"
    assert "三月十五" in response.answer, "答案应给出首次相遇的时间"
    assert "第2章" in response.answer or "（第2章）" in response.answer, "答案应标注来源章节"
    chapters = {item.chapter_number for item in response.evidence}
    assert 2 in chapters, "证据应包含第 2 章"
    assert all(item.excerpt for item in response.evidence)


def test_question_about_death_uses_canon(session, novel):
    memory = _memory()
    response = memory.ask(session, novel, "赵铁山是怎么死的？")
    session.commit()
    assert response.confidence in ("HIGH", "MEDIUM")
    assert "血无痕" in response.answer or "死亡" in response.answer
    assert any(item.ref_type == "CANON_FACT" for item in response.evidence)
    assert all(item.chapter_number for item in response.evidence if item.ref_type == "CANON_FACT")


def test_unknown_question_is_declared_unknown(session, novel):
    memory = _memory()
    response = memory.ask(session, novel, "林默的师父喜欢吃什么？")
    session.commit()
    assert response.confidence == "LOW"
    assert "没有任何记载" in response.answer or "UNKNOWN" in response.answer
    assert any("查不到任何记载" in warning for warning in response.warnings)


def test_question_without_any_evidence_returns_unknown(session, novel):
    memory = _memory()
    response = memory.ask(session, novel, "第三章节里那只猫叫什么名字？")
    session.commit()
    assert response.confidence == "UNKNOWN"
    assert response.answer.startswith("UNKNOWN")
    assert not any(
        item.ref_type == "CANON_FACT" for item in response.evidence
    ), "不得拿无关的 Canon 事实充当答案"
    assert any("没有记载" in warning or "不进行推测" in warning for warning in response.warnings)


def test_proposed_fact_is_not_presented_as_canon(session, novel, chapters):
    """第 14 章抽出的赤霄剑是 PROPOSED，问答必须同时给出 Canon 的青霜剑并声明未确认。"""
    extraction_service.run_extraction(session, novel, chapters[14], provider_name="offline")
    session.commit()

    memory = _memory()
    response = memory.ask(session, novel, "林默的佩剑是什么？")
    session.commit()

    assert "青霜剑" in response.answer, "必须给出 Canon 中的青霜剑"
    assert "赤霄剑" in response.answer, "应提到存在候选信息"
    assert any(
        marker in response.answer for marker in ("尚未确认", "未确认", "PROPOSED", "待确认")
    ), "涉及 PROPOSED 时必须声明未确认状态"
    assert response.confidence != "HIGH" or "尚未确认" in response.answer


def test_hallucinated_citation_is_rewritten(session, novel):
    """模型若引用未在证据中出现的章节，应改用证据摘要并记 warning。"""
    from app.ai.base import AIResponse
    from app.ai.agents.memory_search import MemorySearch
    from app.schemas import AskResponse, EvidenceItem

    class LyingProvider:
        name = "stub"
        model = "stub-model"
        kind = "llm"

        def generate(self, request):  # noqa: ANN001
            return AIResponse(
                text='{"answer": "林默的佩剑在第99章被换成了别的东西。", "confidence": "HIGH"}',
                parsed={"answer": "林默的佩剑在第99章被换成了别的东西。", "confidence": "HIGH"},
                provider=self.name,
                model=self.model,
            )

    memory = MemorySearch(LyingProvider())  # type: ignore[arg-type]
    response = memory.ask(session, novel, "林默的佩剑是什么？", persist=False)
    assert "第99章" not in response.answer
    assert any("未在证据中出现的章节" in warning for warning in response.warnings)
    assert response.confidence in ("MEDIUM", "LOW", "HIGH", "UNKNOWN")
    assert isinstance(response, AskResponse)
    assert all(isinstance(item, EvidenceItem) for item in response.evidence)


def test_queries_are_audited(session, novel):
    from sqlalchemy import select

    from app.models import MemoryQuery

    memory = _memory()
    memory.ask(session, novel, "苏月宁是谁？")
    session.commit()
    rows = list(session.scalars(select(MemoryQuery).where(MemoryQuery.novel_id == novel.id)))
    assert rows, "问答应留审计记录"
    assert rows[-1].evidence, "审计记录应包含证据快照"
