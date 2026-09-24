"""测试小说《剑起青云》的装载器。

数据来源：
- seed/story_bible.md —— 人物/事件/时间线/Canon/伏笔/世界规则与三处故意矛盾的说明书
- seed/seed_entities.json —— 上述设定库的结构化数据
- seed/novel/ch01.md … ch20.md —— 20 章正文（Markdown）

装载后即得到需求第九节要求的测试小说：5 人物 / 10 事件 / 20 章 / 5 伏笔 / 13 条 Canon 事实，
其中第 14、15、18 章各埋了一处前后矛盾。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    Character,
    ContinuityReport,
    Event,
    ExtractionItem,
    ExtractionRun,
    Foreshadowing,
    GenerationRecord,
    MemoryQuery,
    Novel,
    Relationship,
    TimelineEntry,
    WorldRule,
)
from app.services import chapter_service, novel_service, vector_service
from app.services import search_service
from app.timeutil import story_time_sort_key

SEED_DIR = Path(__file__).resolve().parents[2] / "seed"
ENTITIES_FILE = SEED_DIR / "seed_entities.json"
CHAPTERS_DIR = SEED_DIR / "novel"


def load_entities() -> dict[str, Any]:
    return json.loads(ENTITIES_FILE.read_text(encoding="utf-8"))


def _clear(session: Session, novel: Novel) -> None:
    chapter_ids = list(
        session.scalars(select(Chapter.id).where(Chapter.novel_id == novel.id))
    )
    for model in (ExtractionItem,):
        if chapter_ids:
            run_ids = list(
                session.scalars(
                    select(ExtractionRun.id).where(ExtractionRun.chapter_id.in_(chapter_ids))
                )
            )
            if run_ids:
                session.execute(delete(model).where(ExtractionItem.run_id.in_(run_ids)))
    for model in (
        ContinuityReport,
        ExtractionRun,
        GenerationRecord,
        MemoryQuery,
        Event,
        TimelineEntry,
        Foreshadowing,
        WorldRule,
        Relationship,
        CanonFact,
        Chapter,
        Character,
    ):
        session.execute(delete(model).where(model.novel_id == novel.id))
    session.flush()
    directory = Path(novel.slug)
    from app.config import settings

    target = settings.chapters_dir / directory
    if target.exists():
        for file in target.glob("ch*.md"):
            file.unlink()


def load_seed(session: Session, novel: Novel, *, reset: bool = False) -> dict[str, Any]:
    entities = load_entities()
    existing = session.scalar(
        select(Chapter.id).where(Chapter.novel_id == novel.id).limit(1)
    )
    if existing is not None and not reset:
        raise ValueError("该小说已有章节；如需重新装载测试小说，请传 reset=true")

    if existing is not None or reset:
        _clear(session, novel)

    meta = entities["novel"]
    if not novel.synopsis:
        novel.synopsis = meta["synopsis"]
    if not novel.genre:
        novel.genre = meta["genre"]
    if not novel.worldview:
        novel.worldview = meta["worldview"]
    novel.target_word_count = novel.target_word_count or meta["target_word_count"]

    characters: dict[str, Character] = {}
    for item in entities["characters"]:
        character = Character(novel_id=novel.id, **item)
        session.add(character)
        characters[item["name"]] = character
    session.flush()

    for item in entities["relationships"]:
        left = characters.get(item["character_a"])
        right = characters.get(item["character_b"])
        if left is None or right is None:
            continue
        session.add(
            Relationship(
                novel_id=novel.id,
                character_a_id=left.id,
                character_b_id=right.id,
                relation=item["relation"],
                description=item["description"],
                source_chapter=item.get("source_chapter"),
            )
        )

    chapters = chapter_service.import_chapters_from_dir(
        session, novel, CHAPTERS_DIR, meta={int(k): v for k, v in entities["chapters"].items()}
    )
    chapter_by_number = {chapter.chapter_number: chapter for chapter in chapters}

    for item in entities["events"]:
        chapter = chapter_by_number.get(item["chapter_number"])
        session.add(
            Event(
                novel_id=novel.id,
                chapter_id=chapter.id if chapter else None,
                chapter_number=item["chapter_number"],
                time=item.get("time"),
                location=item.get("location", ""),
                characters=item.get("characters", []),
                description=item.get("description", ""),
                consequences=item.get("consequences", ""),
                status=CanonStatus.CANON,
            )
        )

    for item in entities["timeline"]:
        chapter = chapter_by_number.get(item["chapter_number"])
        session.add(
            TimelineEntry(
                novel_id=novel.id,
                story_time=item["story_time"],
                story_time_sort=story_time_sort_key(item["story_time"]),
                chapter_id=chapter.id if chapter else None,
                chapter_number=item["chapter_number"],
                event=item.get("event", ""),
                location=item.get("location", ""),
                description=item.get("description", ""),
                status=item.get("status", CanonStatus.CANON),
            )
        )

    for item in entities["canon_facts"]:
        session.add(
            CanonFact(
                novel_id=novel.id,
                subject=item["subject"],
                predicate=item["predicate"],
                object=item["object"],
                source_chapter=item.get("source_chapter"),
                valid_from_chapter=item.get("valid_from_chapter") or item.get("source_chapter") or 1,
                valid_until_chapter=item.get("valid_until_chapter"),
                status=item.get("status", CanonStatus.CANON),
                confidence=item.get("confidence", 1.0),
                visibility=item.get("visibility", "PUBLIC"),
                known_by=item.get("known_by", []),
                origin="SEED",
                note=item.get("note", ""),
            )
        )

    for item in entities["foreshadowings"]:
        session.add(Foreshadowing(novel_id=novel.id, **item))

    for item in entities["world_rules"]:
        session.add(WorldRule(novel_id=novel.id, **item))

    session.flush()
    novel_service.recount(session, novel)
    search_service.rebuild_index(session, novel.id)
    vector_index = vector_service.index_entities(session, novel.id)
    session.flush()

    return {
        "novel_id": novel.id,
        "title": novel.title,
        "chapters": len(chapters),
        "characters": len(characters),
        "events": len(entities["events"]),
        "timeline": len(entities["timeline"]),
        "canon_facts": len(entities["canon_facts"]),
        "foreshadowings": len(entities["foreshadowings"]),
        "world_rules": len(entities["world_rules"]),
        "word_count": novel.word_count,
        "vector_index": vector_index,
        "planted_conflicts": {
            "第14章": "林默腰间的佩剑，换成了赤霄剑。（与 Canon：林默佩剑为青霜剑冲突）",
            "第15章": "赵铁山按住他的肩膀……（赵铁山第8章已死亡）",
            "第18章": "『那桩血案，是天启三年三月里的事。』（Canon 记录为天启三年四月初五）",
        },
    }


__all__ = ["load_seed", "load_entities", "SEED_DIR"]
