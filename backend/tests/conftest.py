"""pytest 全局配置：把数据目录指向临时目录，默认使用离线规则提供者。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

_TMP_DIR = tempfile.mkdtemp(prefix="novelos_tests_")
os.environ["NOVELOS_DATA_DIR"] = _TMP_DIR
os.environ["NOVELOS_DB_PATH"] = os.path.join(_TMP_DIR, "test.db")
os.environ["NOVELOS_AI_PROVIDER"] = "offline"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Chapter, Novel  # noqa: E402
from app.services import seed_service  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    init_db()
    yield


@pytest.fixture()
def session():
    db = SessionLocal()
    try:
        yield db
        db.commit()
    finally:
        db.close()


@pytest.fixture()
def novel(session):
    """每个用例一套独立的测试小说，避免用例之间互相污染。"""
    instance = Novel(title="剑起青云", slug="", target_word_count=1_000_000)
    session.add(instance)
    session.flush()
    instance.slug = f"test-{instance.id[-6:]}"
    session.flush()
    seed_service.load_seed(session, instance)
    session.commit()
    return instance


@pytest.fixture()
def chapters(session, novel):
    """返回 {章号: Chapter}。"""
    return {
        chapter.chapter_number: chapter
        for chapter in session.scalars(select(Chapter).where(Chapter.novel_id == novel.id))
    }


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
