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


def test_owner_review_preserves_evidence_and_blocks_stale_approval(platform):
    settings, db = platform
    task = task_input(db)
    work = task.work_id
    child = summaries.create(db, work, body(task))
    parent = summaries.create(
        db,
        work,
        body(
            task,
            level="chapter",
            request_key="chapter",
            child_summary_ids=[child],
            source_revision_ids=[],
        ),
    )
    stale = summaries.create(db, work, body(task, request_key="another"))
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)

        def payload(key):
            return {
                "outcome": "approved",
                "reason": "已核对原文",
                "expected_sha256": read(db, work, key)["sha256"],
            }

        def review(key, **changes):
            return client.post(
                f"/api/v1/works/{work}/summaries/{key}/decision",
                headers=headers,
                json=payload(key) | changes,
            )

        assert (
            client.post(
                f"/api/v1/works/{work}/summaries/{child}/decision", json=payload(child)
            ).status_code
            == 403
        )
        assert review(parent).status_code == 409
        assert review(child, expected_sha256="0" * 64).status_code == 409
        original = read(db, work, child)["document_text"]
        receipt = review(child)
        assert receipt.status_code == 200
        assert review(parent).status_code == 200
        revise_source(db, task.input_revision_id)
        assert review(stale).status_code == 409
        assert review(stale, outcome="rejected").status_code == 200
        assert review(child).json() == receipt.json()
        assert review(child, reason="另一个理由").status_code == 409
        detail = read(db, work, child)
        assert detail["status"] == "APPROVED" and detail["source_changed"]
        assert detail["document_text"] == original
        assert client.get(f"/api/v1/works/{work}/memory").json() == []
    for sql in (
        "UPDATE summary_decisions SET reason='changed'",
        "DELETE FROM summary_decisions",
    ):
        with pytest.raises(DBAPIError):
            with db.sessions.begin() as session:
                session.execute(text(sql))


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
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0015_summary_candidates")
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(SummaryCandidate)) == 0
    key = summaries.create(db, task.work_id, body(task))
    with db.sessions() as session:
        frozen = session.get(SummaryCandidate, key).document
    db.upgrade()
    assert db.require_current() == "0016_summary_decisions"
    migrated = read(db, task.work_id, key)
    assert migrated["document_text"] == frozen
    assert migrated["status"] == "CANDIDATE" and migrated["decision"] is None
    assert summaries.create(db, task.work_id, body(task)) == key
