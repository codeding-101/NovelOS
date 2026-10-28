from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from app.platform import memory_retrieval as retrieval, memory_service as memory
from app.platform.api import create_app
from platform_fixtures import signed_in
from test_platform_contexts import approved
from test_platform_memory import draft, decision, revise_source
from test_platform_story import state
from test_platform_tasks import task_input


def search(db, work_id, query, **kwargs):
    with db.sessions() as session:
        return retrieval.retrieve(session, work_id, query, **kwargs)


def test_current_heads_sources_and_work_isolation(platform):
    _, db = platform
    body = task_input(db)
    old = approved(db, body, content="林舟船医")
    pending = memory.create(
        db, body.work_id, draft(body, entry_key="候选", request_key="候选")
    )
    assert [
        item["memory"]["id"] for item in search(db, body.work_id, "林舟")["items"]
    ] == [old]
    replacement = memory.create(
        db,
        body.work_id,
        draft(
            body,
            entry_key="身份",
            base_proposal_id=old,
            request_key="替代",
            content="林舟领航员",
        ),
    )
    memory.decide(db, body.work_id, replacement, decision())
    memory.decide(db, body.work_id, pending, decision("rejected"))
    other = task_input(db)
    approved(db, other)
    result = search(db, body.work_id, "林舟")
    assert [item["memory"]["id"] for item in result["items"]] == [replacement]
    assert result["scanned"] == 1
    revise_source(db, body.input_revision_id)
    assert search(db, body.work_id, "林舟")["items"] == []


def test_time_boundaries_and_unstructured_memory(platform):
    _, db = platform
    body = task_input(db)
    timeless = approved(db, body, key="文字")
    timed = approved(
        db, body, key="阶段", story_state=state(valid_from=10, valid_until=20)
    )

    def ids(at):
        return {
            item["memory"]["id"]
            for item in search(db, body.work_id, "林舟", at=at)["items"]
        }

    assert ids(9) == {timeless}
    assert ids(10) == {timeless, timed}
    assert ids(20) == {timeless}


def test_cap_fails_instead_of_silently_truncating(platform, monkeypatch):
    _, db = platform
    body = task_input(db)
    approved(db, body, key="一")
    approved(db, body, key="二")
    result = search(db, body.work_id, "林舟", limit=1)
    assert result["matched"] == 2
    assert len(result["items"]) == 1
    assert result["items"][0]["memory"]["entry_key"] == "一"
    assert search(db, body.work_id, "完全不相关的词语")["items"] == []
    monkeypatch.setattr(retrieval, "SCAN_LIMIT", 1)
    with pytest.raises(HTTPException) as error:
        search(db, body.work_id, "林舟")
    assert error.value.status_code == 409


def test_api_auth_validation_and_read_only(platform):
    settings, db = platform
    body = task_input(db)
    approved(db, body)
    with TestClient(create_app(settings, db)) as client:
        url = f"/api/v1/works/{body.work_id}/memory/retrieve"
        assert client.get(url, params={"q": "林舟"}).status_code == 401
        signed_in(client, settings)
        for query in (" ", "%%%", "字" * 257):
            assert client.get(url, params={"q": query}).status_code == 422
        assert client.get(url, params={"q": "林舟", "limit": 51}).status_code == 422
        assert client.get(url, params={"q": "林舟", "at": -1}).status_code == 422
        result = client.get(url, params={"q": "林舟"})
        assert result.status_code == 200
        assert result.json()["items"][0]["matched_terms"] == ["林舟"]
        assert (
            client.get(
                url.replace(body.work_id, "missing"), params={"q": "林舟"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize(
    "query,expected",
    [
        ("林舟 船医", "林舟身份"),
        ("北港 禁航", "北港禁航令"),
        ("ＡＲＫ 引擎", "ARK引擎"),
    ],
)
def test_chinese_retrieval_samples(query, expected):
    corpus = [
        ("林舟身份", "林舟是一名船医，负责治疗船员。"),
        ("北港禁航令", "北港封锁期间船队不得出海。"),
        ("ARK引擎", "Ark 引擎需要晶石供能。"),
        ("晚餐", "众人在餐厅用饭。"),
    ]
    ranked = sorted(corpus, key=lambda row: -retrieval.score(query, *row, "")[0])
    assert ranked[0][0] == expected


def test_repetition_does_not_inflate_score():
    assert retrieval.score("船医", "人物", "船医", "") == retrieval.score(
        "船医", "人物", "船医" * 100, ""
    )
