"""能力 3：能否找到故意设置的矛盾，且每条问题都带证据。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models import CanonFact, CanonStatus, Chapter
from app.services import continuity_service, extraction_service

#: 除故意埋设的第 14、15、18 章外，其余章节在离线规则下不应出现任何 error
CLEAN_CHAPTERS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16, 17]


def _run(session, novel, chapter, *, narrative_pass: bool = True):
    result, _, _, _, _ = extraction_service.run_extraction(
        session, novel, chapter, provider_name="offline"
    )
    report, _ = continuity_service.run_check(
        session, novel, chapter, provider_name="offline", extraction=result, narrative_pass=narrative_pass
    )
    session.commit()
    return report


def _codes(report) -> set[str]:
    return {issue.code for issue in report.errors}


def test_finds_attribute_conflict(session, novel, chapters):
    report = _run(session, novel, chapters[14])
    assert "FACT_CONFLICT" in _codes(report)
    issue = next(item for item in report.errors if item.code == "FACT_CONFLICT")
    assert "青霜剑" in issue.message and "赤霄剑" in issue.message
    sources = {evidence.source_chapter for evidence in issue.evidence}
    assert "第3章" in sources, "必须给出来源章节（Canon 侧）"
    assert "第14章" in sources, "必须给出来源章节（本章侧）"
    assert any(evidence.quote and "赤霄剑" in evidence.quote for evidence in issue.evidence)


def test_finds_dead_character_acting(session, novel, chapters):
    report = _run(session, novel, chapters[15])
    assert "DEAD_CHARACTER_ACTIVE" in _codes(report)
    issue = next(item for item in report.errors if item.code == "DEAD_CHARACTER_ACTIVE")
    assert "赵铁山" in issue.message
    sources = {evidence.source_chapter for evidence in issue.evidence}
    assert sources == {"第8章", "第15章"}
    quote = " ".join(evidence.quote or "" for evidence in issue.evidence)
    assert "按住" in quote, "证据应引用原文句子"


def test_finds_timeline_inversion(session, novel, chapters):
    report = _run(session, novel, chapters[18])
    assert "TIMELINE_INVERSION" in _codes(report)
    issue = next(item for item in report.errors if item.code == "TIMELINE_INVERSION")
    assert "天启三年四月初五" in issue.message and "天启三年三月" in issue.message
    sources = {evidence.source_chapter for evidence in issue.evidence}
    assert "第5章" in sources and "第18章" in sources


@pytest.mark.parametrize("number", CLEAN_CHAPTERS)
def test_clean_chapters_have_no_errors(session, novel, chapters, number):
    report = _run(session, novel, chapters[number])
    assert report.errors == [], f"第{number}章不应报出错误：{[i.message for i in report.errors]}"


def test_no_false_timeline_drift_on_dated_openings(session, novel, chapters):
    """各章开篇都有「天启三年X月Y日，地点。」，不得因此误报时间线漂移。"""
    for number in (1, 2, 3, 7, 9, 10):
        report = _run(session, novel, chapters[number])
        codes = {issue.code for issue in report.errors + report.warnings}
        assert "TIMELINE_DRIFT" not in codes, f"第{number}章出现误报的时间线漂移"
        assert "TIMELINE_INVERSION" not in codes


def test_grave_scene_is_resurrection_safe(session, novel, chapters):
    """第 9 章只有墓碑与追述，不得判为死者复现。"""
    report = _run(session, novel, chapters[9])
    assert "DEAD_CHARACTER_ACTIVE" not in _codes(report)
    assert "INCAPACITATED_CHARACTER_ACTIVE" not in _codes(report)


def test_every_issue_carries_evidence(session, novel, chapters):
    for number in (14, 15, 18):
        report = _run(session, novel, chapters[number])
        for issue in report.errors + report.warnings:
            assert issue.evidence, f"{issue.code} 缺少证据"
            assert all(evidence.source_chapter.strip() for evidence in issue.evidence)


def test_unevidenced_model_candidate_is_dropped(session, novel, chapters):
    """离线提供者的叙事通道故意返回一条无证据候选，应被丢弃而不是展示给作者。"""
    report = _run(session, novel, chapters[16], narrative_pass=True)
    assert report.dropped_issues, "无证据的模型候选应记入 dropped_issues（便于审计）"
    shown = [issue.code for issue in report.errors + report.warnings]
    assert "MODEL_UNEVIDENCED" not in shown, "无证据候选不得出现在给作者看的问题列表里"
    assert all("缺少可核验的证据" in item["reason"] for item in report.dropped_issues)
    assert report.errors == []


def test_report_is_persisted(session, novel, chapters):
    report = _run(session, novel, chapters[15])
    assert report.report_id
    latest = continuity_service.latest_report(session, novel.id, chapters[15].id)
    assert latest is not None and latest.id == report.report_id
    assert latest.errors and latest.errors[0]["code"] == "DEAD_CHARACTER_ACTIVE"


def test_incapacity_conflict_resolved_by_confirmed_state_change(session, novel, chapters):
    """第 19 章正文写了王烈苏醒；确认状态变更后，第 20 章不应再报「无法行动者出现动作」。"""
    report_20 = _run(session, novel, chapters[20])
    assert "INCAPACITATED_CHARACTER_ACTIVE" in _codes(report_20)

    report_19 = _run(session, novel, chapters[19])
    assert report_19.errors == [], "第 19 章只应给出状态变更提示"
    assert "STATE_CHANGE_UNCONFIRMED" in {issue.code for issue in report_19.warnings}

    from app.models import ExtractionRun

    run = session.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.chapter_id == chapters[19].id)
        .order_by(ExtractionRun.created_at.desc())
    )
    assert run is not None
    from app.services import extraction_service as es

    applied = es.apply_run(session, novel, run, accept_pending=True)
    session.commit()
    assert applied.applied, "第 19 章应至少写入一条（人物状态）"

    wang = session.scalar(
        select(CanonFact).where(CanonFact.novel_id == novel.id, CanonFact.subject == "王烈")
    )
    assert wang is not None

    report_20_after = _run(session, novel, chapters[20])
    assert "INCAPACITATED_CHARACTER_ACTIVE" not in _codes(report_20_after), (
        "状态变更确认后，第 20 章不应再报同一问题"
    )


def test_duplicate_fact_is_warning_not_error(session, novel, chapters):
    """把与 Canon 完全一致的事实写成 PROPOSED，只应提示重复。"""
    from app.services import query_service

    query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="佩剑",
        object_value="青霜剑",
        source_chapter=13,
        origin="AI_TOOL",
    )
    session.commit()
    report = _run(session, novel, chapters[13])
    assert "DUPLICATE_FACT" in {issue.code for issue in report.warnings}
    assert report.errors == []


def test_canon_count_unchanged_by_check(session, novel, chapters):
    before = len(
        list(
            session.scalars(
                select(CanonFact).where(
                    CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.CANON
                )
            )
        )
    )
    _run(session, novel, chapters[15])
    after = len(
        list(
            session.scalars(
                select(CanonFact).where(
                    CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.CANON
                )
            )
        )
    )
    assert before == after, "审校过程不得改动 Canon"


def test_chapter_content_untouched(session, novel, chapters):
    chapter = session.get(Chapter, chapters[15].id)
    original = chapter.content  # type: ignore[union-attr]
    _run(session, novel, chapters[15])
    session.refresh(chapter)
    assert chapter.content == original, "审校不得改写作者正文"
