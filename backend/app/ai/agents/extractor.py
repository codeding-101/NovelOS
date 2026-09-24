"""ExtractorAgent：输入一章正文，输出严格 JSON 的结构化抽取结果。

流程：组装上下文（已知人物/地点/事件名）→ 调 provider → Pydantic 校验 →
校验失败时带错误信息重试一次 → 仍失败则抛错（绝不把不合规结果写库）。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest, AIResponseFormatError
from app.models import Chapter, Character, Event, Novel, TimelineEntry
from app.schemas import ExtractionResult
from app.services import query_service


class ExtractorAgent:
    task = "extract"

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 上下文
    def build_context(self, session: Session, novel: Novel, chapter: Chapter) -> dict[str, Any]:
        characters = list(
            session.scalars(select(Character).where(Character.novel_id == novel.id))
        )
        character_names = [character.name for character in characters]
        locations = self._known_locations(session, novel)
        events = query_service.list_events(session, novel.id, limit=200)
        known_events: list[str] = []
        for entry in query_service.list_timeline(session, novel.id, limit=200):
            if entry["event"]:
                known_events.append(entry["event"])
        for event in events:
            head = (event["description"] or "").split("，")[0][:20]
            if head:
                known_events.append(head)
        deduped_events = list(dict.fromkeys(known_events))
        return {
            "content": chapter.content or "",
            "chapter_number": chapter.chapter_number,
            "chapter_title": chapter.title,
            "story_time": chapter.story_time,
            "location": chapter.location,
            "known_characters": character_names,
            "known_locations": locations,
            "known_events": deduped_events[:80],
        }

    def _known_locations(self, session: Session, novel: Novel) -> list[str]:
        locations: set[str] = set()
        for chapter in session.scalars(select(Chapter).where(Chapter.novel_id == novel.id)):
            if chapter.location:
                locations.add(chapter.location)
        for event in session.scalars(select(Event).where(Event.novel_id == novel.id)):
            if event.location:
                locations.add(event.location)
        for entry in session.scalars(
            select(TimelineEntry).where(TimelineEntry.novel_id == novel.id)
        ):
            if entry.location:
                locations.add(entry.location)
        for character in session.scalars(
            select(Character).where(Character.novel_id == novel.id)
        ):
            if character.current_location:
                locations.add(character.current_location)
        return sorted(locations)

    # ------------------------------------------------------------------ 执行
    def run(
        self, session: Session, novel: Novel, chapter: Chapter
    ) -> tuple[ExtractionResult, dict[str, Any]]:
        context = self.build_context(session, novel, chapter)
        schema = ExtractionResult.model_json_schema()
        user_prompt = prompts.EXTRACT_USER.format(
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            story_time_line=f"故事时间：{chapter.story_time}\n" if chapter.story_time else "",
            location_line=f"地点：{chapter.location}\n" if chapter.location else "",
            known_characters="、".join(context["known_characters"]) or "（暂无）",
            known_locations="、".join(context["known_locations"]) or "（暂无）",
            known_events="、".join(context["known_events"]) or "（暂无）",
            content=context["content"],
            schema=self._describe_schema(schema),
        )
        request = AIRequest(
            task=self.task,
            system=prompts.EXTRACT_SYSTEM,
            prompt=user_prompt,
            context=context,
            json_schema=schema,
            temperature=0.1,
            max_tokens=8192,
        )
        response = self.provider.generate(request)
        warnings = list(response.warnings)

        result, errors = self._validate(response.parsed)
        if result is not None and not self._stageable(result) and self.provider.kind == "llm":
            # 只填了 unknown/notes 这类字段：对作者来说等于「没有可审阅的东西」。
            # 只有模型才有重试的意义（离线规则提供者重跑一遍结果完全一样）。
            warnings.append("模型没有产出任何可审阅条目，已请求重试一次")
            result, errors = None, [
                "输出里没有任何可审阅条目（人物/事件/时间线/事实/伏笔/承诺全为空）"
            ]
        if result is None and self.provider.kind == "llm":
            warnings.append("首次输出未通过 Schema 校验，已请求模型修复重试一次")
            repair = AIRequest(
                task=self.task,
                system=prompts.EXTRACT_SYSTEM,
                prompt=(
                    user_prompt
                    + "\n\n【上一次的输出无法通过 Schema 校验】\n"
                    + "校验错误：" + ("；".join(errors) or "输出不是合法 JSON")
                    + "\n上一次的原始输出（截断）：\n"
                    + (response.text or "")[:2000]
                    + "\n\n请严格按下表的字段名与类型重新输出 JSON，不要任何解释。"
                ),
                context=context,
                json_schema=schema,
                temperature=0.0,
                max_tokens=8192,
            )
            response = self.provider.generate(repair)
            warnings.extend(response.warnings)
            result, errors = self._validate(response.parsed)

        if result is None:
            raise AIResponseFormatError(
                "ExtractorAgent 未能获得符合 Schema 的抽取结果（已重试），本次不写库"
            )
        if not self._stageable(result):
            warnings.append(
                "重试后仍没有可审阅条目：本章可能确实没有可抽取的信息，也可能是模型这一次没有读进去"
            )

        meta = {
            "provider": response.provider,
            "model": response.model,
            "warnings": warnings,
            "raw_response": (response.text or "")[:20000],
        }
        return result, meta

    @staticmethod
    def _describe_schema(schema: dict[str, Any]) -> str:
        """把 Pydantic 生成的 JSON Schema 转成模型易读的字段说明。

        直接给伪 JSON 会让模型按自己的习惯命名（例如把 foreshadowing 写成 content/basis），
        逐字段列出「字段名(类型,是否必填)」能显著降低这类偏差。
        """
        defs = schema.get("$defs", {})
        lines = ["【输出字段说明】（字段名必须与下表完全一致，不得自造字段名）"]
        for name, spec in schema.get("properties", {}).items():
            items = spec.get("items")
            if items is not None:
                if "$ref" in items:
                    target = defs.get(items["$ref"].split("/")[-1], {})
                    required = set(target.get("required", []))
                    sub = ", ".join(
                        f"{field}({ExtractorAgent._type_label(fspec)}{', 必填' if field in required else ''})"
                        for field, fspec in target.get("properties", {}).items()
                    )
                    lines.append(f"- {name}: 数组，元素字段：{sub}")
                else:
                    lines.append(f"- {name}: {ExtractorAgent._type_label(items)} 数组")
            else:
                lines.append(f"- {name}: {ExtractorAgent._type_label(spec)}")
        return "\n".join(lines)

    @staticmethod
    def _type_label(spec: dict[str, Any]) -> str:
        if "enum" in spec:
            return "|".join(f'"{value}"' for value in spec["enum"])
        if "anyOf" in spec:
            parts = [
                ExtractorAgent._type_label(branch)
                for branch in spec["anyOf"]
                if branch.get("type") != "null"
            ]
            label = "/".join(parts) or "any"
            if any(branch.get("type") == "null" for branch in spec["anyOf"]):
                label += "|null"
            return label
        if "items" in spec:
            return f"{ExtractorAgent._type_label(spec['items'])} 数组"
        if "$ref" in spec:
            return spec["$ref"].split("/")[-1]
        return spec.get("type", "any")

    @staticmethod
    def _validate(parsed: dict[str, Any] | None) -> tuple[ExtractionResult | None, list[str]]:
        if not isinstance(parsed, dict):
            return None, ["模型输出不是 JSON 对象"]
        try:
            result = ExtractionResult.model_validate(parsed)
        except ValidationError as exc:
            errors = [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_url=False)[:12]
            ]
            return None, errors
        if ExtractorAgent._is_empty(result):
            # 空对象也能通过 Schema，但对非空章节显然是无结果，按失败处理去触发修复重试
            return None, ["输出为空对象：所有抽取字段都为空"]
        return result, []

    @staticmethod
    def _is_empty(result: ExtractionResult) -> bool:
        return not any(
            (
                result.characters_changed,
                result.events,
                result.timeline,
                result.locations,
                result.new_facts,
                result.foreshadowing,
                result.relationships_changed,
                result.commitments,
                result.unknown,
                result.notes,
            )
        )

    @staticmethod
    def _stageable(result: ExtractionResult) -> bool:
        """是否有能落成待审项的条目（unknown/notes 不算）。"""
        return any(
            (
                result.characters_changed,
                result.events,
                result.timeline,
                result.new_facts,
                result.foreshadowing,
                result.relationships_changed,
                result.commitments,
            )
        )
