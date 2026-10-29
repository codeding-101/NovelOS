import hashlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, func, text
from sqlalchemy.exc import DBAPIError
from app.platform import summaries
from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.models import SummaryCandidate
from platform_fixtures import signed_in
from test_platform_tasks import task_input
from test_platform_memory import revise_source


def body(task, **changes):
    return summaries.SummaryInput(
        **{
            "level": "scene",
            "title": "风暴",
            "content": "林舟看见远处船影。",
            "narrative_notes": "限于林舟所见，身份未揭示。",
            "unresolved": "船上是谁？",
            "source_revision_ids": [task.input_revision_id],
            "request_key": "scene",
            **changes,
        }
    )


def read(db, work, key):
    with db.sessions() as session:
        return summaries.data(session, summaries.require_summary(session, work, key))


def test_hierarchy_provenance_hash_and_stale_propagation(platform):
    _, db = platform
    task = task_input(db)
    child = summaries.create(db, task.work_id, body(task))
    for level in ("chapter", "volume", "book"):
        child = summaries.create(
            db,
            task.work_id,
            body(
                task,
                level=level,
                request_key=level,
                source_revision_ids=[],
                child_summary_ids=[child],
            ),
        )
    row = read(db, task.work_id, child)
    assert row["status"] == "CANDIDATE"
    assert (
        hashlib.sha256(row["document_text"].encode("utf-8")).hexdigest()
        == row["sha256"]
    )
    assert not row["source_changed"]
    assert row["document"]["sources"][0]["revision_id"] == task.input_revision_id
    assert row["document"]["unresolved"] == "船上是谁？"
    with db.sessions() as session:
        saved = session.get(SummaryCandidate, child)
        assert hashlib.sha256(saved.document.encode()).hexdigest() == saved.sha256
    revise_source(db, task.input_revision_id)
    changed = read(db, task.work_id, child)
    assert changed["source_changed"]
    assert changed["document"] == row["document"]
    assert changed["sha256"] == row["sha256"]
    with pytest.raises(HTTPException):
        summaries.create(db, task.work_id, body(task, request_key="stale"))


def test_cross_work_level_and_child_freshness(platform):
    _, db = platform
    task, other = task_input(db), task_input(db)
    child = summaries.create(db, task.work_id, body(task))
    for request, work in [
        (body(task, level="volume", child_summary_ids=[child]), task.work_id),
        (body(other, child_summary_ids=[child]), other.work_id),
        (body(other, source_revision_ids=[task.input_revision_id]), other.work_id),
    ]:
        with pytest.raises(HTTPException):
            summaries.create(
                db, work, request.model_copy(update={"request_key": "bad"})
            )
    revise_source(db, task.input_revision_id)
    with pytest.raises(HTTPException):
        summaries.create(
            db,
            task.work_id,
            body(
                task,
                level="chapter",
                source_revision_ids=[],
                child_summary_ids=[child],
                request_key="parent",
            ),
        )


def test_idempotence_concurrency_and_immutable(platform):
    _, db = platform
    task = task_input(db)
    request = body(task)
    barrier = Barrier(2)

    def create():
        barrier.wait(timeout=10)
        return summaries.create(db, task.work_id, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(create), pool.submit(create)
        key = a.result(timeout=15)
        assert key == b.result(timeout=15)
    with pytest.raises(HTTPException):
        summaries.create(db, task.work_id, body(task, content="不同内容"))
    revise_source(db, task.input_revision_id)
    assert summaries.create(db, task.work_id, request) == key
    for sql in (
        "UPDATE summary_candidates SET sha256 = 'changed'",
        "DELETE FROM summary_candidates",
    ):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(text(sql))


def test_audit_failure_rolls_back(platform, monkeypatch):
    _, db = platform
    task = task_input(db)

    def fail(*args, **kwargs):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(summaries, "audit", fail)
    with pytest.raises(RuntimeError):
        summaries.create(db, task.work_id, body(task))
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SummaryCandidate)) == 0


def test_api_permissions_validation_and_pagination(platform):
    settings, db = platform
    task = task_input(db)
    url = f"/api/v1/works/{task.work_id}/summaries"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(url).status_code == 401
        headers = signed_in(client, settings)
        payload = body(task).model_dump()
        assert client.post(url, json=payload).status_code == 403
        assert (
            client.post(
                url, json=payload, headers=headers | {"Origin": "https://invalid.test"}
            ).status_code
            == 403
        )
        assert (
            client.post(
                url, json=payload | {"source_revision_ids": []}, headers=headers
            ).status_code
            == 422
        )
        assert (
            client.post(
                url,
                json=payload | {"source_revision_ids": [task.input_revision_id] * 2},
                headers=headers,
            ).status_code
            == 422
        )
        response = client.post(url, json=payload, headers=headers)
        assert response.status_code == 201
        key = response.json()["id"]
        assert client.get(url + "/" + key).json()["status"] == "CANDIDATE"
        assert len(client.get(url).json()) == 1
        assert client.get(url + "?offset=1").json() == []
        assert (
            client.get(url.replace(task.work_id, "other") + "/" + key).status_code
            == 404
        )
        assert client.get(f"/api/v1/works/{task.work_id}/memory").json() == []


def test_upgrade_leaves_old_assets_and_no_implicit_summaries(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0014_task_contexts")
    task = task_input(db)
    db.upgrade()
    assert db.require_current() == "0015_summary_candidates"
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SummaryCandidate)) == 0
    assert summaries.create(db, task.work_id, body(task))
