"""能力 5：能否正确维护人物状态（状态历史 + 当前状态 + 审校联动）。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Character, ExtractionRun, ItemReviewStatus
from app.services import extraction_service, query_service


def _latest_run(session, chapter_id: str) -> ExtractionRun:
    run = session.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.chapter_id == chapter_id)
        .order_by(ExtractionRun.created_at.desc())
    )
    assert run is not None
    return run


def test_state_confirmed_from_chapter_19(session, novel, chapters):
    chapter = chapters[19]
    extraction_service.run_extraction(session, novel, chapter, provider_name="offline")
    session.commit()

    wang = query_service.get_character(session, novel.id, "王烈")
    assert wang is not None
    assert wang.current_status == "重伤昏迷", "确认前不得自动更新人物状态"
    assert wang.states == [], "确认前不应写入状态历史"

    run = _latest_run(session, chapter.id)
    state_items = [item for item in run.items if item.kind == "CHARACTER_STATE"]
    assert state_items, "第 19 章应抽出人物状态项"
    wang_items = [item for item in state_items if item.payload.get("name") == "王烈"]
    assert wang_items, "应包含王烈"
    assert wang_items[0].payload.get("status_change") == "苏醒"

    for item in run.items:
        extraction_service.review_item(session, item, ItemReviewStatus.ACCEPTED)
    result = extraction_service.apply_run(session, novel, run)
    session.commit()
    assert result.applied

    session.refresh(wang)
    assert wang.current_status == "苏醒"
    assert wang.last_appearance == 19
    assert len(wang.states) == 1
    state = wang.states[0]
    assert state.chapter_number == 19
    assert state.source == "EXTRACTOR"
    assert state.status == "苏醒"


def test_manual_state_update_keeps_history(session, novel):
    lin = query_service.get_character(session, novel.id, "林默")
    assert lin is not None
    query_service.record_character_state(
        session,
        lin,
        status="ACTIVE",
        location="青云镇",
        note="下山追查血河教线索",
        chapter_number=13,
        source="MANUAL",
    )
    session.commit()
    session.refresh(lin)
    assert lin.current_location == "青云镇"
    assert [state.chapter_number for state in lin.states] == [13]

    view = query_service.get_character_state(session, novel.id, "林默", chapter_number=12)
    assert view["found"] is True
    assert view["current_location"] == "青云镇"
    assert view["state_at_chapter"] is None, "第 13 章之前还没有状态记录"
    view_after = query_service.get_character_state(session, novel.id, "林默", chapter_number=14)
    assert view_after["state_at_chapter"] is not None
    assert view_after["state_at_chapter"]["location"] == "青云镇"


def test_get_character_state_tool_returns_unknown_for_missing(session, novel):
    from app.ai.tools import AIToolKit

    toolkit = AIToolKit(session, novel)
    result = toolkit.call("get_character_state", {"name": "张无忌"})
    assert result["status"] == "UNKNOWN"
    assert result["data"]["status"] == "UNKNOWN"


def test_character_first_and_last_appearance_updated(session, novel, chapters):
    zhao = query_service.get_character(session, novel.id, "赵铁山")
    assert zhao is not None and zhao.last_appearance == 8
    query_service.record_character_state(
        session, zhao, status="死亡", note="断魂崖", chapter_number=8, source="MANUAL"
    )
    session.commit()
    session.refresh(zhao)
    assert zhao.first_appearance == 1
    assert zhao.last_appearance == 8

    wang = query_service.get_character(session, novel.id, "王烈")
    assert wang is not None
    before = session.scalar(select(Character.name).where(Character.id == wang.id))
    assert before == "王烈"
