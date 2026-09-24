"""能力 1：能否正确提取事实（ExtractorAgent + PROPOSED 落库）。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import CanonFact, CanonStatus, Character, ExtractionItem, ItemReviewStatus
from app.services import extraction_service


def test_extract_chapter_produces_staged_items(session, novel, chapters):
    chapter = chapters[14]
    result, run, warnings, provider, model = extraction_service.run_extraction(
        session, novel, chapter, provider_name="offline"
    )
    session.commit()

    assert provider == "offline" and model
    assert run is not None and run.chapter_number == 14
    assert run.status == "PENDING_REVIEW"
    assert result.new_facts, "第 14 章应抽出佩剑相关事实"
    assert any(
        fact.subject == "林默" and fact.predicate == "佩剑" and fact.object == "赤霄剑"
        for fact in result.new_facts
    ), "应抽出「林默 佩剑 赤霄剑」"

    items = list(
        session.scalars(select(ExtractionItem).where(ExtractionItem.run_id == run.id))
    )
    kinds = {item.kind for item in items}
    assert "FACT" in kinds and "TIMELINE" in kinds and "CHARACTER_STATE" in kinds
    assert all(item.review_status == ItemReviewStatus.PENDING for item in items)


def test_extracted_fact_is_proposed_not_canon(session, novel, chapters):
    chapter = chapters[14]
    extraction_service.run_extraction(session, novel, chapter, provider_name="offline")
    session.commit()

    proposed = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.status == CanonStatus.PROPOSED,
            )
        )
    )
    objects = {fact.object for fact in proposed}
    assert "赤霄剑" in objects, "AI 抽出的事实只能以 PROPOSED 落库"

    canon = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.status == CanonStatus.CANON,
                CanonFact.subject == "林默",
                CanonFact.predicate == "佩剑",
            )
        )
    )
    assert [fact.object for fact in canon] == ["青霜剑"], "未确认前 Canon 中的佩剑必须仍是青霜剑"

    lynchpin = session.scalar(
        select(CanonFact).where(
            CanonFact.novel_id == novel.id, CanonFact.subject == "林默", CanonFact.predicate == "佩剑"
        )
    )
    assert lynchpin is not None and lynchpin.status == CanonStatus.CANON


def test_extract_of_clean_chapter_finds_no_weapon_change(session, novel, chapters):
    result, _, _, _, _ = extraction_service.run_extraction(
        session, novel, chapters[13], provider_name="offline"
    )
    session.commit()
    assert all(fact.object != "赤霄剑" for fact in result.new_facts)
    assert all(fact.predicate != "佩剑" for fact in result.new_facts)


def test_promote_after_user_confirmation(session, novel, chapters):
    extraction_service.run_extraction(session, novel, chapters[14], provider_name="offline")
    session.commit()
    proposed = session.scalar(
        select(CanonFact).where(
            CanonFact.novel_id == novel.id, CanonFact.object == "赤霄剑"
        )
    )
    assert proposed is not None

    outcome = extraction_service.promote_fact(session, proposed)
    session.commit()
    assert outcome["status"] == CanonStatus.CANON
    assert outcome["superseded"], "确认新佩剑后旧事实应被标记为 SUPERSEDED"

    old = session.get(CanonFact, outcome["superseded"][0])
    assert old is not None and old.status == CanonStatus.SUPERSEDED
    assert old.superseded_by == proposed.id


def test_character_state_extraction_is_grounded(session, novel, chapters):
    result, _, _, _, _ = extraction_service.run_extraction(
        session, novel, chapters[15], provider_name="offline"
    )
    session.commit()
    names = {change.name for change in result.characters_changed}
    assert "赵铁山" in names, "第 15 章赵铁山有动作与对白，应出现在 characters_changed"
    zhao = next(change for change in result.characters_changed if change.name == "赵铁山")
    assert zhao.action, "应记录具体动作"
    assert zhao.notes, "应保留原文依据"
    assert "按住" in (zhao.notes or "") + (zhao.action or "")

    known = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "赵铁山")
    )
    assert known is not None, "抽取到的人物应与库中人物对得上"
