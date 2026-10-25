from dataclasses import replace
from datetime import timedelta
import json
import sqlite3

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import inspect, select, text

from app.platform.api import create_app
from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.legacy import inspect_legacy
from app.platform.models import (
    AuditEvent,
    BrowserSession,
    Owner,
    ProviderCredential,
    now,
)
from app.platform.security import CredentialVault, create_owner
from app.platform.worker import heartbeat

from platform_fixtures import PASSWORD, signed_in


def test_private_routes_and_legacy_bypass_are_protected(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
        for path in (
            "/api/v1/runtime",
            "/api/v1/credentials",
            "/api/novels",
            "/api/health",
            "/docs",
            "/openapi.json",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert settings.master_key not in response.text
        assert (
            client.get(
                "/api/v1/runtime", headers={"Authorization": "Bearer fake-agent-token"}
            ).status_code
            == 401
        )


def test_session_csrf_origin_revocation_and_expiry(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        token = client.cookies[settings.session_cookie]
        assert client.get("/api/v1/runtime").status_code == 200
        assert client.post("/api/v1/auth/logout").status_code == 403
        assert (
            client.post(
                "/api/v1/auth/logout",
                headers={**headers, "Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
        assert client.post("/api/v1/auth/logout", headers=headers).status_code == 200
        client.cookies.set(settings.session_cookie, token)
        assert client.get("/api/v1/runtime").status_code == 401
        signed_in(client, settings)
        with db.sessions.begin() as session:
            for record in session.scalars(select(BrowserSession)):
                record.expires_at = now() - timedelta(seconds=1)
        assert client.get("/api/v1/runtime").status_code == 401


def test_credentials_are_encrypted_and_never_echoed(platform):
    settings, db = platform
    plaintext = "PRIVATE_TEST_SECRET_123456"
    with TestClient(create_app(settings, db)) as client:
        headers = signed_in(client, settings)
        payload = {
            "name": "test-provider",
            "provider_type": "model_api",
            "secret": plaintext,
        }
        created = client.post("/api/v1/credentials", json=payload, headers=headers)
        assert created.status_code == 201
        assert plaintext not in created.text
        assert plaintext not in client.get("/api/v1/credentials").text
        duplicate = client.post("/api/v1/credentials", json=payload, headers=headers)
        assert duplicate.status_code == 409 and plaintext not in duplicate.text
        invalid = client.post(
            "/api/v1/credentials", json={**payload, "extra": plaintext}, headers=headers
        )
        assert invalid.status_code == 422 and plaintext not in invalid.text
        with db.sessions() as session:
            item = session.get(ProviderCredential, created.json()["id"])
            assert plaintext not in item.ciphertext
            assert (
                CredentialVault(settings.master_key).decrypt(item.ciphertext)
                == plaintext
            )
            assert all(
                plaintext not in json.dumps(event.details)
                for event in session.scalars(select(AuditEvent))
            )
        assert (
            client.delete(
                f"/api/v1/credentials/{created.json()['id']}", headers=headers
            ).status_code
            == 204
        )
        assert client.get("/api/v1/credentials").json() == []


def test_login_limit_persists_across_app_restart(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        for _ in range(settings.login_limit):
            response = client.post(
                "/api/v1/auth/login",
                json={"username": "owner", "password": "incorrect"},
            )
            assert response.status_code == 401
    with TestClient(create_app(settings, db)) as client:
        assert (
            client.post(
                "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
            ).status_code
            == 429
        )


def test_login_origin_large_body_and_secure_cookies(platform):
    settings, db = platform
    with TestClient(create_app(settings, db)) as client:
        assert (
            client.post(
                "/api/v1/auth/login",
                json={},
                headers={"Origin": "https://attacker.invalid"},
            ).status_code
            == 403
        )
        response = client.post(
            "/api/v1/auth/login", content=iter([b"x" * 40000, b"y" * 40000])
        )
        assert response.status_code == 413
    secure_settings = replace(settings, public_origin="https://testserver")
    with TestClient(
        create_app(secure_settings, db), base_url="https://testserver"
    ) as client:
        response = client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
        )
        cookies = response.headers.get_list("set-cookie")
        assert response.status_code == 200
        assert any(
            "__Host-novelos-session=" in cookie
            and "HttpOnly" in cookie
            and "Secure" in cookie
            for cookie in cookies
        )


def test_migration_is_explicit_idempotent_and_refuses_unknown(platform):
    settings, db = platform
    db.upgrade()
    db.require_current()
    with db.sessions() as session:
        assert session.get(Owner, "owner").username == "owner"
    with db.engine.begin() as connection:
        connection.execute(
            text("UPDATE alembic_version SET version_num='unknown_revision'")
        )
    with pytest.raises(RuntimeError, match="not current"):
        with TestClient(create_app(settings, db)):
            pass
    with pytest.raises(Exception):
        db.upgrade()
    with db.engine.connect() as connection:
        assert "owner_accounts" in inspect(connection).get_table_names()


def test_new_database_requires_upgrade_and_never_adopts_old_tables(tmp_path):
    path = tmp_path / "fresh.db"
    settings = PlatformSettings(
        environment="test",
        database_url=f"sqlite:///{path.as_posix()}",
        master_key=Fernet.generate_key().decode(),
    )
    db = PlatformDatabase(settings)
    with pytest.raises(RuntimeError):
        db.require_current()
    with db.engine.begin() as connection:
        connection.execute(text("CREATE TABLE legacy_assets (id INTEGER PRIMARY KEY)"))
    with pytest.raises(RuntimeError, match="unmanaged"):
        db.upgrade()
    with db.engine.connect() as connection:
        assert inspect(connection).get_table_names() == ["legacy_assets"]
    db.engine.dispose()


def test_owner_cannot_be_reinitialized_and_worker_state_is_honest(platform):
    settings, db = platform
    with db.sessions.begin() as session:
        with pytest.raises(ValueError, match="already exists"):
            create_owner(session, "replacement", PASSWORD)
    heartbeat(db, "test-worker")
    with TestClient(create_app(settings, db)) as client:
        signed_in(client, settings)
        runtime = client.get("/api/v1/runtime").json()
        assert runtime["workers"][0]["online"] is True
        assert runtime["capabilities"]["task_execution"] is True
        assert runtime["capabilities"]["external_execution"] is True
        assert runtime["capabilities"]["provider_api_execution"] is False
        assert runtime["capabilities"]["paid_execution"] is False
        assert runtime["capabilities"]["finalization"] is False
        heartbeat(db, "test-worker", "stopped")
        assert client.get("/api/v1/runtime").json()["workers"][0]["online"] is False


def test_legacy_inventory_preserves_content_and_marks_draft(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE novels(id TEXT)")
        connection.execute("CREATE TABLE chapters(id TEXT, content TEXT, status TEXT)")
        connection.execute("INSERT INTO chapters VALUES('c1', '保留原文', 'COMPLETED')")
    before = path.read_bytes()
    report = inspect_legacy(path)
    assert report["counts"]["chapters"] == 1
    assert report["chapters"][0]["target_status"] == "DRAFT"
    assert path.read_bytes() == before
    assert report["imports_data"] is False


def test_production_configuration_fails_closed():
    key = Fernet.generate_key().decode()
    with pytest.raises(ValueError, match="Production"):
        PlatformSettings(
            environment="production", database_url="sqlite:///:memory:", master_key=key
        ).validate()
    with pytest.raises(ValueError, match="MASTER_KEY"):
        PlatformSettings(database_url="sqlite:///:memory:").validate()
