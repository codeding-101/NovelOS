"""FragmentRealizer：把作者的想法碎片写成有文学气息的正文。

与「让 AI 写一章」的本质区别：
1. **作者的碎片是唯一的创作源**：模型只做展开，不做另起炉灶；
2. **对应关系可核对**：输出必须逐条说明「哪条碎片 → 变成哪段正文」，
   代码会双向校验（碎片原话必须真在碎片里、生成正文必须真在成文里），对不上的直接丢弃；
3. **允许不完美**：作者原话可以原样保留（QUOTED），不做「标准化」；
4. **不发明设定**：成文里出现的属性类陈述会被规则再扫一遍，没被 Canon 支持的一律报出来，
   由作者决定是接受为新设定（进 PROPOSED）还是改掉；
5. **声音优先**：改稿阶段有声音护栏（见 RevisionLoop），套话可以删，作者的用词习惯不能抹。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest, AIResponseFormatError
from app.models import Chapter, Character, Fragment, Novel
from app.schemas import FragmentPassage, FragmentRealization, FragmentRealizeRequest
from app.services import fragment_service, query_service, style_service, text_rules
from app.timeutil import count_words

MIN_QUOTE_CHARS = 4


class RealizerOutput(BaseModel):
    """模型侧的输出契约（与 FragmentRealization 同形，多一层容错）。"""

    title: str = ""
    passages: list[FragmentPassage] = Field(default_factory=list)
    undeveloped: list[dict[str, Any]] = Field(default_factory=list)
    notes: str = ""


class FragmentRealizer:
    task = "realize"

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 上下文
    def build_context(
        self, session: Session, novel: Novel, request: FragmentRealizeRequest
    ) -> dict[str, Any]:
        fragments: list[Fragment] = []
        for fragment_id in request.fragment_ids:
            fragment = session.get(Fragment, fragment_id)
            if fragment is not None and fragment.novel_id == novel.id:
                fragments.append(fragment)
        for raw in request.raw_fragments:
            if raw.strip():
                fragments.append(
                    Fragment(
                        id=f"inline-{len(fragments)}",
                        novel_id=novel.id,
                        kind="WHIM",
                        text=raw.strip(),
                        intent="",
                        origin="USER",
                        status="INBOX",
                    )
                )
        frontier = (
            session.scalar(
                select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel.id)
            )
            or 0
        )
        characters = [
            query_service.character_to_dict(character)
            for character in session.scalars(
                select(Character).where(Character.novel_id == novel.id)
            )
            if not request.characters or character.name in request.characters
        ]
        canon = query_service.canon_as_of(session, novel.id, request.chapter_number or frontier or 1)
        voice_profile = style_service.default_voice_profile(session, novel.id)
        return {
            "fragments": fragments,
            "frontier": frontier,
            "characters": characters,
            "canon_facts": canon,
            "timeline": query_service.list_timeline(
                session, novel.id, status=None, limit=50, as_of_chapter=request.chapter_number or frontier
            )[-8:],
            "foreshadowing": [
                item
                for item in query_service.list_foreshadowing(session, novel.id)
                if item["status"] != "RESOLVED"
            ][:8],
            "voice_profile": voice_profile,
            "voice_terms": (voice_profile.metrics.get("signature_terms") if voice_profile else []) or [],
        }

    # ------------------------------------------------------------------ 执行
    def realize(
        self, session: Session, novel: Novel, request: FragmentRealizeRequest
    ) -> tuple[FragmentRealization, dict[str, Any]]:
        context = self.build_context(session, novel, request)
        if not context["fragments"]:
            raise AIResponseFormatError("没有可用的碎片：请至少给一条碎片或指定 fragment_ids")

        fragment_block = "\n".join(
            f"- id={fragment.id}｜类型={fragment.kind}"
            + (f"｜标题={fragment.title}" if fragment.title else "")
            + (f"｜意图={fragment.intent}" if fragment.intent else "")
            + f"\n  原文：{fragment.text.strip()}"
            for fragment in context["fragments"]
        )
        prompt = prompts.REALIZE_USER.format(
            fragment_block=fragment_block,
            goals=request.goals or "（作者没有额外要求，按碎片本身的意思想写出来）",
            tone=request.tone or "（沿用本书已有的语气）",
            chapter_number=request.chapter_number or (context["frontier"] or 0) + 1,
            target_words=request.target_words,
            keep_original="、".join(request.must_keep) or "（无）",
            canon_facts="\n".join(
                f"{fact['subject']} {fact['predicate']} {fact['object']}（第{fact['source_chapter']}章起）"
                for fact in context["canon_facts"]
            )
            or "（无）",
            characters="\n".join(
                f"{c['name']}｜状态={c['current_status']}｜位置={c['current_location']}｜"
                f"性格={c['personality'][:40]}"
                for c in context["characters"]
            )
            or "（无）",
            timeline="\n".join(
                f"{entry['story_time']}｜{entry['event']}｜{entry['location']}"
                for entry in context["timeline"]
            )
            or "（无）",
            voice_terms="、".join(context["voice_terms"][:20]) or "（暂未建立作者声音画像）",
            schema=self._describe_schema(),
        )
        ai_request = AIRequest(
            task=self.task,
            system=prompts.REALIZE_SYSTEM,
            prompt=prompt,
            context={
                **context,
                "fragments": [fragment_service.fragment_to_dict(f) for f in context["fragments"]],
                "goals": request.goals,
                "target_words": request.target_words,
            },
            json_schema=RealizerOutput.model_json_schema(),
            temperature=0.7,
            max_tokens=8192,
        )
        response = self.provider.generate(ai_request)
        warnings = list(response.warnings)
        output = self._validate(response.parsed)
        if output is None and self.provider.kind == "llm":
            warnings.append("首次输出未通过 Schema 校验，已请求修复重试一次")
            repair = AIRequest(
                task=self.task,
                system=prompts.REALIZE_SYSTEM,
                prompt=(
                    prompt
                    + "\n\n【上一次输出无法通过校验】\n"
                    + (response.text or "")[:1500]
                    + "\n\n请只输出符合 Schema 的 JSON。"
                ),
                context=ai_request.context,
                json_schema=RealizerOutput.model_json_schema(),
                temperature=0.2,
                max_tokens=8192,
            )
            response = self.provider.generate(repair)
            warnings.extend(response.warnings)
            output = self._validate(response.parsed)
        if output is None:
            raise AIResponseFormatError("FragmentRealizer 未能获得符合 Schema 的成文结果（已重试）")

        content, passages, dropped = self._assemble(output, context["fragments"])
        warnings.extend(dropped)

        # 不发明设定：成文里新出现的属性类陈述必须报出来
        invented = self._new_attribute_claims(content, context["canon_facts"], context["characters"])
        if invented:
            warnings.append(
                "成文里出现了 Canon 未记录的设定性陈述（"
                + "、".join(f"{item['subject']}的{item['predicate']}是{item['object']}" for item in invented[:3])
                + "），请确认是接受为新设定，还是改掉"
            )

        realization = FragmentRealization(
            title=output.title or f"第{request.chapter_number or (context['frontier'] or 0) + 1}章",
            content=content,
            passages=passages,
            undeveloped=output.undeveloped,
            notes=output.notes,
            word_count=count_words(content),
            coverage=self._coverage(context["fragments"], passages),
            invented_claims=invented,
        )
        for item in request.forbidden:
            if item and item in content:
                warnings.append(f"成文里出现了禁止内容「{item}」，需要作者修改后再采用")
        return realization, {
            "provider": response.provider,
            "model": response.model,
            "warnings": warnings,
            "context": context,
        }

    # ------------------------------------------------------------------ 装配与校验
    def _assemble(
        self, output: RealizerOutput, fragments: list[Fragment]
    ) -> tuple[str, list[FragmentPassage], list[str]]:
        """按碎片顺序装配正文，并双向校验对应关系。"""
        known = {fragment.id: fragment for fragment in fragments}
        texts = {fragment.id: (fragment.text or "") for fragment in fragments}
        dropped: list[str] = []
        kept: list[FragmentPassage] = []
        for passage in output.passages:
            prose = (passage.prose or "").strip()
            if not prose:
                continue
            if passage.fragment_id == "__bridge__":
                kept.append(passage)
                continue
            fragment = known.get(passage.fragment_id)
            if fragment is None:
                dropped.append(f"丢弃一段对应关系：fragment_id「{passage.fragment_id}」不在本次碎片里")
                continue
            if passage.uses_quote and passage.uses_quote not in texts.get(fragment.id, ""):
                dropped.append(
                    f"丢弃「{passage.uses_quote[:20]}」的引用说明：该原话不在碎片原文里（防编造对应）"
                )
                passage = passage.model_copy(update={"uses_quote": "", "treatment": "PARAPHRASED"})
            kept.append(passage)
        ordered = sorted(
            kept, key=lambda item: self._order_key(item, fragments)
        )
        content = "\n\n".join(item.prose.strip() for item in ordered if item.prose.strip())
        return content, ordered, dropped

    @staticmethod
    def _order_key(passage: FragmentPassage, fragments: list[Fragment]) -> tuple[int, int]:
        if passage.fragment_id == "__bridge__":
            return (10_000, 0)
        for index, fragment in enumerate(fragments):
            if fragment.id == passage.fragment_id:
                return (index, 0)
        return (9_000, 0)

    def _coverage(self, fragments: list[Fragment], passages: list[FragmentPassage]) -> dict[str, Any]:
        used = {passage.fragment_id for passage in passages if passage.fragment_id != "__bridge__"}
        covered = [fragment.id for fragment in fragments if fragment.id in used]
        return {
            "fragments_total": len(fragments),
            "fragments_used": len(covered),
            "used_ids": covered,
            "unused_ids": [fragment.id for fragment in fragments if fragment.id not in used],
            "ratio": round(len(covered) / len(fragments), 3) if fragments else 0.0,
        }

    def _new_attribute_claims(
        self, content: str, canon_facts: list[dict[str, Any]], characters: list[dict[str, Any]]
    ) -> list[dict[str, str]]:
        """用确定性属性规则扫成文，挑出 Canon 里没有的属性陈述。

        只要「主语+谓语+宾语」这一条在 Canon 里找不到，就报给作者 ——
        即便是「同主谓、换宾语」（例如佩剑从青霜剑变成别的），那也是一条新的设定性陈述，
        作者必须自己决定是接受为新设定，还是改掉。
        """
        names = [character["name"] for character in characters] or [
            fact["subject"] for fact in canon_facts
        ]
        names = list(dict.fromkeys(names))
        known = {(fact["subject"], fact["predicate"], fact["object"]) for fact in canon_facts}
        findings: list[dict[str, str]] = []
        for item in text_rules.extract_attribute_facts(content or "", names):
            key = (item["subject"], item["predicate"], item["object"])
            if key in known:
                continue
            findings.append(item)
        return findings[:6]

    @staticmethod
    def _validate(parsed: dict[str, Any] | None) -> RealizerOutput | None:
        if not isinstance(parsed, dict):
            return None
        try:
            output = RealizerOutput.model_validate(parsed)
        except ValidationError:
            return None
        return output if output.passages else None

    @staticmethod
    def _describe_schema() -> str:
        return (
            "{\n"
            '  "title": "章节或片段的标题（字符串）",\n'
            '  "passages": [\n'
            "    {\n"
            '      "fragment_id": "碎片 id（必须来自给定的碎片列表；过渡段落用 \\"__bridge__\\"）",\n'
            '      "prose": "这段碎片变成的正文（字符串，逐字段落）",\n'
            '      "uses_quote": "若原样沿用了碎片里的原话，这里给出那句原话（必须与碎片原文完全一致）；否则空字符串",\n'
            '      "treatment": "QUOTED | PARAPHRASED | EXPANDED | BACKGROUND"\n'
            "    }\n"
            "  ],\n"
            '  "undeveloped": [{"fragment_id": "", "reason": "为什么这次没用上"}],\n'
            '  "notes": "给作者的一句话说明"\n'
            "}"
        )


def merge_back(
    session: Session, novel: Novel, fragments: list[Fragment], realization: FragmentRealization
) -> int:
    """成文后回填碎片：哪条碎片变成了哪段正文（作者可核对）。"""
    chapter = (
        session.get(Chapter, realization.saved_chapter_id)
        if realization.saved_chapter_id
        else None
    )
    updated = 0
    for passage in realization.passages:
        if passage.fragment_id == "__bridge__":
            continue
        fragment = session.get(Fragment, passage.fragment_id)
        if fragment is None or fragment.novel_id != novel.id:
            continue
        fragment_service.mark_realized(
            session,
            fragment,
            chapter_id=chapter.id if chapter else None,
            excerpt=passage.prose.strip()[:400],
            treatment=passage.treatment,
            status="REALIZED" if realization.saved_chapter_id else "PLACED",
        )
        updated += 1
    session.flush()
    return updated
