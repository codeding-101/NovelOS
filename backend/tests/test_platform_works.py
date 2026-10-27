import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from threading import Barrier
from zipfile import ZipFile

from alembic import command
from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.platform.api import create_app
from app.platform.database import migration_config
from app.platform.models import (
    Asset,
    AssetRevision,
    AuditEvent,
    OriginalUpload,
    Owner,
    ProviderCredential,
)
from app.platform.security import create_owner, CredentialVault
from app.platform.works import append_revision
from platform_fixtures import signed_in


def test_upgrade_keeps_m1_accounts_and_encrypted_credentials(platform_database):
    settings, db = platform_database
    with db.engine.begin() as connection:
        config = migration_config()
        config.attributes["connection"] = connection
        command.upgrade(config, "0001_control_foundation")
    vault = CredentialVault(settings.master_key)
    with db.sessions.begin() as session:
        create_owner(session, "original-owner", "long-migration-test-password")
        credential = ProviderCredential(
            name="existing",
            provider_type="model_api",
            ciphertext=vault.encrypt("test-secret"),
        )
        session.add(credential)
        session.flush()
        credential_id = credential.id
    db.upgrade()
    assert db.require_current() == "0013_context_snapshots"
    with db.sessions() as session:
        assert session.get(Owner, "owner").username == "original-owner"
        assert (
            vault.decrypt(session.get(ProviderCredential, credential_id).ciphertext)
            == "test-secret"
        )


def setup_work(client, headers):
    response = client.post("/api/v1/works", json={"title": "远行"}, headers=headers)
    assert response.status_code == 201, response.text
    return "/api/v1/works/" + response.json()["id"]


def create_chapter(client, headers, path, content="原稿\r\n第二行\n"):
    response = client.post(
        path + "/assets", json={"title": "第一章", "content": content}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_save_conflict_restore_and_immutable_database(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        initial = create_chapter(client, headers, path)
        endpoint = f"{path}/assets/{initial['asset_id']}"
        payload = {"expected_version": 1, "title": "新标题", "content": "新稿\n"}
        saved = client.post(endpoint + "/revisions", json=payload, headers=headers)
        assert saved.status_code == 201
        assert (
            client.post(
                endpoint + "/revisions", json=payload, headers=headers
            ).status_code
            == 409
        )
        assert client.get(endpoint).json()["revision"]["content"] == "新稿\n"
        restored = client.post(
            endpoint + f"/revisions/{initial['revision']['id']}/restore",
            json={"expected_version": 2},
            headers=headers,
        )
        assert restored.status_code == 201
        assert restored.json()["number"] == 3
        assert restored.json()["restored_from"] == initial["revision"]["id"]
        assert restored.json()["content"] == initial["revision"]["content"]
        assert restored.json()["sha256"] == initial["revision"]["sha256"]
        assert restored.json()["status"] == "DRAFT"
        history = client.get(endpoint + "/revisions?limit=2").json()
        assert [r["number"] for r in history] == [3, 2]
        assert "content" not in history[0]
        assert len(client.get(endpoint + "/revisions?before=2").json()) == 1
        comparison = client.get(
            endpoint + "/diff",
            params={"source": initial["revision"]["id"], "target": saved.json()["id"]},
        ).json()
        assert "-原稿" in comparison["diff"] and "+新稿" in comparison["diff"]
        assert comparison["target_title"] == "新标题"
        events = client.get(path + "/audit").json()
        assert sum(e["action"] == "asset.saved" for e in events) == 1
        assert sum(e["action"] == "asset.restored" for e in events) == 1
    for statement in (
        "UPDATE asset_revisions SET content='overwrite'",
        "DELETE FROM asset_revisions",
    ):
        with pytest.raises(DBAPIError):
            with db.engine.begin() as connection:
                connection.execute(text(statement))


def test_simultaneous_writers_have_exactly_one_winner(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        created = create_chapter(client, headers, path)
    barrier = Barrier(2)

    def writer(content):
        try:
            with db.sessions.begin() as session:
                asset = session.get(Asset, created["asset_id"])
                barrier.wait(timeout=10)
                append_revision(session, asset, 1, "第一章", content)
            return 201
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(writer, ["分支甲", "分支乙"]))
    assert sorted(results) == [201, 409]
    with db.sessions() as session:
        assert session.get(Asset, created["asset_id"]).head_version == 2
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 2
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "asset.saved")
            )
            == 1
        )


