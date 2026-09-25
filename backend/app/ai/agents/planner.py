"""PlannerAgent：根据小说当前状态规划接下来几章。

输入是「截至最新一章的 Canon + 人物弧线 + 伏笔欠账 + 最近章节摘要 + 已排但未写的计划」，
输出严格 JSON 的章节计划；计划落库后可以直接交给 ChapterWriter 写作。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest, AIResponseFormatError
from app.models import Chapter, ChapterPlan, Novel
from app.schemas import ChapterPlanResult
from app.services import foreshadow_service, fragment_service, novel_service, query_service

PLAN_SCHEMA = ChapterPlanResult.model_json_schema()

#: 与 plan_service 保持一致：这些状态的人物不能作为出场人物
_UNAVAILABLE_TOKENS = ("死亡", "已死", "重伤昏迷", "昏迷", "失踪", "被囚", "封印")


def _unavailable(characters: list[dict[str, Any]]) -> dict[str, str]:
    return {
        str(character.get("name")): str(character.get("current_status"))
        for character in characters
        if any(token in str(character.get("current_status") or "") for token in _UNAVAILABLE_TOKENS)
    }


class PlannerAgent:
    task = "plan"

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 上下文
    def build_context(
        self,
        session: Session,
        novel: Novel,
        *,
        from_chapter: int,
        count: int,
        steer: str = "",
    ) -> dict[str, Any]:
        frontier = session.scalar(
            select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel.id)
        ) or 0
        canon = query_service.canon_as_of(session, novel.id, frontier)
        characters = [
            query_service.character_to_dict(character)
            for character in session.scalars(
                select(query_service.Character).where(
                    query_service.Character.novel_id == novel.id
                )
            )
        ]
        timeline = query_service.list_timeline(
            session, novel.id, status=None, limit=200, as_of_chapter=frontier
        )[-10:]
        recent = list(
            session.scalars(
                select(Chapter)
                .where(Chapter.novel_id == novel.id, Chapter.chapter_number <= frontier)
                .order_by(Chapter.chapter_number.desc())
                .limit(4)
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
        debt = foreshadow_service.debt(session, novel)
        unplaced = fragment_service.unplaced_fragments(session, novel.id, limit=12)
        planned = list(
            session.scalars(
                select(ChapterPlan)
                .where(ChapterPlan.novel_id == novel.id, ChapterPlan.chapter_number >= from_chapter)
                .order_by(ChapterPlan.chapter_number)
            )
        )
        return {
            "frontier": frontier,
            "from_chapter": from_chapter,
            "count": count,
            "steer": steer,
            "canon_facts": canon,
            "characters": characters,
            "timeline": timeline,
            "recent_summaries": recent_summaries,
            "foreshadowing_debt": debt,
            "unplaced_fragments": unplaced,
            "existing_plans": [
                {
                    "chapter_number": plan.chapter_number,
                    "title": plan.title,
                    "goals": plan.goals,
                }
                for plan in planned
            ],
            "world_rules": query_service.list_world_rules(session, novel.id),
        }

    # ------------------------------------------------------------------ 生成
    def plan(
        self,
        session: Session,
        novel: Novel,
        *,
        from_chapter: int | None = None,
        count: int = 3,
        steer: str = "",
    ) -> tuple[ChapterPlanResult, dict[str, Any]]:
        frontier = session.scalar(
            select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel.id)
        ) or 0
        start = from_chapter or frontier + 1
        context = self.build_context(
            session, novel, from_chapter=start, count=count, steer=steer
        )
        prompt = prompts.PLAN_USER.format(
            from_chapter=start,
            count=count,
            steer=steer or "（无额外要求）",
            novel_context=novel_service.novel_context(novel)
            if novel_service.has_book_setting(novel)
            else "（这本书还没有填简介/世界观/大纲：作者可以在「总览」里补上，之后规划会按它来安排章节）",
            canon_facts="\n".join(
                f"{fact['subject']} {fact['predicate']} {fact['object']}"
                f"（第{fact['source_chapter']}章起）"
                for fact in context["canon_facts"]
            )
            or "（无）",
            characters="\n".join(
                f"{c['name']}｜状态={c['current_status']}｜位置={c['current_location']}｜"
                f"目标={'；'.join(c['goals']) or '—'}｜恐惧={'；'.join(c['fears']) or '—'}"
                for c in context["characters"]
            )
            or "（无）",
            unavailable="\n".join(
                f"{name}（{status}）"
                for name, status in _unavailable(context["characters"]).items()
            )
            or "（无）",
            recent_summaries="\n".join(
                f"第{item['chapter_number']}章 {item['title']}：{item['summary']}"
                for item in context["recent_summaries"]
            )
            or "（无）",
            foreshadowing="\n".join(
                f"{item['name']}｜状态={item['status']}｜自第{item['last_reinforced_chapter']}章后"
                f"已 {item['age']} 章未回应｜相关人物={'、'.join(item['related_characters']) or '—'}"
                for item in context["foreshadowing_debt"]
            )
            or "（无）",
            timeline="\n".join(
                f"{entry['story_time']}｜{entry['event']}｜{entry['location']}"
                for entry in context["timeline"]
            )
            or "（无）",
            existing_plans="\n".join(
                f"第{item['chapter_number']}章 {item['title']}：{item['goals']}"
                for item in context["existing_plans"]
            )
            or "（无）",
            unplaced_fragments="\n".join(
                f"- [{item['kind']}] {item['title'] or ''}：{(item['text'] or '')[:80]}"
                + (f"（意图：{item['intent'][:40]}）" if item.get("intent") else "")
                for item in context["unplaced_fragments"]
            )
            or "（作者还没有留下未安排的想法碎片）",
        )
        request = AIRequest(
            task=self.task,
            system=prompts.PLAN_SYSTEM,
            prompt=prompt,
            context=context,
            json_schema=PLAN_SCHEMA,
            temperature=0.6,
            max_tokens=16384,
        )
        response = self.provider.generate(request)
        warnings = list(response.warnings)
        result = self._validate(response.parsed)
        if result is None and self.provider.kind == "llm":
            warnings.append("计划输出未通过 Schema 校验，已请求模型修复重试一次")
            raw = response.text or ""
            if raw and not raw.rstrip().endswith(("}", "]")):
                warnings.append("上一次输出看起来被截断了（内容太长），已要求模型压缩后重出")
            repair = AIRequest(
                task=self.task,
                system=prompts.PLAN_SYSTEM,
                prompt=(
                    prompt
                    + "\n\n【上一次输出无法通过校验】\n"
                    + raw[:1500]
                    + "\n\n请只输出这样的 JSON 对象（顶层键必须是 chapters，里面是数组，"
                    "每项一到两句话即可，不要长篇铺陈）：\n"
                    '{"chapters": [{"chapter_number": 21, "title": "…", "goals": "…", '
                    '"must_include": ["…"], "forbidden": ["…"], "characters": ["…"], '
                    '"advance_foreshadowing": ["…"], "rationale": "…"}], "notes": ""}'
                ),
                context=context,
                json_schema=PLAN_SCHEMA,
                temperature=0.0,
                max_tokens=16384,
            )
            response = self.provider.generate(repair)
            warnings.extend(response.warnings)
            result = self._validate(response.parsed)
        if result is None:
            raise AIResponseFormatError("PlannerAgent 未能获得符合 Schema 的章节计划（已重试）")

        return result, {
            "provider": response.provider,
            "model": response.model,
            "warnings": warnings,
            "context": context,
        }

    @staticmethod
    def _validate(parsed: dict[str, Any] | None) -> ChapterPlanResult | None:
        if not isinstance(parsed, dict):
            return None
        try:
            result = ChapterPlanResult.model_validate(parsed)
        except ValidationError:
            return None
        return result if result.chapters else None
