"""ContinuityChecker：章节一致性检查。

两层结构：
1. 硬规则（确定性、不依赖模型）：人物状态冲突、事实冲突、时间线倒置、未知人物、
   世界观规则违反、伏笔长期未回应。每条问题都由代码从数据库里取出证据。
2. 叙事通道（可选调用模型）：语义层面的可疑之处，但输出必须带证据，
   无证据的候选会被丢弃并记入 dropped_issues —— 对应安全原则「禁止模型无证据地声称存在矛盾」。
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest
from app.ai.json_utils import parse_issues_payload
from app.models import CanonStatus, Chapter, Character, Novel
from app.schemas import ContinuityIssue, ContinuityReportModel, Evidence, ExtractionResult
from app.services import query_service, text_rules
from app.timeutil import format_chapter_ref, split_sentences, story_time_sort_key

#: 状态类 Canon 事实中代表「无法行动」的取值
_INCAPACITY_STATES = ("重伤昏迷", "昏迷", "失踪", "闭关", "被囚", "封印")
_STALE_FORESHADOW_GAP = 10
_ORIGIN_LABELS = {
    "EXTRACTION": "模型抽取",
    "HARD_RULE": "确定性属性规则",
    "EXTRACTOR": "抽取器 PROPOSED 事实",
    "AI_TOOL": "AI 工具提出的 PROPOSED 事实",
}


def _fact_line(fact: dict[str, Any]) -> str:
    source = format_chapter_ref(fact.get("source_chapter"))
    return f"{fact['subject']} {fact['predicate']} {fact['object']}（来源{source}）"


class ContinuityChecker:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 入口
    def run(
        self,
        session: Session,
        novel: Novel,
        chapter: Chapter,
        *,
        extraction: ExtractionResult | None = None,
        proposed_facts: list[dict[str, Any]] | None = None,
        narrative_pass: bool = True,
    ) -> ContinuityReportModel:
        # 按被检章节的时点取 Canon 与时间线：第 6 章就用第 6 章时成立的设定比对，
        # 避免后面的设定变更把早期章节判成矛盾。
        canon_facts = query_service.canon_as_of(session, novel.id, chapter.chapter_number)
        character_models = list(
            session.scalars(select(Character).where(Character.novel_id == novel.id))
        )
        characters = [query_service.character_to_dict(character) for character in character_models]
        timeline = query_service.list_timeline(
            session,
            novel.id,
            status=CanonStatus.CANON,
            limit=300,
            as_of_chapter=chapter.chapter_number,
        )
        events = query_service.list_events(
            session, novel.id, limit=300, as_of_chapter=chapter.chapter_number
        )
        world_rules = query_service.list_world_rules(session, novel.id)
        foreshadowing = query_service.list_foreshadowing(session, novel.id)
        frontier = session.scalar(
            select(func.max(Chapter.chapter_number)).where(Chapter.novel_id == novel.id)
        )

        if proposed_facts is None:
            proposed_facts = query_service.list_canon_facts(
                session, novel.id, status=CanonStatus.PROPOSED, limit=500
            )
        proposed_facts = [fact for fact in proposed_facts if fact.get("source_chapter") == chapter.chapter_number]

        issues: list[ContinuityIssue] = []
        issues.extend(
            self._check_character_states(chapter, character_models, canon_facts, extraction)
        )
        issues.extend(
            self._check_fact_conflicts(chapter, canon_facts, extraction, proposed_facts, characters)
        )
        issues.extend(self._check_timeline(chapter, timeline, extraction, events))
        issues.extend(self._check_unknown_characters(chapter, characters, extraction))
        issues.extend(self._check_world_rules(chapter, world_rules, canon_facts))
        issues.extend(self._check_foreshadowing(chapter, foreshadowing, frontier))

        dropped: list[dict[str, Any]] = []
        provider_name = self.provider.name
        model = self.provider.model
        if narrative_pass:
            narrative_issues, narrative_dropped, provider_name, model = self._narrative_pass(
                chapter, canon_facts, characters, timeline, events, world_rules, proposed_facts
            )
            issues.extend(narrative_issues)
            dropped.extend(narrative_dropped)

        issues = self._dedupe(issues)
        errors = [issue for issue in issues if issue.level == "error"]
        warnings = [issue for issue in issues if issue.level == "warning"]
        return ContinuityReportModel(
            chapter_id=chapter.id,
            chapter_number=chapter.chapter_number,
            errors=errors,
            warnings=warnings,
            dropped_issues=dropped,
            provider=provider_name,
            model=model,
        )

    # ------------------------------------------------------------------ 硬规则
    def _check_character_states(
        self,
        chapter: Chapter,
        characters: list[Character],
        canon_facts: list[dict[str, Any]],
        extraction: ExtractionResult | None,
    ) -> list[ContinuityIssue]:
        """已死亡 / 无法行动的人物在本章出现动作 → 矛盾。

        判定用的是「该人物目前为止最新的状态记录」：Canon 状态事实与人物状态历史里
        章号最大的一条。这样在按顺序处理章节、逐章确认状态变更之后，后续章节不会再被误报。
        """
        issues: list[ContinuityIssue] = []
        extraction_status_changes = {
            change.name: change.status_change
            for change in (extraction.characters_changed if extraction else [])
            if change.status_change
        }
        for character in characters:
            state = self._effective_state(character, canon_facts)
            if state is None:
                continue
            source_chapter, value, ref_type, ref_id = state
            is_death = text_rules.is_death_status(value)
            is_incapable = any(token in value for token in _INCAPACITY_STATES)
            if not (is_death or is_incapable):
                continue
            name = character.name
            if source_chapter and chapter.chapter_number <= source_chapter:
                continue  # 该状态发生在本章之后，不构成矛盾
            hit = text_rules.detect_action_in_text(chapter.content or "", name)
            if hit is None:
                continue
            sentence, verb = hit
            status_change = extraction_status_changes.get(name) or self._chapter_status_change(
                chapter.content or "", name
            )
            evidence = [
                Evidence(
                    source_chapter=format_chapter_ref(source_chapter or None),
                    ref_type="CANON_FACT" if ref_type == "CANON_FACT" else "CHARACTER",
                    ref_id=ref_id,
                    detail=(
                        f"Canon 设定：{name}的状态是「{value}」"
                        if ref_type == "CANON_FACT"
                        else f"人物状态历史：{name}在{format_chapter_ref(source_chapter or None)}的状态是「{value}」"
                    ),
                ),
                Evidence(
                    source_chapter=format_chapter_ref(chapter.chapter_number),
                    ref_type="CHAPTER",
                    ref_id=chapter.id,
                    quote=sentence,
                    detail=f"本章中{name}出现动作「{verb}」",
                ),
            ]
            if status_change and not is_death:
                # 重伤/失踪这类状态，正文明确写出苏醒、痊愈等变化时，只提示作者确认；
                # 已死亡的人物则一律按矛盾处理 —— 死者不可复生（世界规则 WR3），
                # 任何「又出现动作」的写法都必须由作者显式改 Canon 才生效。
                issues.append(
                    ContinuityIssue(
                        level="warning",
                        code="STATE_CHANGE_UNCONFIRMED",
                        message=(
                            f"{name}在 Canon 中的状态是「{value}」（{format_chapter_ref(source_chapter or None)}），"
                            f"本章出现状态变化「{status_change}」，需要作者确认后才更新人物状态。"
                        ),
                        evidence=evidence,
                        suggested_fix=f"确认后将{name}的状态更新为「{status_change}」，并在人物状态历史中留痕。",
                    )
                )
                continue
            code = "DEAD_CHARACTER_ACTIVE" if is_death else "INCAPACITATED_CHARACTER_ACTIVE"
            issues.append(
                ContinuityIssue(
                    level="error",
                    code=code,
                    message=(
                        f"{name}在{format_chapter_ref(source_chapter or None)}已「{value}」，"
                        f"但本章仍出现动作「{verb}」。"
                    ),
                    evidence=evidence,
                    suggested_fix=(
                        f"改写该段：或把{name}换成其他人物，或明确交代其状态变化的来由"
                        "（若确为设定变更，需作者确认后更新 Canon）。"
                    ),
                )
            )
        return issues

    @staticmethod
    def _effective_state(
        character: Character, canon_facts: list[dict[str, Any]]
    ) -> tuple[int, str, str, str | None] | None:
        """返回 (章号, 状态值, 证据类型, 证据 id)：取章号最大的一条状态记录。"""
        best: tuple[int, str, str, str | None] | None = None
        for fact in canon_facts:
            if fact.get("predicate") != "状态" or fact.get("subject") != character.name:
                continue
            candidate = (
                int(fact.get("source_chapter") or 0),
                str(fact.get("object") or ""),
                "CANON_FACT",
                fact.get("id"),
            )
            if best is None or candidate[0] >= best[0]:
                best = candidate
        for state in character.states:
            if not state.status:
                continue
            candidate = (
                int(state.chapter_number or 0),
                state.status,
                "CHARACTER_STATE",
                state.id,
            )
            if best is None or candidate[0] >= best[0]:
                best = candidate
        if best is None or not best[1]:
            return None
        return best

    @staticmethod
    def _chapter_status_change(content: str, name: str) -> str | None:
        for sentence in split_sentences(content):
            if name not in sentence:
                continue
            change = text_rules.detect_status_change_in_sentence(sentence, name)
            if change:
                return change
        return None

    def _check_fact_conflicts(
        self,
        chapter: Chapter,
        canon_facts: list[dict[str, Any]],
        extraction: ExtractionResult | None,
        proposed_facts: list[dict[str, Any]],
        characters: list[dict[str, Any]],
    ) -> list[ContinuityIssue]:
        """本章抽取出的事实与既有 Canon 同主谓但宾不同 → 矛盾；完全相同 → 重复提示。

        事实来源有三处，缺一不可：模型抽取结果、库中本章的 PROPOSED 事实、
        以及确定性属性规则直接从正文里读出的设定句（模型漏抽时由它兜底）。
        """
        issues: list[ContinuityIssue] = []
        incoming: list[dict[str, Any]] = []
        if extraction:
            for fact in extraction.new_facts:
                incoming.append(
                    {
                        "subject": fact.subject,
                        "predicate": fact.predicate,
                        "object": fact.object,
                        "confidence": fact.confidence,
                        "origin": "EXTRACTION",
                    }
                )
        for fact in proposed_facts:
            incoming.append(
                {
                    "subject": fact["subject"],
                    "predicate": fact["predicate"],
                    "object": fact["object"],
                    "confidence": fact.get("confidence", 0.5),
                    "origin": fact.get("origin", "AI"),
                    "id": fact.get("id"),
                }
            )
        names = [character["name"] for character in characters]
        for item in text_rules.extract_attribute_facts(chapter.content or "", names):
            incoming.append(
                {
                    "subject": item["subject"],
                    "predicate": item["predicate"],
                    "object": item["object"],
                    "confidence": 0.6,
                    "origin": "HARD_RULE",
                    "quote": item["sentence"],
                }
            )

        seen: set[tuple[str, str, str]] = set()
        for item in incoming:
            key = (item["subject"], item["predicate"], item["object"])
            if key in seen:
                continue
            seen.add(key)
            for canon in canon_facts:
                if canon["subject"] != item["subject"] or canon["predicate"] != item["predicate"]:
                    continue
                source_chapter = canon.get("source_chapter") or 0
                if source_chapter == chapter.chapter_number:
                    continue
                quote = item.get("quote") or text_rules.sentence_with(
                    chapter.content or "", item["object"]
                )
                origin_label = _ORIGIN_LABELS.get(item["origin"], "PROPOSED 事实")
                evidence = [
                    Evidence(
                        source_chapter=format_chapter_ref(source_chapter or None),
                        ref_type="CANON_FACT",
                        ref_id=canon.get("id"),
                        detail=f"Canon 设定：{canon['subject']}的{canon['predicate']}是「{canon['object']}」",
                    ),
                    Evidence(
                        source_chapter=format_chapter_ref(chapter.chapter_number),
                        ref_type="CHAPTER",
                        ref_id=chapter.id,
                        quote=quote,
                        detail=(
                            f"本章检出：{item['subject']}的{item['predicate']}是「{item['object']}」"
                            f"（待确认，来源：{origin_label}）"
                        ),
                    ),
                ]
                if str(canon["object"]) == str(item["object"]):
                    issues.append(
                        ContinuityIssue(
                            level="warning",
                            code="DUPLICATE_FACT",
                            message=(
                                f"本章提出的「{item['subject']} {item['predicate']} {item['object']}」"
                                f"与既有 Canon（{format_chapter_ref(source_chapter or None)}）相同，无需重复确认。"
                            ),
                            evidence=evidence,
                            suggested_fix="在审校面板中驳回该条 PROPOSED 事实即可。",
                        )
                    )
                    continue
                issues.append(
                    ContinuityIssue(
                        level="error",
                        code="FACT_CONFLICT",
                        message=(
                            f"事实冲突：Canon 记录「{canon['subject']}的{canon['predicate']}是{canon['object']}」"
                            f"（{format_chapter_ref(source_chapter or None)}），"
                            f"本章却出现「{item['object']}」。"
                        ),
                        evidence=evidence,
                        suggested_fix=(
                            f"核对正文：若本意仍是「{canon['object']}」，请修正本章表述；"
                            f"若确为设定变更，需作者确认后把该事实升级为 CANON 并把旧事实标记为 SUPERSEDED。"
                        ),
                    )
                )
        return issues

    def _check_timeline(
        self,
        chapter: Chapter,
        timeline: list[dict[str, Any]],
        extraction: ExtractionResult | None,
        events: list[dict[str, Any]],
    ) -> list[ContinuityIssue]:
        """本章的时间表述与既有时间线对不上 → 矛盾。

        条目来源同样有两处：模型抽取的时间线，以及确定性日期规则从正文里读出的日期句
        （模型漏抽时由它兜底，且只在能可靠归属到既有事件名时才比对）。
        """
        canon_event_names = [entry["event"] for entry in timeline if entry.get("event")]
        canon_event_names += [
            (event["description"] or "").split("，")[0] for event in events if event.get("description")
        ]
        canon_event_names = list(dict.fromkeys(name for name in canon_event_names if name))

        entries: list[dict[str, str]] = []
        if extraction:
            for entry in extraction.timeline:
                entries.append(
                    {
                        "event": entry.event,
                        "story_time": entry.story_time,
                        "origin": "EXTRACTION",
                    }
                )
        for mention in text_rules.extract_dated_mentions(chapter.content or "", canon_event_names):
            if not mention["event"]:
                continue
            entries.append(
                {
                    "event": mention["event"],
                    "story_time": mention["story_time"],
                    "origin": "HARD_RULE",
                }
            )

        issues: list[ContinuityIssue] = []
        seen: set[tuple[str, str]] = set()
        for entry in entries:
            key = (entry["event"], entry["story_time"])
            if key in seen:
                continue
            seen.add(key)
            for canon in timeline:
                if not canon.get("event"):
                    continue
                if not self._same_event(entry["event"], canon["event"]):
                    continue
                delta = story_time_sort_key(entry["story_time"]) - int(canon.get("story_time_sort") or 0)
                quote = text_rules.sentence_with(chapter.content or "", entry["story_time"])
                evidence = [
                    Evidence(
                        source_chapter=format_chapter_ref(canon.get("chapter_number")),
                        ref_type="TIMELINE",
                        ref_id=canon.get("id"),
                        detail=f"既有时间线：{canon['event']} 发生在「{canon['story_time']}」",
                    ),
                    Evidence(
                        source_chapter=format_chapter_ref(chapter.chapter_number),
                        ref_type="CHAPTER",
                        ref_id=chapter.id,
                        quote=quote,
                        detail=f"本章时间线条目：{entry['event']} 发生在「{entry['story_time']}」",
                    ),
                ]
                if delta < 0:
                    issues.append(
                        ContinuityIssue(
                            level="error",
                            code="TIMELINE_INVERSION",
                            message=(
                                f"时间线倒置：「{canon['event']}」在 Canon 中发生于"
                                f"{canon['story_time']}（{format_chapter_ref(canon.get('chapter_number'))}），"
                                f"本章却把它说成{entry['story_time']}。"
                            ),
                            evidence=evidence,
                            suggested_fix="统一两处的时间表述；若本章为旧事重述，请改成与 Canon 一致的时间。",
                        )
                    )
                elif delta > 0:
                    issues.append(
                        ContinuityIssue(
                            level="warning",
                            code="TIMELINE_DRIFT",
                            message=(
                                f"时间漂移：同一事件「{canon['event']}」的记述时间由"
                                f"{canon['story_time']}变为{entry['story_time']}。"
                            ),
                            evidence=evidence,
                            suggested_fix="确认是否有意推进时间；无意请改回 Canon 时间。",
                        )
                    )
                break
        return issues

    @staticmethod
    def _same_event(left: str, right: str) -> bool:
        if not left or not right:
            return False
        if left == right:
            return True
        return text_rules.similarity(left, right) >= 0.8

    def _check_unknown_characters(
        self,
        chapter: Chapter,
        characters: list[dict[str, Any]],
        extraction: ExtractionResult | None,
    ) -> list[ContinuityIssue]:
        if not extraction:
            return []
        known = {character["name"] for character in characters}
        issues: list[ContinuityIssue] = []
        for change in extraction.characters_changed:
            if change.name in known:
                continue
            if len(change.name) > 6 or "（" in change.name or "(" in change.name:
                # 「血河教刺客（未具名，三人）」这类描述不是人名，不当作未建档人物提示
                continue
            issues.append(
                ContinuityIssue(
                    level="warning",
                    code="UNKNOWN_CHARACTER",
                    message=f"本章出现尚未建档的人物「{change.name}」，其状态无法核对。",
                    evidence=[
                        Evidence(
                            source_chapter=format_chapter_ref(chapter.chapter_number),
                            ref_type="CHAPTER",
                            ref_id=chapter.id,
                            quote=change.notes,
                            detail="抽取结果中的人物变更",
                        )
                    ],
                    suggested_fix="在「人物」面板中为该人物建档，补全首次出场章节与当前状态。",
                )
            )
        return issues

    def _check_world_rules(
        self,
        chapter: Chapter,
        world_rules: list[dict[str, Any]],
        canon_facts: list[dict[str, Any]],
    ) -> list[ContinuityIssue]:
        content = chapter.content or ""
        issues: list[ContinuityIssue] = []
        realm_by_name = {
            fact["subject"]: str(fact["object"])
            for fact in canon_facts
            if fact.get("predicate") in ("修为", "境界")
        }
        for rule in world_rules:
            subject = rule.get("subject") or ""
            rule_type = rule.get("rule_type") or ""
            if not subject or subject not in content:
                continue
            evidence_rule = Evidence(
                source_chapter=format_chapter_ref(rule.get("source_chapter") or chapter.chapter_number),
                ref_type="WORLD_RULE",
                ref_id=rule.get("id"),
                detail=f"世界规则：{rule.get('name')} —— {rule.get('description')}",
            )
            if rule_type == "periodic_event":
                for sentence in text_rules.split_sentences(content):
                    if subject not in sentence:
                        continue
                    if not any(token in sentence for token in ("开启", "一开", "开过", "开了", "开启过")):
                        continue
                    dates = text_rules.find_dates(sentence)
                    if not dates:
                        continue
                    next_open = (rule.get("params") or {}).get("next_open")
                    for date in dates:
                        if next_open and date == next_open:
                            continue
                        issues.append(
                            ContinuityIssue(
                                level="warning",
                                code="WORLD_RULE_PERIOD",
                                message=(
                                    f"「{subject}」的开启时间被描述为{date}，"
                                    f"与该规则{rule.get('description')}不一致。"
                                ),
                                evidence=[
                                    evidence_rule,
                                    Evidence(
                                        source_chapter=format_chapter_ref(chapter.chapter_number),
                                        ref_type="CHAPTER",
                                        ref_id=chapter.id,
                                        quote=sentence,
                                        detail="本章的周期描述",
                                    ),
                                ],
                                suggested_fix=f"改为与规则一致的表述（{next_open or '按周期推算'}）。",
                            )
                        )
                        break
            elif rule_type == "immutable_trait":
                for sentence in text_rules.split_sentences(content):
                    if subject not in sentence:
                        continue
                    if not any(token in sentence for token in ("改变", "重塑", "换了", "逆转", "移植", "后天")):
                        continue
                    issues.append(
                        ContinuityIssue(
                            level="warning",
                            code="WORLD_RULE_TRAIT",
                            message=f"本章涉及「{subject}」的可变表述，与该规则「{rule.get('description')}」可能冲突。",
                            evidence=[
                                evidence_rule,
                                Evidence(
                                    source_chapter=format_chapter_ref(chapter.chapter_number),
                                    ref_type="CHAPTER",
                                    ref_id=chapter.id,
                                    quote=sentence,
                                    detail="本章的相关表述",
                                ),
                            ],
                            suggested_fix="确认是否违反世界观规则；违反则需要改写。",
                        )
                    )
                    break
            elif rule_type == "capability_gate":
                required = str((rule.get("params") or {}).get("requires") or "")
                for sentence in text_rules.split_sentences(content):
                    if subject not in sentence:
                        continue
                    for name, realm in realm_by_name.items():
                        if name not in sentence:
                            continue
                        if required and required in realm:
                            continue
                        if any(token in realm for token in ("金丹", "元婴", "化神")):
                            continue
                        issues.append(
                            ContinuityIssue(
                                level="warning",
                                code="WORLD_RULE_GATE",
                                message=(
                                    f"{name}的修为是「{realm}」，本章却与「{subject}」相关联，"
                                    f"违反规则「{rule.get('description')}」。"
                                ),
                                evidence=[
                                    evidence_rule,
                                    Evidence(
                                        source_chapter=format_chapter_ref(chapter.chapter_number),
                                        ref_type="CHAPTER",
                                        ref_id=chapter.id,
                                        quote=sentence,
                                        detail="本章的相关表述",
                                    ),
                                ],
                                suggested_fix=f"改为修为达到{required}之后再使用，或调整该段设定。",
                            )
                        )
                        break
        return issues

    def _check_foreshadowing(
        self,
        chapter: Chapter,
        foreshadowing: list[dict[str, Any]],
        frontier: int | None = None,
    ) -> list[ContinuityIssue]:
        """伏笔欠账只在写作前沿提示：回看第 5 章时不该拿第 20 章的欠账去烦作者。"""
        if frontier and chapter.chapter_number < frontier:
            return []
        issues: list[ContinuityIssue] = []
        for item in foreshadowing:
            if item.get("status") == "RESOLVED":
                continue
            anchor = item.get("last_reinforced_chapter") or item.get("first_chapter")
            if not anchor or chapter.chapter_number - anchor < _STALE_FORESHADOW_GAP:
                continue
            issues.append(
                ContinuityIssue(
                    level="warning",
                    code="FORESHADOWING_STALE",
                    message=(
                        f"伏笔「{item['name']}」自{format_chapter_ref(anchor)}后已"
                        f"{chapter.chapter_number - anchor}章未再回应。"
                    ),
                    evidence=[
                        Evidence(
                            source_chapter=format_chapter_ref(anchor),
                            ref_type="FORESHADOWING",
                            ref_id=item.get("id"),
                            detail=f"伏笔状态：{item.get('status')}；期望回收：{item.get('expected_payoff') or '未定'}",
                        )
                    ],
                    suggested_fix="在本章或后续章节给该伏笔一次呼应，或把它标记为 ABANDONED。",
                )
            )
        return issues

    # ------------------------------------------------------------------ 叙事通道
    def _narrative_pass(
        self,
        chapter: Chapter,
        canon_facts: list[dict[str, Any]],
        characters: list[dict[str, Any]],
        timeline: list[dict[str, Any]],
        events: list[dict[str, Any]],
        world_rules: list[dict[str, Any]],
        proposed_facts: list[dict[str, Any]],
    ) -> tuple[list[ContinuityIssue], list[dict[str, Any]], str, str]:
        context = {
            "chapter_number": chapter.chapter_number,
            "chapter_title": chapter.title,
            "content": chapter.content,
            "canon_facts": canon_facts,
            "characters": characters,
            "timeline": timeline,
            "events": events,
            "world_rules": world_rules,
            "proposed_facts": proposed_facts,
        }
        prompt = prompts.CONTINUITY_USER.format(
            chapter_number=chapter.chapter_number,
            title=chapter.title,
            content=(chapter.content or "")[:6000],
            canon_facts="\n".join(_fact_line(fact) for fact in canon_facts) or "（无）",
            characters="\n".join(
                f"{c['name']}：状态={c['current_status']}，位置={c['current_location']}，首次出场={format_chapter_ref(c['first_appearance'])}"
                for c in characters
            )
            or "（无）",
            timeline="\n".join(
                f"{entry['story_time']}｜{entry['event']}｜{entry['location']}｜{format_chapter_ref(entry['chapter_number'])}"
                for entry in timeline
            )
            or "（无）",
            events="\n".join(
                f"{format_chapter_ref(e['chapter_number'])}｜{e['description'][:60]}｜人物：{'、'.join(e['characters'])}"
                for e in events
            )
            or "（无）",
            world_rules="\n".join(f"{rule['name']}：{rule['description']}" for rule in world_rules) or "（无）",
            proposed_facts="\n".join(
                f"{fact['subject']} {fact['predicate']} {fact['object']}（{format_chapter_ref(fact.get('source_chapter'))}）"
                for fact in proposed_facts
            )
            or "（无）",
        )
        request = AIRequest(
            task="continuity_narrative",
            system=prompts.CONTINUITY_SYSTEM,
            prompt=prompt,
            context=context,
            json_schema={"type": "object"},
            temperature=0.2,
            max_tokens=8192,
        )
        response = self.provider.generate(request)
        raw_issues = parse_issues_payload(response.text or "")
        if response.parsed and isinstance(response.parsed.get("issues"), list):
            raw_issues = [item for item in response.parsed["issues"] if isinstance(item, dict)]

        accepted: list[ContinuityIssue] = []
        dropped: list[dict[str, Any]] = []
        for item in raw_issues:
            try:
                accepted.append(ContinuityIssue.model_validate(item))
            except ValidationError as exc:
                dropped.append(
                    {
                        "reason": "缺少可核验的证据，模型候选被丢弃",
                        "detail": exc.errors(include_url=False, include_input=False),
                        "raw": item,
                    }
                )
        return accepted, dropped, response.provider, response.model

    # ------------------------------------------------------------------ 去重
    @staticmethod
    def _dedupe(issues: list[ContinuityIssue]) -> list[ContinuityIssue]:
        seen: set[tuple[str, str]] = set()
        unique: list[ContinuityIssue] = []
        for issue in issues:
            key = (issue.code, issue.message)
            if key in seen:
                continue
            seen.add(key)
            unique.append(issue)
        unique.sort(key=lambda item: (0 if item.level == "error" else 1, item.code))
        return unique