def test_hierarchy_and_cross_work_revision_isolation(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        a, b = setup_work(client, headers), setup_work(client, headers)
        volume = client.post(
            a + "/assets", headers=headers, json={"title": "卷一", "kind": "volume"}
        ).json()
        chapter = client.post(
            a + "/assets",
            headers=headers,
            json={"title": "章一", "kind": "chapter", "parent_id": volume["asset_id"]},
        ).json()
        assert (
            client.post(
                a + "/assets",
                headers=headers,
                json={
                    "title": "场景",
                    "kind": "scene",
                    "parent_id": chapter["asset_id"],
                },
            ).status_code
            == 201
        )
        assert (
            client.post(
                a + "/assets",
                headers=headers,
                json={"title": "错误场景", "kind": "scene"},
            ).status_code
            == 422
        )
        assert (
            client.post(
                b + "/assets",
                headers=headers,
                json={"title": "错误引用", "parent_id": volume["asset_id"]},
            ).status_code
            == 404
        )
        assert client.get(b + "/assets/" + chapter["asset_id"]).status_code == 404
        other = create_chapter(client, headers, b)
        endpoint = a + "/assets/" + chapter["asset_id"]
        assert (
            client.get(endpoint + "/revisions/" + other["revision"]["id"]).status_code
            == 404
        )
        assert (
            client.post(
                endpoint + "/revisions/" + other["revision"]["id"] + "/restore",
                headers=headers,
                json={"expected_version": 1},
            ).status_code
            == 404
        )
        assert (
            client.get(
                endpoint + "/diff",
                params={
                    "source": chapter["revision"]["id"],
                    "target": other["revision"]["id"],
                },
            ).status_code
            == 404
        )


def test_import_export_exact_bytes_manifest_and_no_finalization(platform):
    settings, db = platform
    raw = b"\xef\xbb\xbf" + "旧稿\r\n  空格与末尾换行\n".encode()
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        imported = client.post(
            path + "/import",
            headers=headers,
            json={"filename": "旧稿.md", "data_base64": base64.b64encode(raw).decode()},
        )
        assert imported.status_code == 201, imported.text
        first = imported.json()
        assert first["revision"]["content"].encode() == raw
        assert first["revision"]["sha256"] == hashlib.sha256(raw).hexdigest()
        response = client.get(path + "/export")
        assert response.status_code == 200
        with ZipFile(io.BytesIO(response.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert manifest["status"] == "DRAFT" and not manifest["finalization"]
            for entry in manifest["files"]:
                data = archive.read(entry["path"])
                assert hashlib.sha256(data).hexdigest() == entry["sha256"]
                assert len(data) == entry["bytes"]
            original = next(entry for entry in manifest["files"] if "filename" in entry)
            assert original["filename"] == "旧稿.md"
            assert archive.read(original["path"]) == raw
            assert archive.read(f"assets/{first['asset_id']}/v1.txt") == raw
        assert (
            client.post(
                path + "/assets",
                headers=headers,
                json={"title": "绕过", "status": "FINAL"},
            ).status_code
            == 422
        )
    with pytest.raises(DBAPIError):
        with db.engine.begin() as connection:
            connection.execute(
                text("UPDATE original_uploads SET filename='overwritten.txt'")
            )


def test_invalid_import_rolls_back_and_assets_require_login_csrf(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/api/v1/works").status_code == 401
        assert client.get("/api/v1/works/any/export").status_code == 401
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        assert (
            client.post(path + "/assets", json={"title": "未校验"}).status_code == 403
        )
        for filename, data in [
            ("../bad.txt", b"x"),
            ("C:\\bad.txt", b"x"),
            ("bad.pdf", b"x"),
            ("bad.txt", b"\xff"),
            ("bad.txt", b"\x00"),
            ("bad.txt", b"x" * (2 * 1024 * 1024 + 1)),
        ]:
            assert (
                client.post(
                    path + "/import",
                    headers=headers,
                    json={
                        "filename": filename,
                        "data_base64": base64.b64encode(data).decode(),
                    },
                ).status_code
                == 422
            )
        assert (
            client.post(
                path + "/import",
                headers=headers,
                json={"filename": "bad.txt", "data_base64": "!"},
            ).status_code
            == 422
        )
        assert client.get(path + "/assets").json() == []
        assert len(client.get(path + "/audit").json()) == 1
        assert (
            client.post(
                path + "/assets", headers=headers, json={"title": "   "}
            ).status_code
            == 422
        )
        assert (
            client.post(
                path + "/assets",
                headers=headers,
                json={"title": "大章节", "content": "文" * 40000},
            ).status_code
            == 201
        )
    with db.sessions() as session:
        assert session.scalar(select(func.count()).select_from(OriginalUpload)) == 0


def test_transaction_failure_does_not_advance_head(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        first = create_chapter(client, headers, path)
    with pytest.raises(RuntimeError, match="simulated"):
        with db.sessions.begin() as session:
            asset = session.get(Asset, first["asset_id"])
            append_revision(session, asset, 1, "新标题", "不应保存")
            raise RuntimeError("simulated crash before commit")
    with db.sessions() as session:
        assert session.get(Asset, first["asset_id"]).head_version == 1
        assert session.scalar(select(func.count()).select_from(AssetRevision)) == 1
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "asset.saved")
            )
            == 0
        )


def test_diff_handles_missing_final_newline_and_bounds_large_comparisons(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        path = setup_work(client, headers)
        first = create_chapter(client, headers, path, "旧正文")
        endpoint = f"{path}/assets/{first['asset_id']}"
        second = client.post(
            endpoint + "/revisions",
            headers=headers,
            json={"expected_version": 1, "title": "第一章", "content": "新正文"},
        ).json()
        comparison = client.get(
            endpoint + "/diff",
            params={"source": first["revision"]["id"], "target": second["id"]},
        )
        assert comparison.status_code == 200
        assert (
            "-旧正文\n\\ No newline at end of file\n+新正文\n"
            in comparison.json()["diff"]
        )
        large = client.post(
            endpoint + "/revisions",
            headers=headers,
            json={"expected_version": 2, "title": "第一章", "content": "文" * 200001},
        ).json()
        assert (
            client.get(
                endpoint + "/diff",
                params={"source": first["revision"]["id"], "target": large["id"]},
            ).status_code
            == 413
        )
        assert (
            client.get(endpoint + f"/revisions/{large['id']}").json()["content"]
            == "文" * 200001
        )
