"""章节完成工作流（需求第六节的 15 步）与审校应用流程。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import CanonFact, CanonStatus, Chapter, ExtractionItem, ItemReviewStatus
from app.services import workflow_service


def test_workflow_runs_steps_1_to_10(session, novel, chapters):
    chapter = chapters[14]
    report = workflow_service.complete_chapter(session, novel, chapter, provider_name="offline")
    session.commit()

    assert [step.step for step in report.steps] == list(range(1, 12))
    assert report.steps[0].name == "保存正文"
    assert report.steps[0].status == "ok"
    assert report.steps[-1].name == "运行 ContinuityChecker"
    assert report.steps[-1].status == "error"
    assert any(step.name == "提取承诺" for step in report.steps), "V0.3 起工作流包含承诺账本抽取"
    assert report.errors >= 1
    assert report.pending_items > 0
    assert report.extraction_run is not None
    assert report.continuity is not None
    assert "第 11—15 步" in report.message

    session.refresh(chapter)
    assert chapter.status == "COMPLETED"


def test_workflow_does_not_touch_canon_before_confirmation(session, novel, chapters):
    workflow_service.complete_chapter(session, novel, chapters[14], provider_name="offline")
    session.commit()
    canon_weapons = [
        fact.object
        for fact in session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.subject == "林默",
                CanonFact.predicate == "佩剑",
                CanonFact.status == CanonStatus.CANON,
            )
        )
    ]
    assert canon_weapons == ["青霜剑"], "工作流跑完也不得自动改 Canon"
    proposed = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.PROPOSED
            )
        )
    )
    assert any(fact.object == "赤霄剑" for fact in proposed)


def test_steps_11_to_15_after_author_confirms(session, novel, chapters):
    chapters = {number: session.get(Chapter, chapter.id) for number, chapter in chapters.items()}
    report = workflow_service.complete_chapter(session, novel, chapters[14], provider_name="offline")
    session.commit()
    run = report.extraction_run
    assert run is not None

    from app.services import extraction_service

    db_run = session.get(extraction_service.ExtractionRun, run.id)
    assert db_run is not None
    for item in db_run.items:
        if item.kind in ("FACT", "TIMELINE", "CHARACTER_STATE", "LOCATION"):
            extraction_service.review_item(session, item, ItemReviewStatus.ACCEPTED)
    session.commit()
    result = extraction_service.apply_run(session, novel, db_run)
    session.commit()

    assert result.applied, "确认后应写入正式表"
    assert db_run.status in ("APPLIED", "PARTIALLY_APPLIED")

    facts = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.subject == "林默",
                CanonFact.predicate == "佩剑",
            )
        )
    )
    statuses = {fact.status: fact.object for fact in facts}
    assert statuses.get("CANON") == "赤霄剑", "作者确认后新事实才升级为 CANON"
    assert statuses.get("SUPERSEDED") == "青霜剑", "旧事实应标记为 SUPERSEDED"

    timeline = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id, CanonFact.subject == "林默"
            )
        )
    )
    assert timeline


def test_workflow_is_idempotent_per_chapter(session, novel, chapters):
    first = workflow_service.complete_chapter(session, novel, chapters[16], provider_name="offline")
    session.commit()
    second = workflow_service.complete_chapter(session, novel, chapters[16], provider_name="offline")
    session.commit()
    assert first.errors == 0 and second.errors == 0
    runs = list(
        session.scalars(
            select(ExtractionItem).where(
                ExtractionItem.run_id == second.extraction_run.id  # type: ignore[union-attr]
            )
        )
    )
    assert runs, "重复跑工作流应产生新的待审项而不是覆盖旧的"
