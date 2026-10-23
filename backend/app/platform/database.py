from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.platform.config import PlatformSettings

MIGRATIONS = Path(__file__).resolve().parents[2] / "platform_migrations"


def migration_config() -> Config:
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS))
    return config


class PlatformDatabase:
    def __init__(self, settings: PlatformSettings):
        url = make_url(settings.database_url)
        kwargs: dict = {"pool_pre_ping": True, "hide_parameters": True}
        if url.get_backend_name() == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False}
            if url.database == ":memory:":
                kwargs["poolclass"] = StaticPool
        self.engine = create_engine(url, **kwargs)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        if self.engine.dialect.name == "sqlite":

            @event.listens_for(self.engine, "connect")
            def pragma(connection, _record):
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA busy_timeout=5000")

    def upgrade(self) -> None:
        url = self.engine.url
        if (
            url.get_backend_name() == "sqlite"
            and url.database
            and url.database != ":memory:"
        ):
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
        with self.engine.begin() as connection:
            tables = set(inspect(connection).get_table_names())
            if tables and "alembic_version" not in tables:
                raise RuntimeError(
                    "Refusing an unmanaged database; select an empty platform database"
                )
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    def require_current(self) -> str:
        return require_current(self.engine)


def require_current(engine: Engine) -> str:
    with engine.connect() as connection:
        heads = MigrationContext.configure(connection).get_current_heads()
    expected = ScriptDirectory.from_config(migration_config()).get_heads()
    if set(heads) != set(expected):
        raise RuntimeError(
            "Database schema is not current; run python -m app.platform.cli upgrade"
        )
    return ",".join(heads)
