"""V0.2 全量一致性扫描与总览看板。"""

from __future__ import annotations

from sqlalchemy import select

from app.models import SweepRun
from app.schemas import SweepRequest
from app.services import sweep_service

EXPECTED = {
    14: "FACT_CONFLICT",
    15: "DEAD_CHARACTER_ACTIVE",
    18: "TIMELINE_INVERSION",
    20: "INCAPACITATED_CHARACTER_ACTIVE",
}


def test_rules_sweep_covers_every_chapter(session, novel):
    sweep = sweep_service.run_sweep(session, novel, SweepRequest(mode="rules"))
    session.commit()
    assert sweep.chapters_total == 20
    assert sweep.chapters_checked == 20
    assert sweep.finished_at is not None
    by_chapter = {item["chapter_number"]: item for item in sweep.detail["chapters"]}
    errored = {number for number, item in by_chapter.items() if item["errors"]}
    assert errored == set(EXPECTED), f"规则模式应报出这些章：{sorted(EXPECTED)}，实际 {sorted(errored)}"
    for number, code in EXPECTED.items():
        assert code in by_chapter[number]["codes"]
    assert sweep.detail["error_totals"] == {
        "FACT_CONFLICT": 1,
        "DEAD_CHARACTER_ACTIVE": 1,
        "TIMELINE_INVERSION": 1,
        "INCAPACITATED_CHARACTER_ACTIVE": 1,
    }
    assert sweep.errors == 4


def test_rules_sweep_reuses_existing_extraction(session, novel, chapters):
    """先做过抽取的章节，扫描时会复用那份抽取结果（不必再花模型调用）。"""
    from app.services import extraction_service

    extraction_service.run_extraction(session, novel, chapters[14], provider_name="offline")
    session.commit()
    sweep = sweep_service.run_sweep(session, novel, SweepRequest(mode="rules", chapter_numbers=[14]))
    session.commit()
    detail = sweep.detail["chapters"][0]
    assert detail["chapter_number"] == 14
    assert detail["extraction"] == "model"
    assert "FACT_CONFLICT" in detail["codes"]


def test_sweep_can_target_specific_chapters(session, novel):
    sweep = sweep_service.run_sweep(
        session, novel, SweepRequest(mode="rules", chapter_numbers=[6, 7])
    )
    session.commit()
    assert sweep.chapters_total == 2 and sweep.chapters_checked == 2
    assert sweep.errors == 0, "第 6、7 章没有埋设矛盾"


def test_full_sweep_runs_extraction(session, novel):
    sweep = sweep_service.run_sweep(
        session, novel, SweepRequest(mode="full", chapter_numbers=[15])
    )
    session.commit()
    detail = sweep.detail["chapters"][0]
    assert detail["extraction"] == "model"
    assert "DEAD_CHARACTER_ACTIVE" in detail["codes"]
    assert sweep.provider == "offline"


def test_sweep_runs_are_recorded(session, novel):
    first = sweep_service.run_sweep(session, novel, SweepRequest(mode="rules", chapter_numbers=[6]))
    second = sweep_service.run_sweep(session, novel, SweepRequest(mode="rules", chapter_numbers=[7]))
    session.commit()
    rows = list(
        session.scalars(
            select(SweepRun).where(SweepRun.novel_id == novel.id).order_by(SweepRun.started_at)
        )
    )
    assert len(rows) == 2
    assert sweep_service.latest_sweep(session, novel.id).id == second.id
    assert first.id != second.id


def test_dashboard_aggregates_health(session, novel):
    sweep_service.run_sweep(session, novel, SweepRequest(mode="rules"))
    session.commit()
    board = sweep_service.dashboard(session, novel)
    assert board["chapter_count"] == 20
    assert board["error_totals"]["FACT_CONFLICT"] == 1
    assert set(board["error_chapters"]) == set(EXPECTED)
    assert board["unchecked_chapters"] == []
    assert board["latest_sweep"] is not None
    health = {item["chapter_number"]: item for item in board["chapters"]}
    assert health[15]["errors"] == 1 and health[15]["top_codes"] == ["DEAD_CHARACTER_ACTIVE"]
    assert health[6]["errors"] == 0
    assert board["foreshadowing_debt"], "应带出伏笔欠账"
    assert board["overdue_foreshadowing"] >= 1
    assert board["vector_index"]["records"] > 0


def test_dashboard_before_any_check(session, novel):
    board = sweep_service.dashboard(session, novel)
    assert len(board["unchecked_chapters"]) == 20
    assert board["error_totals"] == {}
    assert board["latest_sweep"] is None
    assert any("从未做过一致性检查" in note for note in board["notes"])


def test_dashboard_notes_pending_review(session, novel, chapters):
    from app.services import extraction_service

    extraction_service.run_extraction(session, novel, chapters[14], provider_name="offline")
    session.commit()
    board = sweep_service.dashboard(session, novel)
    assert board["proposed_backlog"] >= 1
    assert board["pending_review_items"] >= 1
    assert any("PROPOSED" in note for note in board["notes"])


def test_sweep_endpoint_and_dashboard_endpoint(client):
    novel = client.post("/api/novels", json={"title": "扫描"}).json()
    empty = client.post(f"/api/novels/{novel['id']}/sweep", json={"mode": "rules"})
    assert empty.status_code == 409, "没有章节时不应扫描"

    client.post(f"/api/novels/{novel['id']}/seed")
    response = client.post(f"/api/novels/{novel['id']}/sweep", json={"mode": "rules"})
    assert response.status_code == 200
    body = response.json()
    assert body["chapters_checked"] == 20 and body["errors"] == 4

    sweeps = client.get(f"/api/novels/{novel['id']}/sweeps").json()
    assert len(sweeps) == 1

    board = client.get(f"/api/novels/{novel['id']}/dashboard").json()
    assert board["latest_sweep"]["id"] == body["id"]
    assert board["error_chapters"] == [14, 15, 18, 20]


def test_retrieval_and_vector_endpoints(client):
    novel = client.post("/api/novels", json={"title": "检索接口"}).json()
    client.post(f"/api/novels/{novel['id']}/seed")

    result = client.get(f"/api/novels/{novel['id']}/retrieval?q=青霜剑&limit=5").json()
    assert result["engine"] == "hybrid"
    assert result["hits"] and result["hits"][0]["ref_type"] == "CANON_FACT"

    keyword_only = client.get(
        f"/api/novels/{novel['id']}/retrieval?q=青霜剑&use_vector=false&limit=5"
    ).json()
    assert keyword_only["engine"] == "keyword"
    assert all(hit["vector_score"] == 0 for hit in keyword_only["hits"])

    stats = client.get(f"/api/novels/{novel['id']}/vectors").json()
    assert stats["records"] > 0
    reindexed = client.post(f"/api/novels/{novel['id']}/vectors/reindex").json()
    assert reindexed["provider"] == "local-ngram"
    assert reindexed["chapter_chunks"] > 0
