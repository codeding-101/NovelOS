import os

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import text

from app.platform.config import PlatformSettings
from app.platform.database import PlatformDatabase
from app.platform.security import create_owner

PASSWORD = "test-only-long-password"


@pytest.fixture(params=["sqlite", "postgresql"])
def platform_database(request, tmp_path):
    if request.param == "postgresql":
        url = os.getenv("NOVELOS_TEST_POSTGRES_URL")
        if not url:
            pytest.skip(
                "Set NOVELOS_TEST_POSTGRES_URL to an isolated disposable PostgreSQL database"
            )
    else:
        url = f"sqlite+pysqlite:///{(tmp_path / 'platform.db').as_posix()}"
    settings = PlatformSettings(
        environment="test",
        database_url=url,
        master_key=Fernet.generate_key().decode(),
        public_origin="http://testserver",
    )
    db = PlatformDatabase(settings)
    # The PostgreSQL fixture creates a fresh schema per test, never drops shared tables.
    schema = None
    if request.param == "postgresql":
        from uuid import uuid4

        schema = "novelos_test_" + uuid4().hex
        with db.engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        db.engine.dispose()
        from sqlalchemy import event

        @event.listens_for(db.engine, "connect")
        def set_schema(connection, _record):
            connection.autocommit = True
            connection.execute(f'SET search_path TO "{schema}"')
            connection.autocommit = False

    yield settings, db
    if schema:
        with db.engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    db.engine.dispose()


@pytest.fixture
def platform(platform_database):
    settings, db = platform_database
    db.upgrade()
    with db.sessions.begin() as session:
        create_owner(session, "owner", PASSWORD)
    return settings, db


def signed_in(client, settings):
    response = client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert response.status_code == 200
    return {
        "X-CSRF-Token": client.cookies[settings.csrf_cookie],
        "Origin": settings.public_origin,
    }
