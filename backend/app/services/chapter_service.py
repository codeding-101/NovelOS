"""章节服务：章节 CRUD、Markdown 正文落盘、检索索引同步。

正文同时保存在两处：
- SQLite chapters.content（应用读写与全文检索的事实来源，支撑百万字规模的分页与查询）
- Markdown 文件 data/novels/<slug>/chNNN.md（作者可直接查看、可用 Git 版本管理、可导出）
两者只通过本模块的写入函数更新，因此不会出现各自漂移。
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import CanonFact, CanonStatus, Chapter, ChapterStatus, Novel
from app.schemas import ChapterCreate, ChapterUpdate
from app.services import search_service, vector_service
from app.timeutil import count_words, parse_story_time, story_time_sort_key

_TITLE_LINE = re.compile(r"^#\s*(.+)$")


def next_chapter_number(session: Session, novel_id: str) -> int:
    current = session.scalar(
        select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel_id)
    )
    return int(current or 0) + 1


def chapter_file_path(novel: Novel, chapter_number: int) -> Path:
    return settings.chapter_file(novel.slug, chapter_number)


def render_markdown(chapter: Chapter) -> str:
    heading = f"# 第{chapter.chapter_number}章 {chapter.title}".rstrip()
    return f"{heading}\n\n{(chapter.content or '').strip()}\n"


def write_chapter_file(novel: Novel, chapter: Chapter) -> Path:
    path = chapter_file_path(novel, chapter.chapter_number)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(chapter), encoding="utf-8")
    chapter.content_path = str(path)
    return path


def read_markdown(path: Path) -> tuple[str, str]:
    """读取 Markdown 正文文件，返回 (标题, 正文)。"""
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    title = ""
    body_start = 0
    if lines and _TITLE_LINE.match(lines[0].strip()):
        raw_title = _TITLE_LINE.match(lines[0].strip()).group(1).strip()
        title = re.sub(r"^第[零〇一二三四五六七八九十百千两\d]+章[\s　:：]*", "", raw_title).strip()
        body_start = 1
    body = "\n".join(lines[body_start:]).strip()
    return title, body


def create_chapter(session: Session, novel: Novel, payload: ChapterCreate) -> Chapter:
    number = payload.chapter_number or next_chapter_number(session, novel.id)
    existing = session.scalar(
        select(Chapter).where(Chapter.novel_id == novel.id, Chapter.chapter_number == number)
    )
    if existing is not None:
        raise ValueError(f"第{number}章已存在")
    chapter = Chapter(
        novel_id=novel.id,
        chapter_number=number,
        title=payload.title or f"第{number}章",
        content=payload.content or "",
        summary=payload.summary or "",
        status=payload.status,
        story_time=payload.story_time,
        location=payload.location,
    )
    chapter.word_count = count_words(chapter.content)
    session.add(chapter)
    session.flush()
    write_chapter_file(novel, chapter)
    search_service.index_chapter(session, chapter)
    vector_service.index_chapter(session, novel.id, chapter)
    return chapter


def update_chapter(session: Session, novel: Novel, chapter: Chapter, payload: ChapterUpdate) -> Chapter:
    data = payload.model_dump(exclude_unset=True)
    new_number = data.pop("chapter_number", None)
    if new_number and new_number != chapter.chapter_number:
        clash = session.scalar(
            select(Chapter).where(
                Chapter.novel_id == novel.id,
                Chapter.chapter_number == new_number,
                Chapter.id != chapter.id,
            )
        )
        if clash is not None:
            raise ValueError(f"第{new_number}章已存在")
        old_path = Path(chapter.content_path) if chapter.content_path else None
        chapter.chapter_number = new_number
        if old_path and old_path.exists():
            old_path.unlink()
    for key, value in data.items():
        setattr(chapter, key, value)
    if "content" in data:
        chapter.word_count = count_words(chapter.content)
    session.flush()
    write_chapter_file(novel, chapter)
    search_service.index_chapter(session, chapter)
    vector_service.index_chapter(session, novel.id, chapter)
    return chapter


def delete_chapter(session: Session, novel: Novel, chapter: Chapter) -> None:
    """删除章节：连同正文文件、检索/向量索引一起清理。

    由该章抽出、尚未确认的 PROPOSED 事实会被标记为 REJECTED 并注明原因 ——
    正文都没了，这些候选项不可能再被核实；已确认的 CANON / SUPERSEDED 事实不动，
    它们可能已经被后续章节引用，是否调整由作者决定。
    """
    path = Path(chapter.content_path) if chapter.content_path else chapter_file_path(novel, chapter.chapter_number)
    search_service.remove_chapter(session, chapter.id)
    vector_service.remove_ref(session, novel.id, "CHAPTER", chapter.id)
    orphans = list(
        session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.source_chapter == chapter.chapter_number,
                CanonFact.status.in_((CanonStatus.PROPOSED, CanonStatus.REJECTED)),
            )
        )
    )
    for fact in orphans:
        fact.status = CanonStatus.REJECTED
        fact.note = (fact.note + "（来源章节已删除）").strip()
    if path.exists():
        path.unlink()
    session.delete(chapter)
    session.flush()


def import_chapters_from_dir(
    session: Session,
    novel: Novel,
    directory: Path,
    meta: dict[int, dict] | None = None,
) -> list[Chapter]:
    """从 Markdown 目录导入章节（测试小说种子数据使用）。"""
    meta = meta or {}
    created: list[Chapter] = []
    for path in sorted(directory.glob("ch*.md")):
        match = re.search(r"ch(\d+)", path.stem)
        if not match:
            continue
        number = int(match.group(1))
        title, body = read_markdown(path)
        extra = meta.get(number, {})
        chapter = Chapter(
            novel_id=novel.id,
            chapter_number=number,
            title=title or f"第{number}章",
            content=body,
            summary=extra.get("summary", ""),
            word_count=count_words(body),
            status=extra.get("status", ChapterStatus.COMPLETED),
            story_time=extra.get("story_time"),
            location=extra.get("location"),
        )
        session.add(chapter)
        session.flush()
        write_chapter_file(novel, chapter)
        search_service.index_chapter(session, chapter)
        vector_service.index_chapter(session, novel.id, chapter)
        created.append(chapter)
    return created


def story_time_key(value: str | None) -> int:
    return story_time_sort_key(value)


def story_time_parts(value: str | None) -> tuple[int | None, int | None, int | None]:
    return parse_story_time(value)
