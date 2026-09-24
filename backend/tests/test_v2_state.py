"""V0.2 时点视图：第 N 章时有效的设定，以及它带来的误报消除。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import CanonFact, CanonStatus, Chapter
from app.services import continuity_service, extraction_service, query_service


def _check(session, novel, chapter):
    result, _, _, _, _ = extraction_service.run_extraction(
        session, novel, chapter, provider_name="offline"
    )
    report, _ = continuity_service.run_check(
        session, novel, chapter, provider_name="offline", extraction=result
    )
    session.commit()
    return report


def _chapter(session, novel, number) -> Chapter:
    return session.scalar(
        select(Chapter).where(Chapter.novel_id == novel.id, Chapter.chapter_number == number)
    )


def test_as_of_excludes_later_facts(session, novel):
    at_six = query_service.canon_as_of(session, novel.id, 6)
    objects = {(fact["subject"], fact["predicate"], fact["object"]) for fact in at_six}
    assert ("林默", "佩剑", "青霜剑") in objects
    assert ("赵铁山", "状态", "死亡") not in objects, "第 8 章才发生的事不应出现在第 6 章视图里"
    assert ("王烈", "状态", "重伤昏迷") not in objects, "第 7 章才发生的事不应出现在第 6 章视图里"

    at_twenty = query_service.canon_as_of(session, novel.id, 20)
    later_objects = {(fact["subject"], fact["predicate"], fact["object"]) for fact in at_twenty}
    assert ("赵铁山", "状态", "死亡") in later_objects
    assert len(later_objects) > len(objects)


def test_proposed_facts_never_appear_in_as_of_view(session, novel, chapters):
    query_service.propose_canon_fact(
        session,
        novel.id,
        subject="林默",
        predicate="佩剑",
        object_value="赤霄剑",
        source_chapter=14,
        origin="AI_TOOL",
    )
    session.commit()
    for chapter_number in (6, 14, 20):
        facts = query_service.canon_as_of(session, novel.id, chapter_number)
        assert all(fact["status"] != CanonStatus.PROPOSED for fact in facts)
        assert all(fact["object"] != "赤霄剑" for fact in facts)


def test_confirming_a_change_keeps_earlier_chapters_clean(session, novel, chapters):
    """V0.1 的痛点：确认了第 14 章的换剑之后，回头检查第 6 章会被误判成矛盾。"""
    report_14 = _check(session, novel, chapters[14])
    assert "FACT_CONFLICT" in {issue.code for issue in report_14.errors}

    proposed = session.scalar(
        select(CanonFact).where(CanonFact.novel_id == novel.id, CanonFact.object == "赤霄剑")
    )
    assert proposed is not None
    outcome = extraction_service.promote_fact(session, proposed)
    session.commit()
    assert outcome["valid_from_chapter"] == 14

    old = session.get(CanonFact, outcome["superseded"][0])
    assert old is not None and old.valid_until_chapter == 14, "旧事实应在第 14 章失效"

    after_six = _check(session, novel, chapters[6])
    assert after_six.errors == [], "第 6 章写的仍是当时有效的青霜剑，不该报错"
    after_fourteen = _check(session, novel, chapters[14])
    assert after_fourteen.errors == [], "第 14 章与已确认的新 Canon 一致"


def test_as_of_view_shows_the_right_weapon(session, novel, chapters):
    from app.services import extraction_service as es

    extraction_service.run_extraction(session, novel, chapters[14], provider_name="offline")
    session.commit()
    proposed = session.scalar(
        select(CanonFact).where(CanonFact.novel_id == novel.id, CanonFact.object == "赤霄剑")
    )
    es.promote_fact(session, proposed)
    session.commit()

    def weapon_at(number: int) -> list[str]:
        return [
            fact["object"]
            for fact in query_service.canon_as_of(session, novel.id, number)
            if fact["predicate"] == "佩剑"
        ]

    assert weapon_at(6) == ["青霜剑"]
    assert weapon_at(13) == ["青霜剑"]
    assert weapon_at(14) == ["赤霄剑"]
    assert weapon_at(20) == ["赤霄剑"]


def test_state_endpoint_shape(client):
    novel = client.post("/api/novels", json={"title": "时点视图"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    state = client.get(f"/api/novels/{novel['id']}/state?chapter=10").json()
    assert state["chapter_number"] == 10
    assert state["canon_facts"], "应返回该时点的 Canon 事实"
    assert all(
        fact["valid_from_chapter"] <= 10 for fact in state["canon_facts"] if fact["valid_from_chapter"]
    )
    assert any(character["name"] == "林默" for character in state["characters"])
    assert state["timeline"] and state["world_rules"]

    far = client.get(f"/api/novels/{novel['id']}/state?chapter=1").json()
    assert len(far["canon_facts"]) <= len(state["canon_facts"])


def test_state_endpoint_does_not_backdate_future_status(client):
    """第 6 章时赵铁山还没死，不能把第 8 章的死亡状态显示成当时的状态。"""
    novel = client.post("/api/novels", json={"title": "时点状态推导"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")

    early = client.get(f"/api/novels/{novel['id']}/state?chapter=6").json()
    zhao = next(item for item in early["characters"] if item["name"] == "赵铁山")
    assert zhao["status_at_chapter"] != "死亡", "第 6 章时赵铁山尚未战死"
    assert "状态记录" in zhao["status_source"], zhao["status_source"]

    late = client.get(f"/api/novels/{novel['id']}/state?chapter=8").json()
    zhao_late = next(item for item in late["characters"] if item["name"] == "赵铁山")
    assert zhao_late["status_at_chapter"] == "死亡"
    assert "Canon 事实" in zhao_late["status_source"]

    before_debut = client.get(f"/api/novels/{novel['id']}/state?chapter=1").json()
    su = next(item for item in before_debut["characters"] if item["name"] == "苏月宁")
    assert su["status_at_chapter"] == "尚未登场", "第 4 章才出场的角色，在第 1 章不该有状态"


def test_superseded_seed_fact_has_validity_window(client):
    novel = client.post("/api/novels", json={"title": "修为时点"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    facts = client.get(f"/api/novels/{novel['id']}/state?chapter=6").json()["canon_facts"]
    realms = {
        fact["object"] for fact in facts if fact["predicate"] == "修为" and fact["subject"] == "林默"
    }
    assert realms == {"炼气三层"}, "第 6 章时林默的修为还是炼气三层"
    later = client.get(f"/api/novels/{novel['id']}/state?chapter=20").json()["canon_facts"]
    later_realms = {
        fact["object"] for fact in later if fact["predicate"] == "修为" and fact["subject"] == "林默"
    }
    assert later_realms == {"炼气九层"}
    layers = next(
        (fact for fact in client.get(f"/api/novels/{novel['id']}/canon-facts").json()
         if fact["object"] == "炼气三层"),
        None,
    )
    if layers is not None:
        assert layers["valid_until_chapter"] == 12


def test_timeline_and_events_respect_as_of(session, novel):
    early = query_service.list_timeline(session, novel.id, status=None, as_of_chapter=5, limit=100)
    late = query_service.list_timeline(session, novel.id, status=None, as_of_chapter=20, limit=100)
    assert all((entry["chapter_number"] or 0) <= 5 for entry in early)
    assert len(late) > len(early)
    early_events = query_service.list_events(session, novel.id, limit=100, as_of_chapter=5)
    assert all((event["chapter_number"] or 0) <= 5 for event in early_events)


def test_deleting_chapter_rejects_its_pending_facts(session, novel, chapters):
    """删掉章节后，由它抽出但未确认的候选事实不能变成孤儿。"""
    from app.services import chapter_service, extraction_service

    chapter = chapters[14]
    extraction_service.run_extraction(session, novel, chapter, provider_name="offline")
    session.commit()
    pending = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.status == CanonStatus.PROPOSED,
                CanonFact.source_chapter == 14,
            )
        )
    )
    assert pending, "第 14 章应有未确认的候选事实"

    chapter_service.delete_chapter(session, novel, chapter)
    session.commit()
    for fact in pending:
        session.refresh(fact)
        assert fact.status == CanonStatus.REJECTED
        assert "来源章节已删除" in fact.note

    canon = [
        fact
        for fact in session.scalars(
            select(CanonFact).where(CanonFact.novel_id == novel.id, CanonFact.subject == "林默")
        )
        if fact.status == CanonStatus.CANON
    ]
    assert canon, "已确认的 Canon 事实不受章节删除影响"
