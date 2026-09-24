"""V0.3 承诺账本与修订闭环。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import Chapter, Commitment, Novel
from app.schemas import WriteChapterRequest
from app.services import (
    commitment_service,
    extraction_service,
    invariant_service,
)


def _make_novel(session, title: str) -> Novel:
    novel = Novel(title=title, slug=f"t-{title}", target_word_count=100_000)
    session.add(novel)
    session.flush()
    return novel


# --------------------------------------------------------------------------- 承诺
def test_deadline_arithmetic():
    assert commitment_service.extract_deadline_hint("三日后在听雨楼见")[0] == 3
    assert commitment_service.extract_deadline_hint("十日内回山")[0] == 10
    assert commitment_service.extract_deadline_hint("今晚子时动手")[0] == 0
    assert commitment_service.extract_deadline_hint("他没有说期限")[0] is None


def test_commitment_due_is_computed_from_story_time(session, novel, chapters):
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=5,
        who="苏月宁",
        counterpart="林默",
        what="三日后在听雨楼见",
        quote="“三日后，听雨楼见。”苏月宁说。",
        deadline_text="三日后",
        story_time="天启三年四月初五",
        origin="USER",
    )
    session.commit()
    assert record.due_story_time == "天启三年四月初八"
    assert record.due_sort == commitment_service.story_time_to_day("天启三年四月初五") + 3


def test_overdue_commitment_is_detected_with_evidence(session, novel, chapters):
    """第 12 章的「十日内回山」到第 15 章（五月十四）已经越期。"""
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=12,
        who="林默",
        counterpart="王烈",
        what="十日内回山",
        quote="十日内必回山。",
        deadline_text="十日内",
        story_time="天启三年五月初三",
        origin="USER",
    )
    session.commit()
    assert record.due_story_time == "天启三年五月十三"

    items = commitment_service.evaluate(session, novel, update=True)
    session.commit()
    target = next(item for item in items if item["id"] == record.id)
    assert target["status"] == "OVERDUE"
    assert target["breach_chapter"] == 15
    assert len(target["evidence"]) == 2
    session.refresh(record)
    assert record.status == "OVERDUE"

    report = invariant_service.check_invariants(session, novel)
    assert "COMMITMENT_OVERDUE" in {issue["code"] for issue in report["warnings"]}


def test_commitment_due_today_is_not_overdue(session, novel, chapters):
    """到期当天算「今天到期」，越过了才算逾期。"""
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=19,
        who="林默",
        counterpart="苏月宁",
        what="三日后取东西",
        quote="三日后我来取。",
        deadline_text="三日后",
        story_time="天启三年五月廿二",
        origin="USER",
    )
    session.commit()
    assert record.due_story_time == "天启三年五月廿五"
    items = commitment_service.evaluate(session, novel)
    target = next(item for item in items if item["id"] == record.id)
    assert target["status"] == "OPEN", "第 20 章正是五月廿五，这一天还没过完"
    assert target["days_remaining"] == 0


def test_open_commitment_counts_down(session, novel, chapters):
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=18,
        who="林默",
        counterpart="苏月宁",
        what="五日内送信",
        quote="五日内我把信送来。",
        deadline_text="五日内",
        story_time="天启三年五月二十",
        origin="USER",
    )
    session.commit()
    items = commitment_service.evaluate(session, novel)
    target = next(item for item in items if item["id"] == record.id)
    assert target["status"] == "OPEN"
    assert target["days_remaining"] == 0, "第 20 章是五月廿五，正好到期"


def test_fulfil_and_abandon_clear_the_debt(session, novel, chapters):
    record = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=4,
        who="赵铁山",
        what="三日内到驿站取信",
        quote="信三日内到驿站，取到就回山。",
        deadline_text="三日内",
        story_time="天启三年四月初二",
        origin="USER",
    )
    session.commit()
    overdue = [item for item in commitment_service.evaluate(session, novel) if item["id"] == record.id]
    assert overdue[0]["status"] == "OVERDUE"
    assert overdue[0]["breach_chapter"] == 7, "期限四月初五，第 7 章（四月初六）已经越过"

    commitment_service.fulfil(session, record, chapter_number=6, note="第 6 章补上了取信的交代")
    session.commit()
    items = commitment_service.evaluate(session, novel)
    target = next(item for item in items if item["id"] == record.id)
    assert target["status"] == "FULFILLED"
    assert target["fulfilled_chapter"] == 6
    assert "COMMITMENT_OVERDUE" not in {
        issue["code"] for issue in invariant_service.check_invariants(session, novel)["warnings"]
    }

    second = commitment_service.add_commitment(
        session,
        novel,
        source_chapter=4,
        what="某条不再重要的约定",
        quote="回头再说。",
        deadline_text="",
        story_time="天启三年四月初二",
        origin="USER",
    )
    session.commit()
    commitment_service.abandon(session, second, note="作者决定放弃这条线")
    session.commit()
    target = next(
        item for item in commitment_service.evaluate(session, novel) if item["id"] == second.id
    )
    assert target["status"] == "ABANDONED"


def test_offline_extraction_finds_seeded_promise(session, novel, chapters):
    """第 4 章的「信三日内到驿站」是书里真实存在的约定，规则抽取应该抓得到。"""
    result, run, _, _, _ = extraction_service.run_extraction(
        session, novel, chapters[4], provider_name="offline"
    )
    session.commit()
    assert result.commitments, "应抽出承诺/期限"
    first = result.commitments[0]
    assert first.deadline_text == "三日内"
    assert "驿" in first.quote
    assert run is not None
    assert any(item.kind == "COMMITMENT" for item in run.items)

    applied = extraction_service.apply_run(session, novel, run, accept_pending=True)
    session.commit()
    assert any(entry["kind"] == "COMMITMENT" for entry in applied.applied)
    commitment_service.evaluate(session, novel, update=True)
    session.commit()
    rows = list(session.scalars(select(Commitment).where(Commitment.novel_id == novel.id)))
    assert rows and rows[0].status == "OVERDUE", "该约定在第 7 章已被越过"


def test_commitment_api_flow(client):
    novel = client.post("/api/novels", json={"title": "承诺接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    created = client.post(
        f"/api/novels/{novel['id']}/commitments",
        json={
            "source_chapter": 11,
            "kind": "DEADLINE",
            "who": "林默",
            "what": "十日内出关",
            "quote": "十日内必出关。",
            "deadline_text": "十日内",
        },
    )
    assert created.status_code == 201
    body = created.json()
    assert body["due_story_time"] and body["status"] in ("OPEN", "OVERDUE")

    listed = client.get(f"/api/novels/{novel['id']}/commitments").json()
    assert any(item["id"] == body["id"] for item in listed)

    fulfilled = client.post(
        f"/api/commitments/{body['id']}/fulfill", json={"chapter_number": 12, "note": "第 12 章出关"}
    ).json()
    assert fulfilled["status"] == "FULFILLED"

    open_only = client.get(f"/api/novels/{novel['id']}/commitments?status=OVERDUE").json()
    assert all(item["status"] == "OVERDUE" for item in open_only)

    missing = client.post("/api/commitments/cmt_missing/fulfill", json={})
    assert missing.status_code == 404


# --------------------------------------------------------------------------- 修订闭环
def test_revision_loop_improves_or_holds_metrics(session, novel):
    agents = extraction_service.build_agents("offline")
    loop = agents["revision"]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(
            goals="林默在废祠外遭遇血河教，第一次独立应战",
            must_include=["青霜剑"],
            characters=["林默"],
            chapter_number=21,
            target_words=800,
        ),
        max_rounds=2,
        target_score=85.0,
        use_model_critic=False,
        persist_reviews=True,
    )
    session.commit()
    assert result.rounds, "至少要记录草稿这一轮"
    assert result.rounds[0].stage == "draft"
    assert result.draft.content
    assert result.final_score >= 0
    cliche = result.metric_deltas["deltas"]["cliche_per_1k"]
    assert cliche["delta"] <= 0, f"改稿不应让套话变多：{cliche}"
    assert result.retrieved.get("canon_facts"), "闭环同样要保留检索快照"
    if not result.accepted:
        assert any("未达到目标分" in warning for warning in result.warnings)


def test_revision_loop_stops_when_rewrite_changes_nothing(session, novel):
    from app.ai.base import AIResponse

    class _FrozenProvider:
        name = "frozen"
        model = "frozen-1"
        kind = "llm"
        supports_tools = False

        def generate(self, request):  # noqa: ANN001
            if request.task == "style_revise":
                return AIResponse(
                    text=request.context.get("content", ""),  # 原样返回
                    provider=self.name,
                    model=self.model,
                )
            if request.task == "write":
                return AIResponse(
                    text="# 第21章 测试\n\n他缓缓地抬起头，眼中闪过一丝寒芒。",
                    provider=self.name,
                    model=self.model,
                )
            return AIResponse(text="{}", parsed={}, provider=self.name, model=self.model)

    from app.ai.agents.revision_loop import RevisionLoop

    loop = RevisionLoop(_FrozenProvider())  # type: ignore[arg-type]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(goals="测试无进展保护", characters=["林默"], target_words=300),
        max_rounds=3,
        use_model_critic=False,
    )
    assert len(result.rounds) <= 3
    assert any("没有产生变化" in warning for warning in result.warnings)
    assert result.accepted is False


def test_revision_loop_can_skip_rounds(session, novel):
    loop = extraction_service.build_agents("offline")["revision"]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(goals="只出草稿", characters=["林默"], target_words=400),
        max_rounds=0,
    )
    session.commit()
    assert len(result.rounds) == 1
    assert result.rounds[0].stage == "draft"


def test_revision_loop_saves_final_draft(session, novel):
    loop = extraction_service.build_agents("offline")["revision"]
    result = loop.run(
        session,
        novel,
        WriteChapterRequest(
            goals="保存改后稿",
            must_include=["青霜剑"],
            characters=["林默"],
            chapter_number=22,
            target_words=500,
            save=True,
        ),
        max_rounds=1,
    )
    session.commit()
    assert result.saved_chapter_id
    chapter = session.get(Chapter, result.saved_chapter_id)
    assert chapter is not None and chapter.status == "DRAFT"
    assert "青霜剑" in chapter.content
    assert chapter.content == result.draft.content


def test_revision_loop_records_style_history(session, novel):
    from app.models import StyleReview

    loop = extraction_service.build_agents("offline")["revision"]
    loop.run(
        session,
        novel,
        WriteChapterRequest(goals="留下评审记录", characters=["林默"], target_words=400),
        max_rounds=1,
        persist_reviews=True,
    )
    session.commit()
    rows = list(session.scalars(select(StyleReview).where(StyleReview.novel_id == novel.id)))
    labels = {row.label for row in rows}
    assert "draft" in labels
    assert any(row.metrics for row in rows)


def test_revision_endpoint(client):
    novel = client.post("/api/novels", json={"title": "修订接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    response = client.post(
        f"/api/novels/{novel['id']}/ai/revise-chapter?provider=offline",
        json={
            "goals": "林默追踪血河教线索到废祠",
            "must_include": ["废祠"],
            "characters": ["林默"],
            "chapter_number": 21,
            "target_words": 700,
            "max_rounds": 1,
            "use_model_critic": False,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["draft"]["content"]
    assert body["rounds"]
    assert body["final_score"] >= 0
    assert body["provider"] == "offline"
    assert "deltas" in body["metric_deltas"]

    saved = client.post(
        f"/api/novels/{novel['id']}/ai/revise-chapter?provider=offline",
        json={
            "goals": "存成章节",
            "characters": ["林默"],
            "chapter_number": 22,
            "target_words": 400,
            "max_rounds": 0,
            "save": True,
        },
    ).json()
    assert saved["saved_chapter_id"]
    chapters = client.get(f"/api/novels/{novel['id']}/chapters").json()
    assert any(item["chapter_number"] == 22 for item in chapters)


def test_style_endpoints(client):
    novel = client.post("/api/novels", json={"title": "文风接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")

    assert client.get(f"/api/novels/{novel['id']}/style/baseline").json() is None
    built = client.post(f"/api/novels/{novel['id']}/style/baseline", json={"name": "测试基线"})
    assert built.status_code == 200, built.text
    profile = built.json()
    assert profile["sample_count"] >= 3 and profile["is_default"] is True

    chapters = client.get(f"/api/novels/{novel['id']}/chapters").json()
    review = client.post(
        f"/api/chapters/{chapters[13]['chapter_id']}/style-review?use_model=false"
    ).json()
    assert review["chapter_number"] == 14
    assert "burstiness" in review["metrics"]
    assert review["review_id"]

    text_review = client.post(
        f"/api/novels/{novel['id']}/style/review-text",
        json={"text": "他缓缓地抬起头，眼中闪过一丝寒芒。" * 10, "use_model": False},
    ).json()
    assert any(issue["code"] == "CLICHE_DENSE" for issue in text_review["issues"])

    history = client.get(f"/api/novels/{novel['id']}/style/reviews").json()
    assert history, "评审历史应可查询"
    assert history[0]["metrics"]


def test_dashboard_includes_quality_signals(client):
    novel = client.post("/api/novels", json={"title": "质量看板"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")
    client.post(f"/api/novels/{novel['id']}/style/baseline", json={"name": "看板基线"})
    client.post(
        f"/api/novels/{novel['id']}/commitments",
        json={"source_chapter": 4, "what": "三日内到驿站取信", "quote": "信三日内到驿站。", "deadline_text": "三日内"},
    )
    board = client.get(f"/api/novels/{novel['id']}/dashboard").json()
    assert board["style_baseline"] == "看板基线"
    assert board["invariant_errors"] == 0
    assert "commitments_open" in board
    assert "invariant_codes" in board

    sweep = client.post(f"/api/novels/{novel['id']}/sweep", json={"mode": "rules"}).json()
    assert "invariants" in sweep["detail"]
    assert sweep["detail"]["invariants"]["report_id"]
    assert sweep["errors"] == 4
