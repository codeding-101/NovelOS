"""ChapterWriter：根据作者给出的目标写作新章节。

要求里明确「写作之前必须检索相关 Canon」，因此生成前一定会组装 RetrievalBundle
（Canon 事实、人物状态、时间线、未回收伏笔、世界观规则、前文摘要），
并把这份检索快照随生成记录一起存库，便于事后审计。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest
from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    ChapterStatus,
    Character,
    ForeshadowStatus,
    GenerationRecord,
    Novel,
)
from app.schemas import (
    ChapterCreate,
    ChapterDraft,
    RetrievalBundle,
    WriteChapterRequest,
    WriteChapterResponse,
)
from app.services import chapter_service, novel_service, query_service, retrieval_service, style_service
from app.timeutil import count_words

_TITLE_LINE_RE = re.compile(r"^#+\s*(.*)$")


def _is_literal_requirement(item: str | None) -> bool:
    """这条「必须出现」是能在正文里逐字找到的具体内容吗？

    人名、台词、器物、地名这类短且不含说明性标点的算；
    「只给轮廓，不给名字全解」这种描述性要求不算 —— 它该由人对照检查，
    拿去做字符串比较只会每次都报「未找到」。
    """
    text = (item or "").strip()
    # 14 字是「名字/台词/物件」的量级；超出的多半是描述性要求。
    # 判断错的方向也是安全的：把描述当字面最多少报一次，把字面当描述只是不校验。
    if not text or len(text) > 14:
        return False
    return not any(mark in text for mark in "（）()，。；：、“”「」…—／/")


class ChapterWriter:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 检索
    def retrieve(
        self, session: Session, novel: Novel, request: WriteChapterRequest
    ) -> RetrievalBundle:
        notes: list[str] = []
        characters = list(
            session.scalars(select(Character).where(Character.novel_id == novel.id))
        )
        wanted = [name for name in request.characters if name]
        selected = [c for c in characters if not wanted or c.name in wanted]
        if wanted:
            missing = [name for name in wanted if name not in {c.name for c in characters}]
            if missing:
                notes.append("以下人物尚未建档，写作时不得引入其未记录的设定：" + "、".join(missing))
        if not selected:
            selected = characters[:8]
            notes.append("未指定相关人物，已按主要人物检索 Canon")

        selected_names = {c.name for c in selected}
        facts = []
        for fact in session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.CANON
            )
        ):
            haystack = f"{fact.subject}{fact.object}{''.join(fact.known_by or [])}"
            if not selected_names or fact.subject in selected_names or any(
                name in haystack for name in selected_names
            ):
                facts.append(query_service.canon_fact_to_dict(fact))
        if not facts:
            notes.append("未检索到与相关人物直接关联的 Canon 事实，写作时应避免新增设定性信息")

        events = sorted(
            query_service.list_events(session, novel.id, limit=200),
            key=lambda item: item["chapter_number"] or 0,
            reverse=True,
        )[:10]
        timeline = query_service.list_timeline(session, novel.id, status=CanonStatus.CANON, limit=200)
        timeline = timeline[-10:]
        foreshadowing = [
            item
            for item in query_service.list_foreshadowing(session, novel.id)
            if item["status"] != ForeshadowStatus.RESOLVED
        ][:10]
        world_rules = query_service.list_world_rules(session, novel.id)

        target_number = request.chapter_number or chapter_service.next_chapter_number(session, novel.id)
        recent = list(
            session.scalars(
                select(Chapter)
                .where(Chapter.novel_id == novel.id, Chapter.chapter_number < target_number)
                .order_by(Chapter.chapter_number.desc())
                .limit(3)
            )
        )
        recent_summaries = [
            {
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "summary": chapter.summary or (chapter.content or "")[:120],
            }
            for chapter in sorted(recent, key=lambda item: item.chapter_number)
        ]

        # 语义召回：用目标 + 人物当查询，找出「讲的是同一件事」的前文片段
        semantic_query = " ".join(
            [request.goals, *request.must_include, *request.characters]
        ).strip()
        semantic_hits = (
            retrieval_service.semantic_chapters(session, novel, semantic_query, limit=5)
            if semantic_query
            else []
        )
        if not semantic_hits:
            notes.append("向量索引里没有可用的前文片段（可执行重建索引），本次按摘要与 Canon 检索写作")

        # 作者自己的想法碎片：成文时必须优先体现
        fragments: list[dict[str, Any]] = []
        if semantic_query:
            fragments = retrieval_service.hybrid_search(
                session, novel, semantic_query, ref_types=("FRAGMENT",), limit=5
            )["hits"]
        if fragments:
            notes.append(f"本次带上了 {len(fragments)} 条作者的想法碎片（优先体现其意图）")

        return RetrievalBundle(
            canon_facts=facts,
            characters=[
                {
                    "name": character.name,
                    "current_status": character.current_status,
                    "current_location": character.current_location,
                    "description": character.description,
                    "personality": character.personality,
                    "goals": character.goals or [],
                    "fears": character.fears or [],
                }
                for character in selected
            ],
            events=events,
            timeline=timeline,
            foreshadowing=foreshadowing,
            world_rules=world_rules,
            recent_chapter_summaries=recent_summaries,
            semantic_hits=semantic_hits,
            fragments=fragments,
            notes=notes,
        )

    # ------------------------------------------------------------------ 生成
    def write(
        self, session: Session, novel: Novel, request: WriteChapterRequest
    ) -> WriteChapterResponse:
        bundle = self.retrieve(session, novel, request)
        target_number = request.chapter_number or chapter_service.next_chapter_number(session, novel.id)
        warnings: list[str] = list(bundle.notes)

        # 事前对齐：把已锁定的文风翻译成硬性要求，写之前就告诉它这本书该怎么写
        locked_profile = style_service.default_profile(session, novel.id)
        locked = bool(locked_profile is not None and getattr(locked_profile, "locked", False))
        style_lock = style_service.style_lock_directive(
            locked_profile if locked else None,
            voice_profile=style_service.default_voice_profile(session, novel.id),
        )
        if locked:
            warnings.append(f"本章按已锁定的文风写作：{locked_profile.name}")
        else:
            warnings.append(
                "本书还没有锁定文风：作者可在「质量」面板用样章建立基线并锁定，"
                "之后每一章都会按这个文风对齐"
            )
        if bundle.world_rules:
            warnings.append(f"写作提示里带上了 {len(bundle.world_rules)} 条世界观规则（不得违背）")

        novel_context = novel_service.novel_context(novel)
        has_setting = novel_service.has_book_setting(novel)
        if has_setting:
            warnings.append("写作提示里带上了本书的简介／世界观／大纲")
        else:
            warnings.append("本书还没有简介、世界观或大纲，规划与写作只能靠人物与 Canon 推断")

        prompt = prompts.WRITE_USER.format(
            goals=request.goals,
            must_include="、".join(request.must_include) or "（无）",
            forbidden="、".join(request.forbidden) or "（无）",
            characters="、".join(request.characters) or "（未指定）",
            chapter_number=target_number,
            target_words=request.target_words,
            novel_context=novel_context
            if has_setting
            else "（这本书还没有填简介/世界观/大纲：作者可以在「总览」里补上，之后每一次规划与写作都会带上）",
            canon_facts="\n".join(
                f"{fact['subject']} {fact['predicate']} {fact['object']}"
                f"（来源第{fact['source_chapter']}章）"
                for fact in bundle.canon_facts
            )
            or "（无）",
            character_states="\n".join(
                f"{character['name']}：状态={character['current_status']}，位置={character['current_location']}"
                for character in bundle.characters
            )
            or "（无）",
            timeline="\n".join(
                f"{entry['story_time']}｜{entry['event']}｜{entry['location']}"
                for entry in bundle.timeline
            )
            or "（无）",
            foreshadowing="\n".join(
                f"{item['name']}（首现第{item['first_chapter']}章）：{item['description']}"
                for item in bundle.foreshadowing
            )
            or "（无）",
            world_rules="\n".join(f"{rule['name']}：{rule['description']}" for rule in bundle.world_rules)
            or "（无）",
            recent_summaries="\n".join(
                f"第{item['chapter_number']}章 {item['title']}：{item['summary']}"
                for item in bundle.recent_chapter_summaries
            )
            or "（无）",
            semantic_hits="\n".join(
                f"第{item['chapter_number']}章：{item['excerpt']}" for item in bundle.semantic_hits
            )
            or "（无）",
            fragments="\n".join(
                f"- [{item['status']}] {item['title']}：{item['excerpt']}"
                for item in bundle.fragments
            )
            or "（作者没有为这一章留下想法碎片）",
            style_lock=style_lock
            or "（还没有锁定文风：按前文摘要与人物档案的腔调写，不要自己另起一种风格）",
        )

        request_obj = AIRequest(
            task="write",
            system=prompts.WRITE_SYSTEM,
            prompt=prompt,
            context={
                "chapter_number": target_number,
                "title": request.title or f"第{target_number}章",
                "goals": request.goals,
                "must_include": request.must_include,
                "forbidden": request.forbidden,
                "target_words": request.target_words,
                "story_time": request.story_time,
                "location": request.location
                or (bundle.characters[0]["current_location"] if bundle.characters else "")
                or (bundle.timeline[-1]["location"] if bundle.timeline else ""),
                "characters": bundle.characters,
                "canon_facts": bundle.canon_facts,
                "foreshadowing": bundle.foreshadowing,
                "timeline": bundle.timeline,
                "recent_chapter_summaries": bundle.recent_chapter_summaries,
                "semantic_hits": bundle.semantic_hits,
                "world_rules": bundle.world_rules,
                "style_lock": style_lock,
                "locked_style": locked,
                "novel_context": novel_context,
            },
            temperature=0.8,
            max_tokens=8192,
        )
        response = self.provider.generate(request_obj)
        warnings.extend(response.warnings)

        raw_content = response.text or ""
        if response.parsed and isinstance(response.parsed.get("content"), str):
            raw_content = response.parsed["content"]
            warnings.extend(item for item in (response.parsed.get("warnings") or []) if item)
        title, body = self._split_title(raw_content, request.title, target_number)
        word_count = count_words(body)

        for item in request.forbidden:
            if item and item in body:
                warnings.append(f"生成正文中出现了禁止内容「{item}」，该草稿需人工修改后才能采用")
        # 「必须出现」只在它是一条**具体的东西**（人名、台词、物件）时才能逐字校验。
        # 规划里给的往往是描述性要求（「老人先答前半句，被追问后才补上后半句」），
        # 拿去比字面只会每次都报未找到 —— 那是噪音，不是发现。
        literal_items = [item for item in request.must_include if _is_literal_requirement(item)]
        described = len([item for item in request.must_include if item and not _is_literal_requirement(item)])
        for item in literal_items:
            if item not in body:
                warnings.append(f"生成正文中未找到必须出现的内容「{item}」")
        if described:
            warnings.append(
                f"另有 {described} 条「必须出现」是描述性要求（不是字面内容），未做逐字校验，"
                "请人工对照本章目标确认"
            )
        if request.target_words and abs(word_count - request.target_words) / request.target_words > 0.25:
            warnings.append(f"生成字数 {word_count} 与目标 {request.target_words} 偏差超过 25%")

        saved_chapter_id: str | None = None
        if request.save:
            chapter = chapter_service.create_chapter(
                session,
                novel,
                ChapterCreate(
                    chapter_number=target_number,
                    title=title,
                    content=body,
                    summary=f"由 ChapterWriter 生成（{self.provider.name}/{self.provider.model}）",
                    story_time=request.story_time,
                    location=request.location,
                    status=ChapterStatus.DRAFT,
                ),
            )
            saved_chapter_id = chapter.id

        record = GenerationRecord(
            novel_id=novel.id,
            chapter_id=saved_chapter_id,
            request=request.model_dump(),
            retrieved=bundle.model_dump(),
            content=body,
            provider=response.provider,
            model=response.model,
            warnings=warnings,
        )
        session.add(record)
        session.flush()

        return WriteChapterResponse(
            draft=ChapterDraft(
                title=title,
                content=body,
                word_count=word_count,
                chapter_number=target_number,
                story_time=request.story_time,
                location=request.location,
            ),
            retrieved=bundle,
            provider=response.provider,
            model=response.model,
            warnings=warnings,
            saved_chapter_id=saved_chapter_id,
            generation_id=record.id,
        )

    @staticmethod
    def _split_title(raw_content: str, requested_title: str | None, chapter_number: int) -> tuple[str, str]:
        text = (raw_content or "").strip()
        title = requested_title or f"第{chapter_number}章"
        lines = text.splitlines()
        if lines and _TITLE_LINE_RE.match(lines[0].strip()):
            heading = _TITLE_LINE_RE.match(lines[0].strip()).group(1).strip()
            heading = re.sub(r"^第[零〇一二三四五六七八九十百千两\d]+章[\s　:：]*", "", heading).strip()
            if heading:
                title = requested_title or heading
            text = "\n".join(lines[1:]).strip()
        return title, text


__all__ = ["ChapterWriter"]
