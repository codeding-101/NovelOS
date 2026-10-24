from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class PlatformSettings:
    environment: str = "development"
    database_url: str = field(default="", repr=False)
    master_key: str = field(default="", repr=False)
    public_origin: str = "http://localhost:3000"
    session_seconds: int = 12 * 60 * 60
    login_limit: int = 5
    login_window_seconds: int = 15 * 60
    storage_root: Path = field(default=ROOT / "data" / "platform" / "files")

    @classmethod
    def from_env(cls) -> PlatformSettings:
        path = ROOT / "data" / "platform" / "novelos.db"
        database_url = os.getenv("NOVELOS_DATABASE_URL", "")
        if not database_url and os.getenv("NOVELOS_DB_HOST"):
            database_url = URL.create(
                "postgresql+psycopg",
                username=os.getenv("NOVELOS_DB_USER", "novelos"),
                password=os.getenv("NOVELOS_DB_PASSWORD", ""),
                host=os.environ["NOVELOS_DB_HOST"],
                port=int(os.getenv("NOVELOS_DB_PORT", "5432")),
                database=os.getenv("NOVELOS_DB_NAME", "novelos"),
            ).render_as_string(hide_password=False)
        return cls(
            storage_root=Path(
                os.getenv(
                    "NOVELOS_STORAGE_ROOT", str(ROOT / "data" / "platform" / "files")
                )
            ),
            environment=os.getenv("NOVELOS_ENV", "development"),
            database_url=database_url or f"sqlite+pysqlite:///{path.as_posix()}",
            master_key=os.getenv("NOVELOS_MASTER_KEY", ""),
            public_origin=os.getenv(
                "NOVELOS_PUBLIC_ORIGIN", "http://localhost:3000"
            ).rstrip("/"),
        )

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.public_origin).scheme == "https"

    @property
    def session_cookie(self) -> str:
        return "__Host-novelos-session" if self.secure_cookie else "novelos_session"

    @property
    def csrf_cookie(self) -> str:
        return "__Host-novelos-csrf" if self.secure_cookie else "novelos_csrf"

    def validate(self) -> None:
        if self.environment not in {"development", "production", "test"}:
            raise ValueError("NOVELOS_ENV must be development, production or test")
        origin = urlsplit(self.public_origin)
        if (
            origin.scheme not in {"http", "https"}
            or not origin.hostname
            or origin.username
            or origin.password
            or origin.path
            or origin.query
            or origin.fragment
        ):
            raise ValueError("NOVELOS_PUBLIC_ORIGIN must be a single HTTP(S) origin")
        try:
            Fernet(self.master_key.encode())
        except (ValueError, TypeError) as exc:
            raise ValueError(
                "Set NOVELOS_MASTER_KEY using the generate-key command"
            ) from exc
        url = make_url(self.database_url)
        if url.get_backend_name() not in {"sqlite", "postgresql"}:
            raise ValueError(
                "Only SQLite development and PostgreSQL databases are supported"
            )
        if self.environment == "production":
            if not self.secure_cookie or url.get_backend_name() != "postgresql":
                raise ValueError("Production requires HTTPS and PostgreSQL")
        if (
            url.get_backend_name() == "sqlite"
            and url.database
            and url.database != ":memory:"
        ):
            # Never initialize the existing user's novel database as the new platform.
            legacy = ROOT / "data" / "novelos.db"
            if Path(url.database).resolve() == legacy.resolve():
                raise ValueError(
                    "Use a separate platform database; legacy data is imported read-only"
                )
