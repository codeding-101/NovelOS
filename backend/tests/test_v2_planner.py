"""V0.2 章节规划与伏笔调度。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Chapter, ChapterPlan
from app.schemas import ChapterPlanCreate, PlanGenerateRequest
from app.services import foreshadow_service, plan_service


def _generate(session, novel, *, count=3, steer="", overwrite=False):
    return plan_service.generate_plans(
        session, novel, PlanGenerateRequest(from_chapter=21, count=count, steer=steer, overwrite=overwrite)
    )


def test_planner_produces_consecutive_persisted_plans(session, novel):
    result = _generate(session, novel, count=3, steer="尽快揭开师父的旧事")
    session.commit()
    plans = result["plans"]
    assert [plan.chapter_number for plan in plans] == [21, 22, 23]
    assert all(plan.goals and plan.rationale for plan in plans)
    assert any(plan.advance_foreshadowing for plan in plans), "有伏笔欠账时应安排推进"
    assert all("师父" in plan.goals or plan.steer for plan in plans)

    stored = list(
        session.scalars(select(ChapterPlan).where(ChapterPlan.novel_id == novel.id))
    )
    assert len(stored) == 3
    assert all(plan.source == "PLANNER" and plan.provider for plan in stored)


def test_planner_avoids_dead_and_incapacitated_characters(session, novel):
    plans = _generate(session, novel, count=2)["plans"]
    for plan in plans:
        assert "赵铁山" not in plan.characters, "赵铁山已死亡，不应作为出场人物"
        assert "王烈" not in plan.characters, "王烈仍重伤昏迷，不应作为出场人物"
        assert "赵铁山" in plan.forbidden or "王烈" in plan.forbidden


def test_cast_constraint_is_enforced_in_code(session, novel):
    """模型若把无法行动的人物写成出场人物，服务层应自动移出并写入 forbidden。"""
    from app.schemas import PlannedChapter
    from app.services.plan_service import enforce_cast_constraints, unavailable_characters

    context = {
        "characters": [
            {"name": "林默", "current_status": "ACTIVE"},
            {"name": "赵铁山", "current_status": "死亡"},
            {"name": "王烈", "current_status": "重伤昏迷"},
        ]
    }
    unavailable = unavailable_characters(context)
    assert unavailable == {"赵铁山": "死亡", "王烈": "重伤昏迷"}

    chapters = [
        PlannedChapter(
            chapter_number=21,
            title="测试",
            characters=["林默", "赵铁山", "王烈"],
            forbidden=["血无痕直接现身"],
        )
    ]
    warnings = enforce_cast_constraints(chapters, unavailable)
    assert chapters[0].characters == ["林默"]
    assert set(chapters[0].forbidden) == {"血无痕直接现身", "赵铁山", "王烈"}
    assert len(warnings) == 1 and "无法行动" in warnings[0]


def test_planner_advances_the_stalest_foreshadowing_first(session, novel):
    debt = foreshadow_service.debt(session, novel)
    assert debt, "测试小说应有伏笔欠账"
    oldest = debt[0]["name"]
    plans = _generate(session, novel, count=1)["plans"]
    assert oldest in plans[0].advance_foreshadowing


def test_regenerate_without_overwrite_skips_existing(session, novel):
    _generate(session, novel, count=2)
    session.commit()
    second = _generate(session, novel, count=2, overwrite=False)
    assert second["plans"] == []
    skipped = second["context_summary"]["skipped"]
    assert len(skipped) == 2 and all("已有计划" in item["reason"] for item in skipped)

    third = _generate(session, novel, count=2, overwrite=True)
    session.commit()
    assert len(third["plans"]) == 2


def test_plans_are_skipped_for_chapters_that_already_exist(session, novel):
    result = plan_service.generate_plans(
        session, novel, PlanGenerateRequest(from_chapter=19, count=3, overwrite=True)
    )
    session.commit()
    numbers = [plan.chapter_number for plan in result["plans"]]
    assert numbers == [21], "第 19、20 章已有正文，只应保存第 21 章的计划"
    skipped = result["context_summary"]["skipped"]
    assert {item["chapter_number"] for item in skipped} == {19, 20}
    assert all("已有正文" in item["reason"] for item in skipped)


def test_plan_status_syncs_to_written(session, novel):
    plans = _generate(session, novel, count=1)["plans"]
    session.commit()
    plan = plans[0]
    assert plan.status == "PLANNED"
    from app.schemas import ChapterCreate
    from app.services import chapter_service

    chapter = chapter_service.create_chapter(
        session, novel, ChapterCreate(chapter_number=plan.chapter_number)
    )
    session.commit()
    plan_service.sync_status(session, novel)
    session.commit()
    session.refresh(plan)
    assert plan.status == "WRITTEN"

    # 正文被删掉后计划应退回 PLANNED，避免看板上出现「已写但没正文」的悬空计划
    chapter_service.delete_chapter(session, novel, chapter)
    session.commit()
    plan_service.sync_status(session, novel)
    session.commit()
    session.refresh(plan)
    assert plan.status == "PLANNED"


def test_manual_plan_upsert(session, novel):
    plan = plan_service.upsert_plan(
        session,
        novel,
        ChapterPlanCreate(
            chapter_number=25,
            title="尾声",
            goals="收束父亲旧事",
            must_include=["青霜剑"],
            characters=["林默"],
            status="PLANNED",
        ),
    )
    session.commit()
    assert plan.source == "USER"
    updated = plan_service.upsert_plan(
        session, novel, ChapterPlanCreate(chapter_number=25, title="尾声（改）", status="DISCARDED")
    )
    session.commit()
    assert updated.id == plan.id and updated.status == "DISCARDED"
    assert updated.title == "尾声（改）"


def test_plan_to_write_request_keeps_constraints(session, novel):
    plans = _generate(session, novel, count=1)["plans"]
    session.commit()
    request = plan_service.plan_to_write_request(plans[0], target_words=700)
    assert request.chapter_number == plans[0].chapter_number
    assert request.must_include == list(plans[0].must_include)
    assert request.forbidden == list(plans[0].forbidden)
    assert request.characters == list(plans[0].characters)
    assert request.target_words == 700


def test_foreshadowing_debt_and_suggestions(session, novel):
    debt = foreshadow_service.debt(session, novel)
    assert len(debt) == 5, "测试小说有 5 条伏笔"
    ages = [item["age"] for item in debt]
    assert ages == sorted(ages, reverse=True), "欠账按拖得最久的排前面"
    assert all(item["evidence"] for item in debt)

    plan = foreshadow_service.suggest(session, novel, horizon=5)
    assert plan["frontier_chapter"] == 20
    assert plan["open_count"] == 5
    assert plan["suggestions"]
    top = plan["suggestions"][0]
    assert top["age"] == max(ages)
    assert top["urgency"] in ("HIGH", "MEDIUM")
    assert top["suggested_chapter"] >= 21
    assert top["reason"]


def test_foreshadowing_plan_endpoint(client):
    novel = client.post("/api/novels", json={"title": "伏笔调度"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    plan = client.get(f"/api/novels/{novel['id']}/foreshadowing-plan").json()
    assert plan["frontier_chapter"] == 20
    assert len(plan["debt"]) == 5
    assert plan["suggestions"]
    assert plan["overdue_count"] >= 1


def test_write_from_plan_endpoint(client):
    novel = client.post("/api/novels", json={"title": "按计划写作"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    generated = client.post(
        f"/api/novels/{novel['id']}/plans/generate",
        json={"from_chapter": 21, "count": 1, "steer": "写一段追踪戏"},
    ).json()
    assert generated["plans"], generated
    plan = generated["plans"][0]

    written = client.post(f"/api/plans/{plan['id']}/write?save=true&target_words=600").json()
    assert written["draft"]["content"]
    assert written["saved_chapter_id"]
    for item in plan["must_include"]:
        assert item in written["draft"]["content"], f"计划要求出现的「{item}」应出现在正文里"
    assert written["retrieved"]["canon_facts"]

    refreshed = client.get(f"/api/novels/{novel['id']}/plans").json()
    assert refreshed[0]["status"] == "WRITTEN"
    assert refreshed[0]["chapter_number"] == 21
    chapters = client.get(f"/api/novels/{novel['id']}/chapters").json()
    assert len(chapters) == 21
    assert chapters[-1]["status"] == "DRAFT"


def test_plan_delete_and_not_found(client):
    novel = client.post("/api/novels", json={"title": "计划删除"}).json()
    plan = client.post(
        f"/api/novels/{novel['id']}/plans",
        json={"chapter_number": 1, "title": "第一章", "goals": "开局"},
    ).json()
    assert client.post("/api/plans/plan_missing/write").status_code == 404
    assert client.delete(f"/api/plans/{plan['id']}").status_code == 204
    assert client.get(f"/api/novels/{novel['id']}/plans").json() == []


def test_chapter_records_unchanged_by_planning(session, novel):
    before = list(
        session.scalars(
            select(Chapter.chapter_number).where(Chapter.novel_id == novel.id)
        )
    )
    _generate(session, novel, count=3)
    session.commit()
    after = list(
        session.scalars(
            select(Chapter.chapter_number).where(Chapter.novel_id == novel.id)
        )
    )
    assert before == after, "规划不得写入正文"
