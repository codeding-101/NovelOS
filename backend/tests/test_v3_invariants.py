"""V0.3 全局不变量：跨全书的硬约束（含植入违规的检出）。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import CanonFact, CanonStatus, Chapter, Character
from app.schemas import ChapterUpdate
from app.services import chapter_service, invariant_service, query_service


def _facts(session, novel, subject, predicate):
    return list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.subject == subject,
                CanonFact.predicate == predicate,
            )
        )
    )


def test_clean_seed_novel_has_no_invariant_issues(session, novel):
    report = invariant_service.check_invariants(session, novel)
    assert report["errors"] == [], f"干净的种子小说不应有全局错误：{report['errors']}"
    assert report["checked"]["chapters"] == 20
    assert report["checked"]["facts"] >= 10


def test_time_inversion_is_detected(session, novel, chapters):
    chapter = session.get(Chapter, chapters[18].id)
    chapter_service.update_chapter(
        session, novel, chapter, ChapterUpdate(story_time="天启三年四月初一")
    )
    session.commit()
    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["errors"]}
    assert "TIME_INVERSION" in codes
    issue = next(item for item in report["errors"] if item["code"] == "TIME_INVERSION")
    sources = {evidence["source_chapter"] for evidence in issue["evidence"]}
    assert len(sources) == 2, "必须给出回退前后两章的出处"
    assert issue["suggestion"]


def test_realm_regression_is_detected(session, novel):
    query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="修为",
        object_value="筑基中期",
        source_chapter=13,
        origin="USER",
    )
    session.commit()
    fact = _facts(session, novel, "林默", "修为")[-1]
    from app.services import extraction_service

    extraction_service.promote_fact(session, fact)
    session.commit()

    lower = query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="修为",
        object_value="炼气五层",
        source_chapter=16,
        origin="USER",
    )
    session.commit()
    extraction_service.promote_fact(session, lower)
    session.commit()

    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["errors"]}
    assert "REALM_REGRESSION" in codes
    issue = next(item for item in report["errors"] if item["code"] == "REALM_REGRESSION")
    assert "筑基中期" in issue["message"] and "炼气五层" in issue["message"]


def test_realm_level_parsing():
    assert invariant_service.realm_level("炼气三层") < invariant_service.realm_level("炼气九层")
    assert invariant_service.realm_level("炼气九层") < invariant_service.realm_level("筑基初期")
    assert invariant_service.realm_level("筑基中期") < invariant_service.realm_level("金丹后期")
    assert invariant_service.realm_level("无法解析的说法") is None


def test_exclusive_possession_conflict(session, novel):
    """同一时点两把佩剑：V0.1 的换剑流程会产生不重叠的窗口，这里植入一次真重叠。"""
    first = query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="法宝",
        object_value="青玉葫芦",
        source_chapter=5,
        origin="USER",
    )
    second = query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="法宝",
        object_value="黑铁令牌",
        source_chapter=9,
        origin="USER",
    )
    session.commit()
    for fact in (first, second):
        fact.status = CanonStatus.CANON
        fact.valid_from_chapter = fact.source_chapter
    session.commit()

    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["errors"]}
    assert "EXCLUSIVE_CONFLICT" in codes
    issue = next(item for item in report["errors"] if item["code"] == "EXCLUSIVE_CONFLICT")
    assert "青玉葫芦" in issue["message"] and "黑铁令牌" in issue["message"]


def test_superseded_possession_is_not_a_conflict(session, novel):
    report = invariant_service.check_invariants(session, novel)
    assert "EXCLUSIVE_CONFLICT" not in {issue["code"] for issue in report["errors"]}


def test_knowledge_shrink_warning(session, novel):
    from app.services import extraction_service

    old = query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="父亲",
        object_value="林远山",
        source_chapter=10,
        origin="USER",
        known_by=["林默", "苏月宁", "王烈"],
    )
    session.commit()
    extraction_service.promote_fact(session, old)
    session.commit()

    new = query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="父亲",
        object_value="林远山（生死未明）",
        source_chapter=13,
        origin="USER",
        known_by=["林默"],
    )
    session.commit()
    extraction_service.promote_fact(session, new)
    session.commit()

    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["warnings"]}
    assert "KNOWLEDGE_SHRINK" in codes
    issue = next(item for item in report["warnings"] if item["code"] == "KNOWLEDGE_SHRINK")
    assert "苏月宁" in issue["message"] or "王烈" in issue["message"]


def test_location_jump_same_day(session, novel, chapters):
    """第 5、6 章的故事时间都是天启三年四月初五：同一天跑两个地方就是硬伤。"""
    lin = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "林默")
    )
    query_service.record_character_state(
        session, lin, status="ACTIVE", location="青云山", chapter_number=5, note="在山门"
    )
    query_service.record_character_state(
        session, lin, status="ACTIVE", location="青州城", chapter_number=6, note="同一天却在青州"
    )
    session.commit()
    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["warnings"]}
    assert "LOCATION_JUMP" in codes, "同一天出现在两地必须提示"
    issue = next(item for item in report["warnings"] if item["code"] == "LOCATION_JUMP")
    sources = {evidence["source_chapter"] for evidence in issue["evidence"]}
    assert sources == {"第5章", "第6章"}


def test_travel_across_days_is_not_flagged(session, novel):
    """相隔数天的位置变化是正常的，不该被报成瞬移。"""
    lin = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "林默")
    )
    query_service.record_character_state(
        session, lin, status="ACTIVE", location="青云山", chapter_number=12, note="闭关"
    )
    query_service.record_character_state(
        session, lin, status="ACTIVE", location="青云镇", chapter_number=13, note="下山"
    )
    session.commit()
    report = invariant_service.check_invariants(session, novel)
    assert "LOCATION_JUMP" not in {issue["code"] for issue in report["warnings"]}


def test_character_dormant_and_unused(session, novel, chapters):
    session.add(
        Character(novel_id=novel.id, name="路人甲", description="没有出场记录的角色")
    )
    session.commit()
    report = invariant_service.check_invariants(session, novel)
    codes = {issue["code"] for issue in report["warnings"]}
    assert "CHARACTER_UNUSED" in codes

    character = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "苏月宁")
    )
    character.last_appearance = 2
    session.commit()
    report_after = invariant_service.check_invariants(session, novel)
    assert "CHARACTER_DORMANT" in {issue["code"] for issue in report_after["warnings"]}


def test_run_invariants_persists_report(session, novel):
    report = invariant_service.run_invariants(session, novel)
    session.commit()
    assert report["report_id"]
    latest = invariant_service.latest_report(session, novel.id)
    assert latest is not None and latest.id == report["report_id"]
    assert latest.errors == len(report["errors"])


def test_extra_chapter_can_break_then_fix_invariants(session, novel, chapters):
    """把植入的违规改回来，不变量必须回到干净状态（不能只报不管）。"""
    chapter = session.get(Chapter, chapters[18].id)
    original = chapter.story_time
    chapter_service.update_chapter(
        session, novel, chapter, ChapterUpdate(story_time="天启三年四月初一")
    )
    session.commit()
    assert invariant_service.check_invariants(session, novel)["errors"]

    chapter = session.get(Chapter, chapters[18].id)
    chapter_service.update_chapter(session, novel, chapter, ChapterUpdate(story_time=original))
    session.commit()
    assert invariant_service.check_invariants(session, novel)["errors"] == []


def test_invariant_endpoint(client):
    novel = client.post("/api/novels", json={"title": "不变量接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    report = client.get(f"/api/novels/{novel['id']}/invariants").json()
    assert report["checked"]["chapters"] == 20
    assert report["errors"] == 0

    rerun = client.post(f"/api/novels/{novel['id']}/invariants/run").json()
    assert rerun["report_id"]
    assert rerun["codes"] == {}

    chapters = client.get(f"/api/novels/{novel['id']}/chapters").json()
    target = chapters[17]["chapter_id"]
    client.put(f"/api/chapters/{target}", json={"story_time": "天启三年四月初一"})
    assert client.post(f"/api/novels/{novel['id']}/invariants/run").json()["errors"] >= 1
