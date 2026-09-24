"""数据库引擎与会话管理。"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


def build_engine(db_path=None) -> Engine:
    url = settings.database_url if db_path is None else f"sqlite+pysqlite:///{str(db_path)}"
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):  # pragma: no cover - 驱动回调
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()

    return engine


engine = build_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db(target_engine: Engine | None = None) -> list[str]:
    """建表 + 幂等迁移。引擎可注入，便于测试使用临时库。返回本次迁移动作。"""
    from app import models  # noqa: F401  确保模型已注册到 metadata
    from app.migrations import migrate

    bound = target_engine or engine
    Base.metadata.create_all(bind=bound)
    return migrate(bound)


def get_session() -> Iterator[Session]:
    """FastAPI 依赖：每个请求一个会话。"""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
