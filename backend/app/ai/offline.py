"""离线规则提供者：不依赖网络与 API Key 的确定性实现。

用途有两个：
1. 单元测试与回归测试：结果完全确定，可断言；
2. 降级运行：没有配置 DEEPSEEK_API_KEY 时系统仍可跑通完整工作流。

它不是「假装成 AI」——它是明确标注的规则引擎（kind = "offline"），抽取能力覆盖正文中
显式表达的设定（属性句、日期句、状态句、伏笔标记句、人物动作句），
不具备语义推理能力；真正的理解仍来自 DeepSeek Flash。
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.ai.base import AIError, AIProvider, AIRequest, AIResponse
from app.ai.json_utils import extract_json
from app.services.text_rules import (
    _DATE_RE,
    detect_action_in_sentence,
    detect_status_change_in_sentence,
    extract_attribute_facts,
    extract_dated_mentions,
)
from app.timeutil import count_words, parse_deadline, split_sentences

_RELATION_WORDS = (
    "结盟", "反目", "决裂", "信任", "怀疑", "并肩", "为敌", "旧识", "交易", "师徒", "挚友",
    "血仇", "同门", "盟友",
)

_FORESHADOW_MARKERS = (
    "日后", "终有一日", "总有一天", "尚未", "还未", "未曾", "不曾", "没有言明", "没说出口",
    "另有隐情", "似乎知道", "迟早", "留待", "埋下", "将来", "没人知道", "不知道的是", "许多年后",
)

def target_words(context: dict[str, Any]) -> int:
    try:
        return int(context.get("target_words") or 1200)
    except (TypeError, ValueError):
        return 1200


class OfflineProvider(AIProvider):
    name = "offline"
    kind = "offline"
    _model = "novelos-rules-v1"

    @property
    def model(self) -> str:
        return self._model

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "kind": self.kind,
            "available": True,
            "supports_tools": self.supports_tools,
            "detail": "确定性规则引擎，用于离线测试与无密钥降级",
        }

    # ------------------------------------------------------------------ 分发
    def generate(self, request: AIRequest) -> AIResponse:
        handler = {
            "extract": self._task_extract,
            "continuity_narrative": self._task_continuity_narrative,
            "answer": self._task_answer,
            "write": self._task_write,
            "plan": self._task_plan,
            "style_review": self._task_style_review,
            "style_revise": self._task_style_revise,
            "realize": self._task_realize,
            "emotion_prompts": self._task_emotion_prompts,
        }.get(request.task)
        if handler is None:
            raise AIError(f"离线提供者不支持任务 {request.task}")
        parsed = handler(request)
        return AIResponse(
            text=json.dumps(parsed, ensure_ascii=False, indent=2),
            parsed=parsed,
            provider=self.name,
            model=self.model,
            usage={"prompt_chars": len(request.prompt), "output_chars": len(json.dumps(parsed))},
            warnings=["由离线规则提供者生成，抽取精度低于 DeepSeek Flash"],
        )

    # ------------------------------------------------------------------ 抽取
    def _task_extract(self, request: AIRequest) -> dict[str, Any]:
        ctx = request.context
        content: str = ctx.get("content", "") or ""
        chapter_number: int | None = ctx.get("chapter_number")
        chapter_time: str | None = ctx.get("story_time")
        chapter_location: str | None = ctx.get("location")
        known_characters: list[str] = list(ctx.get("known_characters") or [])
        known_locations: list[str] = list(ctx.get("known_locations") or [])
        known_events: list[str] = list(ctx.get("known_events") or [])

        sentences = split_sentences(content)
        paragraphs = [p.strip() for p in content.split("\n") if p.strip()]

        return {
            "characters_changed": self._extract_characters(
                sentences, known_characters, known_locations
            ),
            "events": self._extract_events(
                paragraphs, known_characters, known_locations, chapter_time, chapter_location
            ),
            "timeline": self._extract_timeline(paragraphs, content, known_events, known_locations),
            "locations": self._extract_locations(content, known_locations, known_characters),
            "new_facts": self._extract_facts(
                sentences, known_characters, known_locations, chapter_number
            ),
            "foreshadowing": self._extract_foreshadowing(sentences, known_characters),
            "relationships_changed": self._extract_relationships(
                sentences, known_characters, chapter_number
            ),
            "commitments": self._extract_commitments(
                sentences, known_characters, chapter_number, chapter_time
            ),
            "unknown": [],
            "notes": "离线规则抽取：仅覆盖正文中显式表达的设定。",
        }

    def _extract_characters(
        self, sentences: list[str], names: list[str], locations: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sentence in sentences:
            for name in names:
                if name not in sentence or name in seen:
                    continue
                action = self._detect_action(sentence, name)
                status_change = self._detect_status_change(sentence, name)
                if not action and not status_change:
                    continue
                location = next((loc for loc in locations if loc in sentence), None)
                results.append(
                    {
                        "name": name,
                        "action": action,
                        "status_change": status_change,
                        "location": location,
                        "notes": sentence[:80],
                    }
                )
                seen.add(name)
        return results

    def _detect_action(self, sentence: str, name: str) -> str | None:
        return detect_action_in_sentence(sentence, name)

    def _detect_status_change(self, sentence: str, name: str) -> str | None:
        return detect_status_change_in_sentence(sentence, name)

    def _extract_events(
        self,
        paragraphs: list[str],
        names: list[str],
        locations: list[str],
        chapter_time: str | None,
        chapter_location: str | None,
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for paragraph in paragraphs:
            involved = [name for name in names if name in paragraph]
            if len(involved) < 2:
                continue
            if not any(self._detect_action(paragraph, name) for name in involved):
                continue
            date_match = _DATE_RE.search(paragraph)
            location = next((loc for loc in locations if loc in paragraph), chapter_location or "")
            if not date_match and not location:
                continue
            events.append(
                {
                    "time": date_match.group(0) if date_match else chapter_time,
                    "location": location,
                    "characters": involved,
                    "description": paragraph[:120],
                    "consequences": None,
                }
            )
            if len(events) >= 4:
                break
        return events

    def _extract_timeline(
        self,
        paragraphs: list[str],
        content: str,
        known_events: list[str],
        locations: list[str],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for mention in extract_dated_mentions(content, known_events)[:4]:
            location = next((loc for loc in locations if loc in mention["paragraph"]), "")
            entries.append(
                {
                    "event": mention["event"] or self._prefix_event_name(mention),
                    "story_time": mention["story_time"],
                    "location": location or None,
                    "description": mention["paragraph"],
                }
            )
        return entries

    def _prefix_event_name(self, mention: dict[str, str]) -> str:
        """未匹配到既有事件名时，取日期前一小段作为事件名。"""
        paragraph, story_time = mention["paragraph"], mention["story_time"]
        index = paragraph.find(story_time)
        prefix = paragraph[max(0, index - 14) : index] if index > 0 else ""
        prefix = re.sub(r"[，。！？；：、“”「」\s]+$", "", prefix)
        return prefix or "未命名事件"

    def _extract_locations(
        self, content: str, locations: list[str], names: list[str]
    ) -> list[str]:
        return [loc for loc in locations if loc in content]

    def _extract_facts(
        self,
        sentences: list[str],
        names: list[str],
        locations: list[str],
        chapter_number: int | None,
    ) -> list[dict[str, Any]]:
        content = "\n".join(sentences)
        facts: list[dict[str, Any]] = []
        for item in extract_attribute_facts(content, names)[:8]:
            facts.append(
                {
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "object": item["object"],
                    "source_chapter": chapter_number,
                    "confidence": 0.6,
                    "visibility": "PUBLIC",
                    "known_by": [name for name in names if name in item["sentence"]],
                }
            )
        return facts

    def _extract_foreshadowing(
        self, sentences: list[str], names: list[str]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sentence in sentences:
            if not any(marker in sentence for marker in _FORESHADOW_MARKERS):
                continue
            head = re.split(r"[，。！？；：]", sentence)[0][:20]
            if not head or head in seen:
                continue
            seen.add(head)
            results.append(
                {
                    "name": head,
                    "description": sentence[:160],
                    "expected_payoff": None,
                    "related_characters": [n for n in names if n in sentence],
                }
            )
            if len(results) >= 5:
                break
        return results

    #: 承诺句里常见的动作词，避免把纯叙述的日期句当成承诺
    _COMMITMENT_VERBS = (
        "见", "来", "去", "等", "找", "送", "还", "回", "取", "杀", "动手", "上门",
        "禀", "告", "交", "给", "到", "赴", "动手", "领", "接",
    )

    def _extract_commitments(
        self,
        sentences: list[str],
        names: list[str],
        chapter_number: int | None,
        story_time: str | None,
    ) -> list[dict[str, Any]]:
        """规则抽取承诺：句子里有明确期限 + 动作词 + 至少一个人物。"""
        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        for sentence in sentences:
            days, raw = parse_deadline(sentence)
            if days is None or not raw:
                continue
            present = [name for name in names if name in sentence]
            if not present:
                continue
            if not any(verb in sentence for verb in self._COMMITMENT_VERBS):
                continue
            if sentence in seen:
                continue
            seen.add(sentence)
            kind = "DEADLINE"
            if any(word in sentence for word in ("杀", "取你", "性命", "饶不了", "等着")):
                kind = "THREAT"
            elif any(word in sentence for word in ("见", "来", "等", "赴")):
                kind = "APPOINTMENT"
            results.append(
                {
                    "kind": kind,
                    "who": present[0],
                    "counterpart": present[1] if len(present) > 1 else "",
                    "what": sentence.strip()[:200],
                    "quote": sentence.strip()[:200],
                    "deadline_text": raw,
                }
            )
            if len(results) >= 4:
                break
        return results

    def _extract_relationships(
        self, sentences: list[str], names: list[str], chapter_number: int | None
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for sentence in sentences:
            present = [n for n in names if n in sentence]
            if len(present) < 2:
                continue
            relation = next((word for word in _RELATION_WORDS if word in sentence), None)
            if not relation:
                continue
            pair = (present[0], present[1])
            if pair in seen:
                continue
            seen.add(pair)
            results.append(
                {
                    "character_a": present[0],
                    "character_b": present[1],
                    "relation": relation,
                    "change": sentence[:100],
                }
            )
        return results

    # ------------------------------------------------------------------ 审校叙事通道
    def _task_continuity_narrative(self, request: AIRequest) -> dict[str, Any]:
        """离线提供者的叙事通道。

        这里故意返回一条「没有证据」的候选问题，用来验证后端的证据门槛：
        无证据的问题会被丢弃并记入 dropped_issues，绝不会进入给作者看的报告。
        """
        return {
            "issues": [
                {
                    "level": "warning",
                    "code": "MODEL_UNEVIDENCED",
                    "message": "（离线规则提供者的占位提示）整体读感与前文氛围可能不完全一致。",
                }
            ]
        }

    # ------------------------------------------------------------------ 问答
    _FACT_TEMPLATES = {
        "佩剑": "{subject}的佩剑是{object}",
        "身份": "{subject}的身份是{object}",
        "修为": "{subject}的修为是{object}",
        "状态": "{subject}的状态是{object}",
        "师承": "{subject}的师承是{object}",
        "死因": "{subject}的死因是{object}",
        "父亲": "{subject}的父亲是{object}",
        "武器": "{subject}的武器是{object}",
    }

    def _task_answer(self, request: AIRequest) -> dict[str, Any]:
        ctx = request.context
        question: str = ctx.get("question", "")
        evidence: list[dict[str, Any]] = list(ctx.get("evidence") or [])
        uncovered: list[str] = list(ctx.get("uncovered_terms") or [])
        confidence: str = ctx.get("retrieval_confidence", "LOW")
        if not evidence:
            return {"answer": "UNKNOWN", "confidence": "UNKNOWN"}

        facts = [item for item in evidence if item.get("ref_type") == "CANON_FACT"]
        canon = [item for item in facts if item.get("fact_status") != "PROPOSED"]
        proposed = [item for item in facts if item.get("fact_status") == "PROPOSED"]
        structured = [
            item for item in evidence if item.get("ref_type") in ("TIMELINE", "EVENT", "CHARACTER")
        ]
        chapters = [item for item in evidence if item.get("ref_type") == "CHAPTER"]

        parts: list[str] = []
        if canon:
            described = []
            for item in canon[:3]:
                text = re.sub(r"［(CANON|PROPOSED|SUPERSEDED|REJECTED)］", "", item.get("excerpt", ""))
                parts_of = text.split()
                if len(parts_of) >= 3:
                    template = self._FACT_TEMPLATES.get(parts_of[1])
                    described.append(
                        (template.format(subject=parts_of[0], object=" ".join(parts_of[2:]))
                         if template
                         else text)
                        + f"（第{item.get('chapter_number')}章）"
                    )
                else:
                    described.append(f"{text}（第{item.get('chapter_number')}章）")
            parts.append("根据已确认的 Canon 事实：" + "；".join(described) + "。")
        if structured:
            described = [
                f"{item.get('title', '')}——{item.get('excerpt', '')[:60]}（第{item.get('chapter_number')}章）"
                for item in structured[:2]
            ]
            parts.append("前文记录：" + "；".join(described) + "。")
        if chapters and not canon and not structured:
            item = chapters[0]
            parts.append(
                f"前文正文（第{item.get('chapter_number')}章 {item.get('chapter_title') or ''}）："
                f"「{(item.get('excerpt') or '')[:70]}」"
            )
        if proposed:
            described = [
                re.sub(r"［(CANON|PROPOSED|SUPERSEDED|REJECTED)］", "", item.get("excerpt", ""))
                + f"（第{item.get('chapter_number')}章）"
                for item in proposed[:2]
            ]
            parts.append(
                "另外，以下信息尚未确认（PROPOSED），需作者确认后才会成为 Canon 设定："
                + "；".join(described)
                + "。"
            )
        if uncovered:
            parts.append(
                "问题中的「" + "」「".join(uncovered) + "」在已收录的前文里没有任何记载，只能算 UNKNOWN。"
            )
        if not parts:
            return {"answer": "UNKNOWN", "confidence": "UNKNOWN"}
        if confidence not in ("HIGH", "MEDIUM", "LOW"):
            confidence = "LOW"
        if proposed and confidence == "HIGH":
            confidence = "MEDIUM"
        return {"answer": "".join(parts), "confidence": confidence, "question": question}

    # ------------------------------------------------------------------ 写作
    _TEMPLATES = (
        "{loc}的风比往日更冷。{who}把衣领压紧了些，沿着石阶一步步往上走。",
        "{who}没有立刻回答。他先看了一眼远处，像是在确认什么，又像是在拖延。",
        "「{line}」{who2}的声音不高，却让人没法当作没听见。",
        "{who}想起那些没能说完的话，喉咙里像压着一块石头。有些账，迟早要算。",
        "夜色沉下来，{loc}只剩下几盏灯。{who}在灯下把手里的东西又检查了一遍。",
        "「你要走的路，没人能替你走。」{who2}说完这句，转身走开，没有再看他一眼。",
        "{who}闭上眼，把呼吸压到最慢。他需要在最短的时间里，把上一次的失误想清楚。",
        "远处传来一声闷响，像是有什么塌了。{who}和{who2}同时停住脚步。",
        "{who}终于明白当年那句没说完的话是什么意思，只是这个明白来得太晚。",
        "他们没有再多说。{loc}的雾散了一半，剩下的那一半还在等着他们。",
    )

    _FACT_SENTENCES = {
        "佩剑": "他腰间那件兵器是{object}，这一点从未变过。",
        "身份": "{subject}的身份是{object}，知道的人并不多。",
        "修为": "{subject}的修为停在{object}，这已是他此刻能到的地方。",
        "状态": "他知道，{subject}如今的状态是{object}。",
        "师承": "他的师承是{object}，这件事没有第二个人能替他改。",
        "父亲": "{subject}的父亲是{object}，这个名字他很少对人提起。",
        "死因": "他记得{subject}的死因：{object}。",
    }

    _MUST_INCLUDE_TEMPLATES = (
        "{who}在心里把这件事过了一遍：{item}。走到这一步，这一点不能丢。",
        "「{item}」——{who}把这个念头压住，先看眼前的事。",
        "他想起{item}，手里那把剑的分量忽然清楚了。",
        "{who}没有忘{item}。在这条路上忘一件，就等于把路走窄一寸。",
    )

    def _task_write(self, request: AIRequest) -> dict[str, Any]:
        ctx = request.context
        chapter_number = ctx.get("chapter_number") or 1
        title = ctx.get("title") or f"第{chapter_number}章"
        wrapped_title = title if title.startswith("#") else f"# 第{chapter_number}章 {title}"
        goals: str = ctx.get("goals", "")
        must_include: list[str] = list(ctx.get("must_include") or [])
        forbidden: list[str] = list(ctx.get("forbidden") or [])
        characters: list[dict[str, Any]] = list(ctx.get("characters") or [])
        canon_facts: list[dict[str, Any]] = list(ctx.get("canon_facts") or [])
        foreshadowing: list[dict[str, Any]] = list(ctx.get("foreshadowing") or [])
        timeline: list[dict[str, Any]] = list(ctx.get("timeline") or [])
        warnings: list[str] = []

        def _blocked(text: str) -> bool:
            return any(item and item in text for item in forbidden)

        blocked_facts = []
        safe_facts = []
        for fact in canon_facts:
            hit = next(
                (
                    item
                    for item in forbidden
                    if item and (item in str(fact.get("object", "")) or item in str(fact.get("subject", "")))
                ),
                None,
            )
            if hit:
                blocked_facts.append(hit)
                continue
            safe_facts.append(fact)
        for item in dict.fromkeys(blocked_facts):
            warnings.append(f"禁止内容「{item}」出现在 Canon 检索结果中，已从写作上下文中剔除")

        who = [c.get("name", "") for c in characters if c.get("name")] or ["林默"]
        who2 = who[1] if len(who) > 1 else "王烈"
        location = (
            ctx.get("location")
            or (characters[0].get("current_location") if characters else "")
            or (timeline[-1].get("location") if timeline else "")
            or ""
        )
        story_time = ctx.get("story_time") or ""
        scene = "，".join(part for part in (story_time, location) if part)

        paragraphs: list[str] = []
        opening = f"{scene}。" if scene else ""
        paragraphs.append(
            f"{opening}{who[0]}站定身形，没有马上开口。{goals.strip()}——他心里比谁都清楚这一点。"
        )
        if _blocked(paragraphs[-1]):
            paragraphs[-1] = f"{opening}{who[0]}站定身形，没有马上开口。"

        described = 0
        for fact in safe_facts:
            template = self._FACT_SENTENCES.get(str(fact.get("predicate", "")))
            if not template:
                continue
            sentence = template.format(
                subject=fact.get("subject", ""), object=fact.get("object", "")
            )
            if _blocked(sentence):
                continue
            paragraphs.append(f"{who[0]}想起这些时，语气比平时慢了一拍。" + sentence)
            described += 1
            if described >= 2:
                break

        for index, item in enumerate(must_include):
            if _blocked(item):
                warnings.append(f"必须出现的内容「{item}」命中禁止列表，已跳过")
                continue
            template = self._MUST_INCLUDE_TEMPLATES[index % len(self._MUST_INCLUDE_TEMPLATES)]
            paragraphs.append(template.format(who=who[0], item=item))

        for item in foreshadowing[:2]:
            description = str(item.get("description") or item.get("name") or "")
            if not description or _blocked(description):
                continue
            paragraphs.append(f"他还记得{description[:60]}。那时他不懂，现在也只是略懂。")

        index = 0
        guard = 0
        while count_words("\n".join(paragraphs)) < target_words(ctx) and guard < 200:
            guard += 1
            template = self._TEMPLATES[index % len(self._TEMPLATES)]
            index += 1
            paragraph = template.format(
                loc=location or "山道上", who=who[0], who2=who2, line="这一次，不能再退"
            )
            if _blocked(paragraph):
                continue
            paragraphs.append(paragraph)

        content = "\n\n".join(paragraphs)
        word_count = count_words(content)
        target = target_words(ctx)
        if abs(word_count - target) / max(target, 1) > 0.3:
            warnings.append(f"离线模板生成字数（{word_count}）与目标（{target}）差距较大")
        return {
            "title": wrapped_title,
            "content": f"{wrapped_title}\n\n{content}",
            "word_count": word_count,
            "warnings": list(dict.fromkeys(warnings)),
        }

    # ------------------------------------------------------------------ 文风
    def _task_style_review(self, request: AIRequest) -> dict[str, Any]:
        """离线规则提供者不做读感评审：它没有语义判断力，装作能评审只会误导作者。

        规则层指标由 style_service 单独计算，这里如实返回「没有模型意见」。
        """
        return {
            "issues": [],
            "summary": "离线规则提供者不参与读感评审（只有规则层指标可用）",
        }

    #: 安全的口语化精简：只删除/替换固定搭配，不改变句意
    FILLER_REPLACEMENTS: tuple[tuple[str, str], ...] = (
        ("深吸了一口气", "吸了口气"),
        ("深吸一口气", "吸了口气"),
        ("不由得", ""),
        ("缓缓地", ""),
        ("缓缓", ""),
        ("淡淡地", ""),
        ("淡淡", ""),
        ("深深地", ""),
        ("深深", ""),
        ("冷冷地", ""),
        ("冷冷", ""),
        ("微微地", ""),
        ("心中一惊", "心里一紧"),
        ("心头一震", "心里一震"),
        ("如释重负", "松了口气"),
        ("若有所思", "没作声"),
        ("眉头微皱", "皱眉"),
        ("眉头一皱", "皱眉"),
        ("微微颔首", "点头"),
        ("沉默了片刻", "沉默了一息"),
        ("沉默片刻", "沉默了一息"),
        ("不易察觉", "几乎看不出来"),
        ("眼中闪过一丝", "眼里掠过"),
        ("瞳孔微缩", "瞳孔一缩"),
    )

    def _task_style_revise(self, request: AIRequest) -> dict[str, Any]:
        """确定性改稿：删掉填充式副词、把超长句断开。

        它不会新增情节，也不会补章末钩子——做不到的事如实报告为未能处理。
        """
        ctx = request.context
        content: str = ctx.get("content", "") or ""
        issues: list[dict[str, Any]] = list(ctx.get("issues") or [])
        revised = content
        applied: list[str] = []

        if any(issue.get("metric") in ("cliche_per_1k", "cliche_variety") for issue in issues):
            for source, target in self.FILLER_REPLACEMENTS:
                if source in revised:
                    revised = revised.replace(source, target)
                    applied.append(f"精简「{source}」")

        if any(issue.get("metric") == "long_sentence_ratio" for issue in issues):
            revised, split_count = self._split_long_sentences(revised)
            if split_count:
                applied.append(f"断开 {split_count} 处长句")

        unresolved = [
            issue.get("code", "")
            for issue in issues
            if issue.get("metric") in ("hook_score", "burstiness", "dialogue_ratio", "model", "conflict_per_1k")
        ]
        warnings = ["离线改稿只做「去填充词 + 断长句」，其余问题需要模型或人工处理"]
        if unresolved:
            warnings.append("未处理的改进项：" + "、".join(code for code in unresolved if code))
        return {
            "content": revised.strip(),
            "warnings": warnings,
            "applied": applied,
            "unresolved": unresolved,
        }

    @staticmethod
    def _split_long_sentences(text: str, threshold: int = 38) -> tuple[str, int]:
        """把超长句断开：优先在句子中段（25%~75%）的逗号处断，没有就退而求其次取最接近中点的标点。

        阈值取 38 而不是 42：文风指标把「40 字以上」记为长句，改稿要能覆盖到指标标记的每一类句子。
        """
        from app.timeutil import split_sentences

        sentences = [s for s in split_sentences(text) if s.strip()]
        if not sentences:
            return text, 0
        parts: list[str] = []
        splits = 0
        for sentence in sentences:
            if len(sentence) <= threshold:
                parts.append(sentence)
                continue
            breaks = [index for index, char in enumerate(sentence) if char in "，、；："]
            if not breaks:
                parts.append(sentence)
                continue
            low, high = int(len(sentence) * 0.25), int(len(sentence) * 0.75)
            preferred = [index for index in breaks if low <= index <= high]
            cut = max(preferred) if preferred else min(breaks, key=lambda index: abs(index - len(sentence) // 2))
            head = sentence[:cut].rstrip("，、；：")
            tail = sentence[cut + 1 :].lstrip("，、；：")
            if not head or not tail:
                parts.append(sentence)
                continue
            parts.append(head + "。")
            parts.append(tail)
            splits += 1
        return "".join(parts), splits

    # ------------------------------------------------------------------ 碎片成文
    def _task_realize(self, request: AIRequest) -> dict[str, Any]:
        """离线规则的「成文」：把作者原话原样保留，只补最少的连接与场景落点。

        它不会做文学化改写 —— 那正是需要模型的地方。这里如实标注 treatment=QUOTED，
        让作者一眼看出「这一段是原话，没有被加工」。
        """
        ctx = request.context
        fragments: list[dict[str, Any]] = list(ctx.get("fragments") or [])
        canon_facts: list[dict[str, Any]] = list(ctx.get("canon_facts") or [])
        passages: list[dict[str, Any]] = []
        for index, fragment in enumerate(fragments):
            text = (fragment.get("text") or "").strip()
            if not text:
                continue
            prose = text if text.endswith(("。", "！", "？", "…", "”")) else text + "。"
            passages.append(
                {
                    "fragment_id": fragment.get("id") or f"inline-{index}",
                    "prose": prose,
                    "uses_quote": text[:120],
                    "treatment": "QUOTED",
                }
            )
        if canon_facts and passages:
            anchor = canon_facts[0]
            passages.insert(
                0,
                {
                    "fragment_id": "__bridge__",
                    "prose": (
                        f"{anchor.get('subject')}的{anchor.get('predicate')}是{anchor.get('object')}"
                        "——这一点没有变。"
                    ),
                    "uses_quote": "",
                    "treatment": "BACKGROUND",
                },
            )
        return {
            "title": "",
            "passages": passages,
            "undeveloped": [],
            "notes": "离线规则提供者不做文学化改写：碎片原话被原样保留，仅补了最少的连接。",
        }

    def _task_emotion_prompts(self, request: AIRequest) -> dict[str, Any]:
        """离线规则给出的是通用但具体的提问模板（不假装懂这个故事）。"""
        ctx = request.context
        characters: list[str] = list(ctx.get("characters") or [])
        who = characters[0] if characters else "他"
        goals = str(ctx.get("goals") or "这一幕")
        return {
            "questions": [
                f"关于「{goals[:20]}」：{who}此刻最想说的是哪一句？为什么没说出口？",
                f"{who}的手、肩膀或呼吸在这一刻有什么变化？是由什么动作引起的？",
                f"如果这一幕失败，{who}失去的具体是什么（一个人、一件东西、还是某个身份）？",
                "这一幕里有什么具体的声音、气味或物件，是你亲眼见过或闻过的？",
                "写完之后，你希望读者记住哪一个画面？",
            ],
            "notes": "离线规则提供的通用提问模板，用于把作者的真实细节问出来。",
        }

    # ------------------------------------------------------------------ 规划
    def _task_plan(self, request: AIRequest) -> dict[str, Any]:
        """离线规则规划：按伏笔欠账从久到近安排，每章一条主伏笔 + 主角视角。

        不发明新设定；已死亡或无法行动的人物写进 forbidden，交给写手回避。
        """
        ctx = request.context
        start = int(ctx.get("from_chapter") or 1)
        count = max(1, min(int(ctx.get("count") or 3), 10))
        steer = str(ctx.get("steer") or "").strip()
        debt = list(ctx.get("foreshadowing_debt") or [])
        characters = list(ctx.get("characters") or [])
        names = [item.get("name", "") for item in characters if item.get("name")]
        protagonist = names[0] if names else "林默"
        unusable = [
            item["name"]
            for item in characters
            if item.get("current_status")
            and any(token in str(item["current_status"]) for token in ("死亡", "重伤昏迷", "失踪"))
        ]

        chapters: list[dict[str, Any]] = []
        for offset in range(count):
            number = start + offset
            target = debt[offset % len(debt)] if debt else None
            related = [name for name in (target or {}).get("related_characters", []) if name in names]
            cast = related or [protagonist]
            if protagonist not in cast:
                cast.insert(0, protagonist)
            goals = (
                f"推进伏笔「{target['name']}」：{str(target.get('description', ''))[:40]}"
                if target
                else "沿主线推进：主角继续追查当前最主要的威胁"
            )
            if steer:
                goals += f"；作者要求：{steer}"
            must_include = [protagonist]
            if target:
                must_include.append(target["name"])
            chapters.append(
                {
                    "chapter_number": number,
                    "title": str(target["name"]) if target else f"第{number}章",
                    "goals": goals,
                    "must_include": must_include,
                    "forbidden": unusable,
                    "characters": cast,
                    "advance_foreshadowing": [target["name"]] if target else [],
                    "rationale": (
                        f"伏笔「{target['name']}」已 {target.get('age', 0)} 章未回应，优先安排；"
                        if target
                        else "当前没有明显欠账，按主线推进；"
                    )
                    + "离线规则规划，未经模型润色。",
                }
            )
        return {"chapters": chapters}

    # ------------------------------------------------------------------ 工具调用
    supports_tools = True

    def generate_with_tools(self, request: AIRequest, tools: list[dict[str, Any]]) -> AIResponse:
        """离线规则的工具调用：先按问题里的实体查工具，再把工具结果汇总成带出处的回答。"""
        messages = request.messages or []
        tool_messages = [message for message in messages if message.get("role") == "tool"]
        if not tool_messages:
            calls = self._planned_tool_calls(request, tools)
            if calls:
                return AIResponse(
                    text="",
                    provider=self.name,
                    model=self.model,
                    tool_calls=calls,
                    warnings=["离线规则提供者：按问题中的实体发起工具查询"],
                )
        answer, citations = self._summarize_tool_results(tool_messages)
        return AIResponse(
            text=json.dumps(
                {"answer": answer, "confidence": "HIGH" if citations else "LOW"}, ensure_ascii=False
            ),
            parsed={"answer": answer, "confidence": "HIGH" if citations else "LOW"},
            provider=self.name,
            model=self.model,
            warnings=["离线规则提供者：工具结果汇总（未经过语言模型润色）"],
        )

    @staticmethod
    def _planned_tool_calls(request: AIRequest, tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        available = {item["function"]["name"] for item in tools}
        entities = [str(item) for item in (request.context.get("entities") or [])][:2]
        question = str(request.context.get("question") or "")
        calls: list[dict[str, Any]] = []
        for entity in entities:
            if "search_chapters" in available:
                calls.append(
                    {
                        "id": f"call_search_{len(calls)}",
                        "name": "search_chapters",
                        "arguments": {"query": entity, "limit": 3},
                    }
                )
            if "get_canon_facts" in available:
                calls.append(
                    {
                        "id": f"call_facts_{len(calls)}",
                        "name": "get_canon_facts",
                        "arguments": {"subject": entity, "limit": 10},
                    }
                )
        if not calls and "search_chapters" in available and question:
            calls.append(
                {
                    "id": "call_search_0",
                    "name": "search_chapters",
                    "arguments": {"query": question[:20], "limit": 3},
                }
            )
        return calls[:4]

    @staticmethod
    def _summarize_tool_results(
        tool_messages: list[dict[str, Any]],
    ) -> tuple[str, list[int]]:
        chapters: list[int] = []
        details: list[str] = []
        for message in tool_messages:
            payload = message.get("content")
            if isinstance(payload, str):
                payload = extract_json(payload) or {}
            if not isinstance(payload, dict):
                continue
            data = payload.get("data")
            tool = payload.get("tool", "")
            if tool == "search_chapters" and isinstance(data, dict):
                for hit in data.get("hits", [])[:2]:
                    chapter = hit.get("chapter_number")
                    if chapter:
                        chapters.append(int(chapter))
                    details.append(
                        f"第{chapter}章《{hit.get('title', '')}》原文：{(hit.get('snippet') or '')[:50]}"
                    )
            elif tool == "get_canon_facts" and isinstance(data, list):
                for fact in data[:3]:
                    if fact.get("source_chapter"):
                        chapters.append(int(fact["source_chapter"]))
                    details.append(
                        f"{fact.get('subject')}的{fact.get('predicate')}是{fact.get('object')}"
                        f"（第{fact.get('source_chapter')}章）"
                    )
        if not details:
            return "UNKNOWN（工具没有返回可用证据）", []
        return "工具检索结果：" + "；".join(details), sorted(set(chapters))
