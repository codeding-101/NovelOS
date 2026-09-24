"""测试小说装载与基础结构（需求第九节的数据要求）。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Character,
    Event,
    Foreshadowing,
    Novel,
    TimelineEntry,
    WorldRule,
)
from app.services import search_service


def test_seed_counts(session, novel):
    chapters = list(session.scalars(select(Chapter).where(Chapter.novel_id == novel.id)))
    characters = list(session.scalars(select(Character).where(Character.novel_id == novel.id)))
    events = list(session.scalars(select(Event).where(Event.novel_id == novel.id)))
    facts = list(session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)))
    foreshadowings = list(session.scalars(select(Foreshadowing).where(Foreshadowing.novel_id == novel.id)))
    timeline = list(session.scalars(select(TimelineEntry).where(TimelineEntry.novel_id == novel.id)))
    rules = list(session.scalars(select(WorldRule).where(WorldRule.novel_id == novel.id)))

    assert len(chapters) == 20, "测试小说应有 20 章"
    assert len(characters) == 5, "测试小说应有 5 个人物"
    assert len(events) >= 10, "测试小说应有至少 10 个事件"
    assert len(foreshadowings) == 5, "测试小说应有 5 个伏笔"
    assert len([f for f in facts if f.status == CanonStatus.CANON]) >= 10, "至少 10 条 Canon 事实"
    assert len(timeline) >= 10
    assert len(rules) == 5

    session.refresh(novel)
    assert novel.chapter_count == 20
    assert novel.word_count > 20_000, "20 章正文合计应超过 2 万字"


def test_markdown_files_written(session, novel):
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel.id).order_by(Chapter.chapter_number)
        )
    )
    for chapter in chapters:
        path = Path(chapter.content_path)
        assert path.exists(), f"第{chapter.chapter_number}章的 Markdown 文件应存在"
        text = path.read_text(encoding="utf-8")
        assert text.startswith(f"# 第{chapter.chapter_number}章"), "文件应以标题行开头"
        assert chapter.content.strip() in text


def test_chapter_field_contract(session, novel):
    chapter = session.scalar(
        select(Chapter).where(Chapter.novel_id == novel.id, Chapter.chapter_number == 8)
    )
    assert chapter is not None
    assert chapter.title and chapter.content and chapter.word_count > 0
    assert chapter.created_at is not None and chapter.updated_at is not None


def test_full_text_search(session, novel):
    result = search_service.search_chapters(session, novel.id, "青霜剑", limit=5)
    assert result["engine"] in ("fts5-trigram", "like")
    assert result["hits"], "应能检索到含「青霜剑」的章节"
    assert all(hit["chapter_number"] >= 1 for hit in result["hits"])

    short = search_service.search_chapters(session, novel.id, "青霜", limit=5)
    assert short["engine"] == "like", "两字查询应回退到 LIKE 扫描"
    assert short["hits"]

    misses = search_service.search_chapters(session, novel.id, "宇宙飞船停靠站", limit=5)
    assert misses["hits"] == []


def test_character_dynamic_state(session, novel):
    zhao = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "赵铁山")
    )
    wang = session.scalar(
        select(Character).where(Character.novel_id == novel.id, Character.name == "王烈")
    )
    assert zhao is not None and wang is not None
    assert zhao.current_status == "死亡"
    assert wang.current_status == "重伤昏迷"
    assert zhao.first_appearance == 1 and zhao.last_appearance == 8


def test_seed_is_idempotent_guard(session):
    from app.services import seed_service

    novel = Novel(title="重复装载", slug="dup-check", target_word_count=1000)
    session.add(novel)
    session.flush()
    seed_service.load_seed(session, novel)
    session.commit()
    try:
        seed_service.load_seed(session, novel)
    except ValueError as exc:
        assert "已有章节" in str(exc)
    else:  # pragma: no cover - 守卫失效时才会走到
        raise AssertionError("未装载保护应阻止重复装载")
    settings.data_dir.exists()
