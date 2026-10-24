import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import hashlib
import io
import json
from pathlib import Path
import sqlite3
from threading import Barrier
from zipfile import ZipFile

from alembic import command
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.legacy_import import import_legacy
from app.platform.models import (
    Asset,
    AssetRevision,
    LegacyImport,
    LegacyRecord,
    ProjectionJob,
    SearchDocument,
    Work,
    now,
)
from app.platform.projections import (
    LocalFileStore,
    claim,
    deliver,
    process_batch,
    rebuild,
)
from app.platform.works import AssetInput, create_asset, append_revision
from platform_fixtures import signed_in


def seed(db, content="山风与百分号%_"):
    with db.sessions.begin() as session:
        work = Work(title="测试作品")
        session.add(work)
        session.flush()
        asset, revision = create_asset(
            session, work.id, AssetInput(title="第一章", content=content)
        )
        return work.id, asset.id, revision.id


def ready_jobs(db):
    with db.sessions.begin() as session:
        session.execute(
            update(ProjectionJob)
            .where(ProjectionJob.state == "pending")
            .values(available_at=now() - timedelta(seconds=1))
        )


def test_projection_failure_retry_corruption_rebuild_and_current_only_search(platform):
    settings, db = platform
    work, asset, revision = seed(db)
    store = LocalFileStore(settings.storage_root)

    class FailingStore:
        def put(self, *_):
            raise OSError("private path must not appear in errors")

    assert process_batch(db, FailingStore()) == 1
    with db.sessions() as session:
        job = session.scalar(select(ProjectionJob))
        assert (
            job.state == "pending"
            and job.attempts == 1
            and job.error_code == "PROJECTION_FAILED"
        )
        assert session.get(AssetRevision, revision).content == "山风与百分号%_"
        assert session.get(SearchDocument, revision) is None
    ready_jobs(db)
    assert process_batch(db, store) == 1
    with db.sessions.begin() as session:
        record = session.get(AssetRevision, revision)
        mirror = store.path(record.sha256)
        assert mirror.read_bytes() == record.content.encode()
        newer = append_revision(session, session.get(Asset, asset), 1, "新标题", "雨声")
        new_id = newer.id
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        assert (
            client.get(f"/api/v1/works/{work}/search", params={"q": "山风"}).json()
            == []
        )
        status = client.get(f"/api/v1/works/{work}/projections").json()
        assert status["indexed_current_assets"] == 0 and status["total_assets"] == 1
        process_batch(db, store)
        results = client.get(
            f"/api/v1/works/{work}/search", params={"q": "雨声"}
        ).json()
        assert results[0]["revision_id"] == new_id
        assert (
            client.get(f"/api/v1/works/{work}/search", params={"q": "%_"}).json() == []
        )
        mirror.write_bytes(b"corrupt derived file")
        with db.sessions.begin() as session:
            session.execute(delete(SearchDocument))
        assert (
            client.post(
                f"/api/v1/works/{work}/projections/rebuild", headers=headers
            ).json()["queued"]
            == 2
        )
        assert process_batch(db, store) == 2
        assert mirror.read_bytes() == "山风与百分号%_".encode()
        assert (
            client.get(f"/api/v1/works/{work}/search", params={"q": "雨声"}).json()[0][
                "id"
            ]
            == asset
        )


def test_claim_expiry_fences_late_results_and_bounded_failures(platform):
    settings, db = platform
    work, _, revision = seed(db)
    first = claim(db)
    assert first and claim(db) is None
    with db.sessions.begin() as session:
        session.execute(
            update(ProjectionJob).values(lease_until=now() - timedelta(seconds=1))
        )
    replacement = claim(db)
    assert replacement["token"] != first["token"]
    store = LocalFileStore(settings.storage_root)
    assert deliver(db, store, first) is False
    with db.sessions() as session:
        assert session.get(SearchDocument, revision) is None
    assert deliver(db, store, replacement) is True
    with db.sessions.begin() as session:
        rebuild(session, work)

    class Broken:
        def put(self, *_):
            raise OSError("offline")

    for _ in range(5):
        ready_jobs(db)
        assert process_batch(db, Broken()) == 1
    assert process_batch(db, Broken()) == 0
    with db.sessions() as session:
        job = session.scalar(select(ProjectionJob))
        assert job.state == "failed" and job.attempts == 5
        job_id = job.id
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        assert (
            client.post(
                f"/api/v1/works/{work}/projections/{job_id}/retry", headers=headers
            ).status_code
            == 200
        )
        assert process_batch(db, store) == 1
    # A worker that crashes during its fifth attempt must not leave a job running forever.
    with db.sessions.begin() as session:
        session.execute(
            update(ProjectionJob).values(
                state="running", attempts=5, lease_until=now() - timedelta(seconds=1)
            )
        )
    assert claim(db) is None
    with db.sessions() as session:
        assert session.get(ProjectionJob, job_id).state == "failed"


