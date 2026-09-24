"""REST API 冒烟测试：项目、章节、设定库、AI 工具与安全约束。"""

from __future__ import annotations

EXPECTED_TOOLS = {
    "search_chapters",
    "get_character",
    "get_character_state",
    "get_relationship",
    "get_events",
    "get_timeline",
    "get_canon_facts",
    "get_foreshadowing",
    "get_world_rules",
    "propose_canon_fact",
}


def test_health_and_providers(client):
    health = client.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["search_engine"] == "fts5-trigram"
    names = {provider["name"] for provider in health["providers"]}
    assert names == {"deepseek", "offline"}
    offline = next(item for item in health["providers"] if item["name"] == "offline")
    assert offline["kind"] == "offline" and offline["available"] is True


def test_tools_are_exposed_with_schemas(client):
    tools = client.get("/api/ai/tools").json()
    assert {tool["name"] for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool["parameters"]["type"] == "object"
        assert tool["description"]


def test_tool_call_validates_arguments(client, session, novel):
    response = client.post(
        f"/api/ai/tools/get_character?novel_id={novel.id}", json={"name": "林默"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "OK"
    assert body["data"]["current_status"] == "ACTIVE"

    missing = client.post(
        f"/api/ai/tools/get_character?novel_id={novel.id}", json={"nope": 1}
    ).json()
    assert missing["status"] == "ERROR", "参数不符合 Schema 应被拒绝"

    unknown = client.post(
        f"/api/ai/tools/get_character?novel_id={novel.id}", json={"name": "查无此人"}
    ).json()
    assert unknown["status"] == "UNKNOWN"

    bad_tool = client.post(f"/api/ai/tools/no_such_tool?novel_id={novel.id}", json={}).json()
    assert bad_tool["status"] == "ERROR"


def test_propose_canon_fact_tool_only_creates_proposed(client, session, novel):
    response = client.post(
        f"/api/ai/tools/propose_canon_fact?novel_id={novel.id}",
        json={
            "subject": "林默",
            "predicate": "佩剑",
            "object": "赤霄剑",
            "source_chapter": 14,
            "confidence": 0.9,
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "PROPOSED"
    assert "PROPOSED" in data["note"]

    canon = client.get(f"/api/novels/{novel.id}/canon-facts?status=CANON").json()
    assert all(fact["object"] != "赤霄剑" for fact in canon)

    confirm = client.post(f"/api/canon-facts/{data['id']}/confirm", json={})
    assert confirm.status_code == 200
    assert confirm.json()["status"] == "CANON"
    assert confirm.json()["superseded"], "确认新佩剑应取代旧的佩剑事实"

    reject = client.post(f"/api/canon-facts/{data['id']}/reject", json={})
    assert reject.status_code == 409, "已确认的 CANON 不能被直接驳回"


def test_novel_crud_and_empty_state(client):
    created = client.post(
        "/api/novels", json={"title": "空项目测试", "target_word_count": 300000}
    )
    assert created.status_code == 201
    novel = created.json()
    assert novel["slug"]

    stats = client.get(f"/api/novels/{novel['id']}/stats").json()
    assert stats["chapter_count"] == 0 and stats["word_count"] == 0

    search = client.get(f"/api/novels/{novel['id']}/search?q=任意").json()
    assert search["hits"] == []

    chapters = client.get(f"/api/novels/{novel['id']}/chapters").json()
    assert chapters == []

    patched = client.patch(
        f"/api/novels/{novel['id']}", json={"synopsis": "一句话简介", "genre": "玄幻"}
    ).json()
    assert patched["synopsis"] == "一句话简介" and patched["genre"] == "玄幻"

    assert client.get("/api/novels/nov_missing").status_code == 404
    assert client.delete(f"/api/novels/{novel['id']}").status_code == 204
    assert client.get(f"/api/novels/{novel['id']}").status_code == 404


def test_chapter_crud_and_search_flow(client):
    novel = client.post("/api/novels", json={"title": "章节流程"}).json()
    first = client.post(
        f"/api/novels/{novel['id']}/chapters",
        json={"title": "初章", "content": "林默握紧了青霜剑，青云山的风从石阶上刮过。", "story_time": "天启三年三月初七", "location": "青云山"},
    )
    assert first.status_code == 201
    chapter = first.json()
    assert chapter["chapter_number"] == 1
    assert chapter["word_count"] > 0

    saved = client.put(
        f"/api/chapters/{chapter['chapter_id']}",
        json={"content": chapter["content"] + "他把剑收回鞘里。", "summary": "开篇"},
    ).json()
    assert saved["word_count"] > chapter["word_count"]

    hits = client.get(f"/api/novels/{novel['id']}/search?q=青霜剑").json()
    assert hits["hits"] and hits["hits"][0]["chapter_number"] == 1

    stats = client.get(f"/api/novels/{novel['id']}/stats").json()
    assert stats["chapter_count"] == 1 and stats["word_count"] == saved["word_count"]

    duplicate = client.post(
        f"/api/novels/{novel['id']}/chapters", json={"chapter_number": 1, "title": "重复"}
    )
    assert duplicate.status_code == 409

    assert client.delete(f"/api/chapters/{chapter['chapter_id']}").status_code == 204
    assert client.get(f"/api/novels/{novel['id']}/chapters").json() == []
    assert client.get("/api/chapters/chp_missing").status_code == 404


def test_validation_rejects_bad_payload(client):
    assert client.post("/api/novels", json={"title": ""}).status_code == 422
    novel = client.post("/api/novels", json={"title": "参数校验"}).json()
    bad = client.post(
        f"/api/novels/{novel['id']}/chapters", json={"chapter_number": 0, "title": "x"}
    )
    assert bad.status_code == 422
    bad_fact = client.post(
        f"/api/novels/{novel['id']}/canon-facts",
        json={"subject": "林默", "predicate": "佩剑", "object": "剑", "confidence": 2.0},
    )
    assert bad_fact.status_code == 422


def test_seed_endpoint_and_bible(client):
    novel = client.post("/api/novels", json={"title": "装载测试"}).json()
    summary = client.post(f"/api/novels/{novel['id']}/seed").json()
    assert summary["chapters"] == 20
    assert summary["characters"] == 5
    assert len(summary["planted_conflicts"]) == 3

    again = client.post(f"/api/novels/{novel['id']}/seed")
    assert again.status_code == 409

    bible = client.get(f"/api/novels/{novel['id']}/bible").json()
    assert len(bible["characters"]) == 5
    assert len(bible["canon_facts"]) >= 10
    assert bible["timeline"] and bible["world_rules"] and bible["foreshadowings"]

    timeline = client.get(f"/api/novels/{novel['id']}/timeline").json()
    assert timeline == sorted(timeline, key=lambda item: (item["story_time_sort"], item["chapter_number"] or 0))


def test_ai_endpoints_require_selected_chapter(client):
    novel = client.post("/api/novels", json={"title": "AI 流程"}).json()
    missing = client.post("/api/chapters/chp_none/extract")
    assert missing.status_code == 404
    assert client.post(f"/api/novels/{novel['id']}/ai/ask", json={"question": "有人吗？"}).status_code == 200