def test_concurrent_claims_and_transactional_outbox(platform):
    _, db = platform
    seed(db)
    barrier = Barrier(2)

    def worker(_):
        barrier.wait(timeout=10)
        return claim(db)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(worker, range(2)))
    assert sum(job is not None for job in results) == 1
    with pytest.raises(RuntimeError):
        with db.sessions.begin() as session:
            work = session.scalar(select(Work))
            create_asset(session, work.id, AssetInput(title="必须回滚"))
            raise RuntimeError("fail before commit")
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(ProjectionJob)) == 1
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1


def test_attachment_download_export_and_reference_isolation(platform):
    settings, db = platform
    work, asset, first = seed(db)
    other, _, foreign = seed(db)
    with db.sessions.begin() as session:
        _, target = create_asset(
            session, work, AssetInput(title="资料", kind="material", content="资料原文")
        )
        target_id = target.id
    raw = b"<script>never execute</script>\x00\xff"
    path = f"/api/v1/works/{work}"
    with TestClient(create_app(settings, db)) as client:
        assert client.get(path + "/attachments/unknown/download").status_code == 401
        headers = signed_in(client, settings)
        body = {
            "filename": "原附件.html",
            "data_base64": base64.b64encode(raw).decode(),
            "asset_id": asset,
        }
        assert client.post(path + "/attachments", json=body).status_code == 403
        upload = client.post(path + "/attachments", json=body, headers=headers)
        assert upload.status_code == 201, upload.text
        attachment = upload.json()
        response = client.get(path + f"/attachments/{attachment['id']}/download")
        assert response.content == raw
        assert response.headers["Content-Type"] == "application/octet-stream"
        assert response.headers["Content-Disposition"].startswith("attachment;")
        assert (
            client.get(
                f"/api/v1/works/{other}/attachments/{attachment['id']}/download"
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/attachments",
                json={**body, "filename": "../bad.txt"},
                headers=headers,
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/api/v1/works/{other}/attachments", json=body, headers=headers
            ).status_code
            == 404
        )
        ref = {
            "source_revision_id": first,
            "target_revision_id": target_id,
            "relation": "depends_on",
        }
        created = client.post(path + "/references", json=ref, headers=headers)
        assert created.status_code == 201, created.text
        assert (
            client.post(path + "/references", json=ref, headers=headers).json()
            == created.json()
        )
        assert (
            client.post(
                path + "/references",
                json={**ref, "target_revision_id": foreign},
                headers=headers,
            ).status_code
            == 404
        )
        assert (
            client.post(
                path + "/references",
                json={**ref, "target_revision_id": first},
                headers=headers,
            ).status_code
            == 422
        )
        with db.sessions.begin() as session:
            target_revision = session.get(AssetRevision, target_id)
            append_revision(
                session, session.get(Asset, target_revision.asset_id), 1, "资料", "更新"
            )
        binding = client.get(path + "/references").json()[0]
        assert binding["source_is_current"] and not binding["target_is_current"]
        with ZipFile(io.BytesIO(client.get(path + "/export").content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert archive.read(f"attachments/{attachment['id']}.bin") == raw
            assert manifest["references"][0]["target_revision_id"] == target_id
        assert client.get(path + "/projections").json()["counts"]["pending"] == 4
    with pytest.raises(DBAPIError):
        with db.engine.begin() as connection:
            connection.execute(text("DELETE FROM attachments"))


def legacy_file(path: Path, orphan=False):
    with sqlite3.connect(path) as connection:
        connection.executescript("""CREATE TABLE novels(id TEXT PRIMARY KEY,title TEXT,outline TEXT);
            CREATE TABLE chapters(id TEXT PRIMARY KEY,novel_id TEXT,chapter_number INTEGER,title TEXT,content TEXT,status TEXT);
            CREATE TABLE characters(id TEXT PRIMARY KEY,novel_id TEXT,name TEXT);
            CREATE TABLE character_states(id TEXT PRIMARY KEY,character_id TEXT,status TEXT);
            CREATE TABLE canon_facts(id TEXT PRIMARY KEY,novel_id TEXT,object TEXT,status TEXT);""")
        connection.execute("INSERT INTO novels VALUES('n1','旧书','不可丢失的大纲')")
        connection.execute(
            "INSERT INTO chapters VALUES('c1',?,1,'旧章',?,'COMPLETED')",
            ("missing" if orphan else "n1", "\ufeff旧稿\r\n"),
        )
        connection.execute("INSERT INTO characters VALUES('p1','n1','主角')")
        connection.execute("INSERT INTO character_states VALUES('s1','p1','ACTIVE')")
        connection.execute("INSERT INTO canon_facts VALUES('f1','n1','旧事实','CANON')")


def test_legacy_read_only_hashes_mapping_idempotence_and_changed_source(
    platform, tmp_path
):
    settings, db = platform
    path = tmp_path / "legacy.db"
    legacy_file(path)
    before = path.read_bytes()
    report = import_legacy(db, path, "source-1")
    assert not report["reused"] and not report["formal_memory"]
    assert path.read_bytes() == before
    with db.sessions.begin() as session:
        mapping = session.scalar(
            select(LegacyRecord).where(LegacyRecord.table_name == "chapters")
        )
        revision = session.get(AssetRevision, mapping.revision_id)
        assert revision.status == "DRAFT" and revision.content == "\ufeff旧稿\r\n"
        assert revision.sha256 == hashlib.sha256(revision.content.encode()).hexdigest()
        edited = append_revision(
            session, session.get(Asset, mapping.asset_id), 1, "新平台改稿", "新稿"
        )
        asset_id, revision_id = mapping.asset_id, edited.id
    moved = tmp_path / "renamed.db"
    moved.write_bytes(before)
    assert import_legacy(db, moved, "source-1")["reused"]
    with db.sessions() as session:
        assert session.get(Asset, asset_id).head_version == 2
        assert session.get(AssetRevision, revision_id).content == "新稿"
        assert session.scalar(select(func.count()).select_from(LegacyRecord)) == 5
    with sqlite3.connect(moved) as connection:
        connection.execute("UPDATE chapters SET content='来源已变'")
    with pytest.raises(ValueError, match="SOURCE_CHANGED"):
        import_legacy(db, moved, "source-1")
    with TestClient(create_app(settings, db)) as client:
        signed_in(client, settings)
        base = "/api/v1/works/" + report["works"][0]["work_id"]
        assert len(client.get(base + "/legacy-records").json()) == 5
        with ZipFile(io.BytesIO(client.get(base + "/export").content)) as archive:
            assert len([p for p in archive.namelist() if p.startswith("legacy/")]) == 5


def test_failed_import_rolls_back_and_concurrent_import_is_idempotent(
    platform, tmp_path
):
    _, db = platform
    bad = tmp_path / "bad.db"
    legacy_file(bad, orphan=True)
    before = bad.read_bytes()
    with pytest.raises(ValueError, match="ORPHAN"):
        import_legacy(db, bad, "broken-source")
    assert bad.read_bytes() == before
    with db.sessions() as session:
        for model in (Work, LegacyImport, LegacyRecord, ProjectionJob):
            assert session.scalar(select(func.count()).select_from(model)) == 0
    good = tmp_path / "good.db"
    legacy_file(good)
    barrier = Barrier(2)

    def concurrent(_):
        barrier.wait(timeout=10)
        return import_legacy(db, good, "same-source")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(concurrent, range(2)))
    assert sorted(r["reused"] for r in results) == [False, True]
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(Work)) == 1
        assert session.scalar(select(func.count()).select_from(ProjectionJob)) == 1


def test_m2_upgrade_backfills_jobs_without_changing_existing_text(platform_database):
    _, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0002_work_assets")
    with db.sessions.begin() as session:
        work = Work(title="升级前作品")
        session.add(work)
        session.flush()
        asset = Asset(work_id=work.id, kind="chapter")
        session.add(asset)
        session.flush()
        content = "升级前原稿\r\n"
        revision = AssetRevision(
            asset_id=asset.id,
            number=1,
            title="原章",
            content=content,
            sha256=hashlib.sha256(content.encode()).hexdigest(),
        )
        session.add(revision)
        session.flush()
        revision_id = revision.id
    db.upgrade()
    with db.sessions() as session:
        assert session.get(AssetRevision, revision_id).content == content
        job = session.scalar(select(ProjectionJob))
        assert (
            job.kind == "revision"
            and job.target_id == revision_id
            and job.state == "pending"
        )


def test_storage_adapter_rejects_paths_and_wrong_content(tmp_path):
    store = LocalFileStore(tmp_path / "files")
    with pytest.raises(ValueError):
        store.put("../outside", b"data")
    with pytest.raises(ValueError):
        store.put("0" * 64, b"wrong hash")
