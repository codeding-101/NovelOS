"""ClaimVerifier：把正文里的「断言」逐条拆出来，对着已有设定核对。

与 ContinuityChecker 的分工：Checker 检查**抽取结果**之间是否打架（人物状态、时间线、
事件），ClaimVerifier 检查**任意一段正文**（AI 生成稿、草稿、碎片成文）里每一句断言
是否与 Canon 事实、世界观规则一致。

三条硬规矩，都是为了让「模型只负责拆句，判定交给可核对的规则」：
1. 模型给出的每条声称都必须附上正文里逐字存在的片段，找不到片段的候选直接丢弃；
2. 判定在 Python 里做：命中 Canon 事实 → SUPPORTED；同一主体的同一谓词给出不同取值 → CONFLICT；
   Canon 里查无此设定 → UNVERIFIED（等作者确认，绝不自动写进 Canon）；
3. 世界观规则里带结构的那几类（死者不可复生、本命灵剑唯一、能力门槛、不可变特质）
   直接做结构化判定，而不是靠文本相似度猜。
"""

from __future__ import annotations

import re

from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest
from app.models import CanonStatus, Character, Novel
from app.services import query_service, text_rules
from app.timeutil import split_sentences

MIN_QUOTE_LEN = 4
#: 取值匹配的相似度下限：Canon 说「青锋剑」、正文写「青锋」也算同一件东西
OBJECT_MATCH_SIMILARITY = 0.6
#: 自由文本规则的相关度下限：太低会把无关规则也牵进来
RULE_RELEVANCE = 0.34

#: 谓词归一化：模型可能用「兵器」「剑」，Canon 里统一记作「佩剑」/「武器」
PREDICATE_GROUPS: dict[str, str] = {
    "佩剑": "武器",
    "佩刀": "武器",
    "兵器": "武器",
    "剑": "武器",
    "刀": "武器",
    "本命灵剑": "本命灵剑",
    "门派": "身份",
    "宗门": "身份",
    "师门": "身份",
    "职务": "身份",
    "境界": "修为",
    "修为": "修为",
    "师父": "师承",
    "师尊": "师承",
    "死因": "死因",
    "父亲": "父亲",
    "状态": "状态",
}

#: 境界阶梯：能力类规则里读到门槛名称时，用它比较谁高谁低
REALM_LADDER: tuple[str, ...] = ("炼气", "筑基", "金丹", "元婴", "化神")

#: 「改变」类动词：不可变特质（灵根／血脉）出现这些词就意味着改设定
CHANGE_VERBS: tuple[str, ...] = (
    "变成", "成了", "化为", "转变", "觉醒为", "进化", "突变为", "改成了", "换成", "化作",
)
#: 判定「死者是否又在行动」用的补集：动作表之外，这些字出现在人名同句也说明人在动
EXTRA_ACTION_CHARS = "提迎迈跃跨立睁躺俯撑踏拥拽塞"
#: 追述标记：出现这些词说明是在回忆，不是在当场行动
RETROSPECTIVE_MARKERS: tuple[str, ...] = (
    "当年", "生前", "曾经", "记得", "那一年", "想起", "回忆起", "遗言", "坟", "碑", "灵位",
)
#: 否定词：判定规则文本与正文断言的极性是否相反
NEGATION_MARKERS: tuple[str, ...] = (
    "不可", "不能", "不得", "禁止", "无法", "不许", "不存在", "绝非", "并非", "永不", "不再",
)
#: 状态类取值的关键词：用于判断「这条断言是不是在说状态」
STATUS_TOKENS: tuple[str, ...] = (
    "死亡", "已死", "身亡", "战死", "气绝", "断气", "殒命", "死去", "重伤", "昏迷",
    "苏醒", "失踪", "闭关", "被囚", "封印", "受伤", "中毒", "正常", "行动", "活着", "清醒",
)


class ClaimDraft(BaseModel):
    """模型拆出的一条断言。quote 必须能在正文里逐字找到。"""

    subject: str = Field(min_length=1, max_length=64)
    predicate: str = Field(min_length=1, max_length=64)
    object: str = Field(min_length=1, max_length=160)
    kind: str = "SETTING"
    quote: str = Field(min_length=MIN_QUOTE_LEN)


class ClaimModelResult(BaseModel):
    claims: list[ClaimDraft] = Field(default_factory=list)
    unknown_entities: list[str] = Field(default_factory=list)
    summary: str = ""


def normalize_predicate(predicate: str) -> str:
    text = (predicate or "").strip()
    if not text:
        return ""
    if text in PREDICATE_GROUPS:
        return PREDICATE_GROUPS[text]
    for key, value in PREDICATE_GROUPS.items():
        if key and key in text:
            return value
    return text


def object_matches(left: str, right: str) -> bool:
    """两个取值是不是同一件事：包含关系或二元组重合度够高都算。"""
    left, right = (left or "").strip(), (right or "").strip()
    if not left or not right:
        return False
    if left == right:
        return True
    if len(left) >= 2 and (left in right or right in left):
        return True
    return text_rules.similarity(left, right) >= OBJECT_MATCH_SIMILARITY


def comparable_object(claim_object: str, canon_object: str, predicate: str = "") -> bool:
    """两个取值能不能直接判定冲突：都应当是短实体名，且形状要跟谓词匹配。

    模型偶尔把描述性短语当成取值（「师承=师父所授第一式是'守'」「状态=白发」）——
    这类表述与 Canon 的取值不同，但它是**表述问题**而不是设定冲突。
    宁可降级成「请人工确认」，也不能报出一条假冲突。
    """
    for value in (claim_object, canon_object):
        text = (value or "").strip()
        if not text or len(text) > 12:
            return False
        if any(mark in text for mark in "“”‘’「」\"'\n，。、；：！？—-"):
            return False
    group = normalize_predicate(predicate) if predicate else ""
    if group == "状态":
        # 状态类取值必须真的是状态词，否则「白发」会被拿去和「死亡」比
        if not any(token in claim_object for token in STATUS_TOKENS):
            return False
        if not any(token in canon_object for token in STATUS_TOKENS):
            return False
    if group in ("武器", "师承", "父亲", "身份") and len(claim_object.strip()) > 8:
        return False
    return True


def _has_negation(text: str) -> bool:
    return any(marker in (text or "") for marker in NEGATION_MARKERS)


def _realm_rank(text: str) -> int:
    """从一段文字里读出境界名，返回它在阶梯上的位置（读不到返回 -1）。"""
    for index, realm in enumerate(REALM_LADDER):
        if realm in (text or ""):
            return index
    return -1


class ClaimVerifier:
    """正文 → 断言 → 与 Canon / 世界观规则核对。"""

    task = "claim_verify"

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 上下文
    def build_context(
        self, session: Session, novel: Novel, *, as_of_chapter: int | None = None
    ) -> dict[str, Any]:
        characters = list(session.scalars(select(Character).where(Character.novel_id == novel.id)))
        names = [character.name for character in characters]
        if as_of_chapter:
            facts = query_service.canon_as_of(session, novel.id, as_of_chapter)
        else:
            facts = query_service.list_canon_facts(session, novel.id)
        # PROPOSED 单独列出：命中它只说明「有人提过」，不能算已验证，更不能算冲突
        proposed = query_service.list_canon_facts(
            session, novel.id, status=CanonStatus.PROPOSED, limit=200
        )
        states = {
            character.name: {
                "current_status": character.current_status,
                "current_location": character.current_location,
            }
            for character in characters
        }
        return {
            "names": names,
            "facts": facts,
            "proposed": proposed,
            "states": states,
            "rules": query_service.list_world_rules(session, novel.id),
        }

    # ------------------------------------------------------------------ 抽取
    def _deterministic_claims(self, text: str, names: list[str]) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for fact in text_rules.extract_attribute_facts(text, names):
            claims.append(
                {
                    "subject": fact["subject"],
                    "predicate": fact["predicate"],
                    "object": fact["object"],
                    "kind": "SETTING",
                    "quote": fact["sentence"],
                    "origin": "deterministic",
                }
            )
        return claims

    def _model_claims(
        self, session: Session, novel: Novel, text: str, names: list[str]
    ) -> tuple[list[dict[str, Any]], list[str], list[str], str]:
        """返回 (声称, 未登记的实体, warnings, summary)。"""
        request = AIRequest(
            task=self.task,
            system=prompts.CLAIM_SYSTEM,
            prompt=prompts.CLAIM_USER.format(
                known_names="、".join(names) or "（未建档）",
                content=(text or "")[:12000],
            ),
            context={"novel_id": novel.id, "content": text, "names": names},
            json_schema=ClaimModelResult.model_json_schema(),
            temperature=0.1,
            max_tokens=3072,
        )
        response = self.provider.generate(request)
        warnings = list(response.warnings)
        parsed = response.parsed
        if parsed is None:
            warnings.append("声称抽取首次输出无法解析为 JSON，已请求模型修复重试一次")
            repair = AIRequest(
                task=self.task,
                system=prompts.CLAIM_SYSTEM,
                prompt=(
                    prompts.CLAIM_USER.format(
                        known_names="、".join(names) or "（未建档）",
                        content=(text or "")[:12000],
                    )
                    + "\n\n【上一次输出无法解析】\n"
                    + (response.text or "")[:1500]
                    + '\n\n请只输出 {"claims": [...], "unknown_entities": [...], "summary": "..."}，'
                    "不要任何解释。"
                ),
                context=request.context,
                json_schema=ClaimModelResult.model_json_schema(),
                temperature=0.0,
                max_tokens=3072,
            )
            response = self.provider.generate(repair)
            warnings.extend(response.warnings)
            parsed = response.parsed
        if not isinstance(parsed, dict):
            return [], [], warnings, ""

        claims: list[dict[str, Any]] = []
        dropped = 0
        for item in parsed.get("claims") or []:
            try:
                draft = ClaimDraft.model_validate(item)
            except ValidationError:
                dropped += 1
                continue
            if draft.quote and draft.quote not in (text or ""):
                dropped += 1
                continue
            claims.append({**draft.model_dump(), "origin": "model"})
        if dropped:
            warnings.append(f"已丢弃 {dropped} 条无法在正文里逐字核对的声称（防止凭印象判定）")

        entities: list[str] = []
        dropped_entities = 0
        for name in parsed.get("unknown_entities") or []:
            item = str(name).strip()
            if not item or item not in (text or ""):
                dropped_entities += 1
                continue
            if item in names:
                continue
            entities.append(item)
        if dropped_entities:
            warnings.append(f"已丢弃 {dropped_entities} 个不在正文里的「新实体」")
        summary = str(parsed.get("summary") or "")
        return claims, entities, warnings, summary

    # ------------------------------------------------------------------ 判定
    def verify(
        self,
        session: Session,
        novel: Novel,
        text: str,
        *,
        as_of_chapter: int | None = None,
        use_model: bool = True,
    ) -> dict[str, Any]:
        context = self.build_context(session, novel, as_of_chapter=as_of_chapter)
        names = context["names"]
        warnings: list[str] = []
        claims = self._deterministic_claims(text, names)
        summary = ""
        if use_model and self.provider.kind == "llm":
            model_claims, entities, model_warnings, summary = self._model_claims(
                session, novel, text, names
            )
            warnings.extend(model_warnings)
            seen = {
                (claim["subject"], normalize_predicate(claim["predicate"]), claim["object"])
                for claim in claims
            }
            for claim in model_claims:
                key = (
                    claim["subject"],
                    normalize_predicate(claim["predicate"]),
                    claim["object"],
                )
                if key in seen:
                    continue
                seen.add(key)
                claims.append(claim)
        else:
            entities = []

        verdicts = verify_claims(
            claims,
            facts=context["facts"],
            proposed=context["proposed"],
            rules=context["rules"],
            names=names,
            states=context["states"],
            text=text,
        )
        known_locations = [
            str(state.get("current_location") or "")
            for state in (context["states"] or {}).values()
        ]
        for entity in entities:
            if _is_known_entity(entity, names, context["facts"], known_locations):
                continue
            sentences = _sentences_with(text, entity)
            verdicts.append(
                {
                    "claim": {
                        "subject": entity,
                        "predicate": "实体",
                        "object": "新出现",
                        "kind": "ENTITY",
                        "quote": sentences[0][:120] if sentences else entity,
                        "origin": "model",
                    },
                    "verdict": "UNVERIFIED",
                    "reason": f"「{entity}」没有建档记录：确认是新人/新地名/新物件后建议补进设定",
                    "evidence": [],
                }
            )
        conflicts = [item for item in verdicts if item["verdict"] == "CONFLICT"]
        verdicts = _drop_shadowed_unverified(verdicts, conflicts)
        conflicts = [item for item in verdicts if item["verdict"] == "CONFLICT"]
        unverified = [item for item in verdicts if item["verdict"] == "UNVERIFIED"]
        supported = [item for item in verdicts if item["verdict"] == "SUPPORTED"]
        return {
            "claims": verdicts,
            "conflicts": conflicts,
            "unverified": unverified,
            "supported": supported,
            "claim_count": len(verdicts),
            "conflict_count": len(conflicts),
            "unverified_count": len(unverified),
            "supported_count": len(supported),
            "as_of_chapter": as_of_chapter,
            "provider": self.provider.name,
            "model": self.provider.model,
            "warnings": warnings,
            "summary": summary,
        }


def verify_claims(
    claims: list[dict[str, Any]],
    *,
    facts: list[dict[str, Any]],
    rules: list[dict[str, Any]],
    names: list[str],
    proposed: list[dict[str, Any]] | None = None,
    states: dict[str, dict[str, Any]] | None = None,
    text: str = "",
) -> list[dict[str, Any]]:
    """逐条判定：命中 Canon → SUPPORTED；同谓词不同取值 → CONFLICT；查无 → UNVERIFIED。"""
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        by_subject.setdefault(fact["subject"], []).append(fact)
    proposed_by_subject: dict[str, list[dict[str, Any]]] = {}
    for fact in proposed or []:
        proposed_by_subject.setdefault(fact["subject"], []).append(fact)

    verdicts: list[dict[str, Any]] = []
    for claim in claims:
        subject = claim["subject"]
        predicate = normalize_predicate(claim["predicate"])
        related = [
            fact
            for fact in by_subject.get(subject, [])
            if normalize_predicate(fact["predicate"]) == predicate
        ]
        if related:
            matched = [fact for fact in related if object_matches(fact["object"], claim["object"])]
            if matched:
                verdicts.append(
                    _verdict(
                        claim,
                        "SUPPORTED",
                        f"与已确认设定一致：{subject}的{claim['predicate']}是{matched[0]['object']}",
                        [_fact_evidence(fact) for fact in matched],
                    )
                )
                continue
            if not comparable_object(claim["object"], related[0]["object"], claim["predicate"]):
                verdicts.append(
                    _verdict(
                        claim,
                        "UNVERIFIED",
                        f"这条断言与已确认设定「{subject}的{related[0]['predicate']}="
                        f"{related[0]['object']}」对不上，但它不像一个设定取值，无法自动判定："
                        "请人工确认是改写还是新设定",
                        [_fact_evidence(fact) for fact in related],
                    )
                )
                continue
            verdicts.append(
                _verdict(
                    claim,
                    "CONFLICT",
                    f"与已确认设定冲突：Canon 记的是「{subject}的{related[0]['predicate']}"
                    f"={related[0]['object']}」（第{related[0].get('source_chapter')}章），"
                    f"正文写的是「{claim['object']}」",
                    [_fact_evidence(fact) for fact in related],
                )
            )
            continue

        pending = [
            fact
            for fact in proposed_by_subject.get(subject, [])
            if normalize_predicate(fact["predicate"]) == predicate
        ]
        if pending:
            verdicts.append(
                _verdict(
                    claim,
                    "UNVERIFIED",
                    f"Canon 里没有这条设定；有一条待确认的相同条目「{pending[0]['object']}」，"
                    "确认后才会生效",
                    [_fact_evidence(fact) for fact in pending],
                )
            )
            continue

        if subject not in names and subject not in by_subject:
            reason = f"「{subject}」没有建档记录：确认是新人物/新地名后建议补进设定"
        else:
            reason = (
                f"Canon 里没有「{subject}·{predicate}」这条设定："
                "确认接受就补进 Canon，不打算用就改掉正文"
            )
        verdicts.append(_verdict(claim, "UNVERIFIED", reason, []))

    verdicts.extend(_rule_verdicts(claims, rules=rules, facts=facts, names=names, states=states, text=text))
    return verdicts


def _rule_verdicts(
    claims: list[dict[str, Any]],
    *,
    rules: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    names: list[str],
    states: dict[str, dict[str, Any]] | None,
    text: str,
) -> list[dict[str, Any]]:
    """世界观规则检查：结构化规则走结构判定，其余走「相关但相反」判定。"""
    verdicts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for rule in rules:
        rule_type = rule.get("rule_type") or ""
        subject = (rule.get("subject") or "").strip()
        description = rule.get("description") or ""
        evidence = [
            {
                "kind": "WORLD_RULE",
                "text": f"{rule.get('name')}：{description}",
                "source_chapter": rule.get("source_chapter"),
                "rule_type": rule_type,
            }
        ]

        if rule_type == "no_resurrection":
            for name in names:
                if not _is_dead(name, facts, states):
                    continue
                sentence = _acting_sentence(text, name)
                if not sentence:
                    continue
                key = (name, "no_resurrection")
                if key in seen:
                    continue
                seen.add(key)
                verdicts.append(
                    {
                        "claim": {
                            "subject": name,
                            "predicate": "状态",
                            "object": "仍在行动",
                            "kind": "ABILITY",
                            "quote": sentence[:120],
                            "origin": "rule",
                        },
                        "verdict": "CONFLICT",
                        "reason": f"与世界观规则冲突：{name} 已被记为死亡，正文里却在行动",
                        "evidence": evidence,
                    }
                )
            continue

        if rule_type == "possession_unique" and subject:
            holders: dict[str, set[str]] = {}
            for claim in claims:
                if subject and subject in (claim["predicate"] or ""):
                    holders.setdefault(claim["subject"], set()).add(claim["object"])
            for holder, objects in holders.items():
                if len(objects) < 2:
                    continue
                key = (holder, "possession_unique")
                if key in seen:
                    continue
                seen.add(key)
                verdicts.append(
                    {
                        "claim": {
                            "subject": holder,
                            "predicate": subject,
                            "object": "、".join(sorted(objects)),
                            "kind": "SETTING",
                            "quote": text_rules.sentence_with(text, subject) or subject,
                            "origin": "rule",
                        },
                        "verdict": "CONFLICT",
                        "reason": f"与世界观规则冲突：{subject} 不可同时有两个（{('、'.join(sorted(objects)))}）",
                        "evidence": evidence,
                    }
                )
            continue

        if rule_type == "capability_gate" and subject:
            required = _realm_rank(description)
            if required < 0:
                continue
            for name in names:
                realm = _character_realm(name, facts)
                if realm < 0 or realm >= required:
                    continue
                if subject not in text:
                    continue
                for sentence in _sentences_with(text, name):
                    if subject not in sentence or _has_negation(sentence):
                        continue
                    key = (name, f"capability_gate:{subject}")
                    if key in seen:
                        break
                    seen.add(key)
                    verdicts.append(
                        {
                            "claim": {
                                "subject": name,
                                "predicate": subject,
                                "object": REALM_LADDER[realm],
                                "kind": "ABILITY",
                                "quote": sentence[:120],
                                "origin": "rule",
                            },
                            "verdict": "CONFLICT",
                            "reason": (
                                f"与世界观规则冲突：{description}而 {name} 的修为只有"
                                f"{REALM_LADDER[realm]}"
                            ),
                            "evidence": evidence,
                        }
                    )
                    break
            continue

        if rule_type == "immutable_trait" and subject:
            for sentence in _sentences_with(text, subject):
                if not any(verb in sentence for verb in CHANGE_VERBS):
                    continue
                key = (subject, "immutable_trait")
                if key in seen:
                    continue
                seen.add(key)
                verdicts.append(
                    {
                        "claim": {
                            "subject": subject,
                            "predicate": "状态",
                            "object": "被改变",
                            "kind": "SETTING",
                            "quote": sentence,
                            "origin": "rule",
                        },
                        "verdict": "CONFLICT",
                        "reason": f"与世界观规则冲突：{description}",
                        "evidence": evidence,
                    }
                )
            continue

        # 自由文本规则：正文里出现相关表述且极性与规则相反时才算冲突
        for claim in claims:
            quote = claim.get("quote") or ""
            if len(quote) < MIN_QUOTE_LEN:
                continue
            if text_rules.similarity(quote, description) < RULE_RELEVANCE:
                continue
            if _has_negation(quote) == _has_negation(description):
                continue
            key = (claim["subject"], rule.get("id") or rule.get("name") or "")
            if key in seen:
                continue
            seen.add(key)
            verdicts.append(
                {
                    "claim": claim,
                    "verdict": "CONFLICT",
                    "reason": (
                        f"与世界观规则「{rule.get('name')}」表述相反：{description}"
                    ),
                    "evidence": evidence,
                }
            )
    return verdicts


def _normalize_quote(text: str) -> str:
    """去掉标点与空白后的片段：模型给的片段与规则取出的整句常常只差一个句号。"""
    return re.sub(r"[\s，。；：！？、“”‘’「」\"'—…\-]", "", text or "")


def _drop_shadowed_unverified(
    verdicts: list[dict[str, Any]], conflicts: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """同一句话既被规则判成冲突、又只是「查无此设定」时，只保留冲突那条。

    例：「林默御剑飞行」既会命中能力门槛规则（冲突），又会因为 Canon 里没有「能力」这条
    而进待确认 —— 两条指向同一句原文，留着只会让作者多看一遍。冲突的信息量更大，保留它。
    """
    if not conflicts:
        return verdicts
    quoted = [
        _normalize_quote(item["claim"].get("quote") or "")
        for item in conflicts
        if len(_normalize_quote(item["claim"].get("quote") or "")) >= MIN_QUOTE_LEN
    ]
    if not quoted:
        return verdicts
    kept: list[dict[str, Any]] = []
    for item in verdicts:
        if item["verdict"] == "UNVERIFIED":
            quote = _normalize_quote(item["claim"].get("quote") or "")
            if len(quote) >= MIN_QUOTE_LEN and any(
                quote in known or known in quote for known in quoted
            ):
                continue
        kept.append(item)
    return kept


def _is_known_entity(
    entity: str, names: list[str], facts: list[dict[str, Any]], extra_tokens: list[str] | None = None
) -> bool:
    """这个「新实体」其实已知吗？

    已建档的名字、Canon 里出现过的取值、人物当前所在地都算已知；
    两字的词若是已知名字的片段（如「天机」之于天机阁）也算截断而非新实体。
    """
    known = list(names) + list(extra_tokens or [])
    for fact in facts:
        known.append(str(fact.get("subject") or ""))
        known.append(str(fact.get("object") or ""))
    tokens = [token for token in known if token]
    if entity in tokens:
        return True
    return len(entity) < 3 and any(entity in token for token in tokens)


def _verdict(
    claim: dict[str, Any], verdict: str, reason: str, evidence: list[dict[str, Any]]
) -> dict[str, Any]:
    return {"claim": claim, "verdict": verdict, "reason": reason, "evidence": evidence}


def _fact_evidence(fact: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "CANON_FACT",
        "text": f"{fact['subject']}的{fact['predicate']}是{fact['object']}",
        "source_chapter": fact.get("source_chapter"),
        "fact_id": fact.get("id"),
    }


def _sentences_with(text: str, needle: str) -> list[str]:
    """包含关键片段的分句，并去掉分句开头残留的引号换行（证据要干净）。"""
    return [
        sentence.strip().lstrip("“”「」\"'\n ").strip()
        for sentence in split_sentences(text or "")
        if needle in sentence
    ]


def _acting_sentence(text: str, name: str) -> str | None:
    """死者「又在行动」的句子：人名同句出现动作词，且不是追述／回忆。

    比 detect_action_in_text 宽一档（提剑、迎上去这种也算动作），
    但仍然排除「当年」「生前」这类回叙句 —— 回忆里出现死人是对的。
    """
    from app.services.text_rules import ONE_CHAR_ACTIONS, TWO_CHAR_ACTIONS

    for sentence in _sentences_with(text, name):
        if any(marker in sentence for marker in RETROSPECTIVE_MARKERS):
            continue
        if any(verb in sentence for verb in TWO_CHAR_ACTIONS):
            return sentence
        if any(ch in sentence for ch in ONE_CHAR_ACTIONS + EXTRA_ACTION_CHARS):
            return sentence
    return None


def _is_dead(name: str, facts: list[dict[str, Any]], states: dict[str, dict[str, Any]] | None) -> bool:
    for fact in facts:
        if (
            fact["subject"] == name
            and fact["predicate"] == "状态"
            and text_rules.is_death_status(fact["object"])
        ):
            return True
    status = (states or {}).get(name, {}).get("current_status") or ""
    return text_rules.is_death_status(status)


def _character_realm(name: str, facts: list[dict[str, Any]]) -> int:
    for fact in facts:
        if fact["subject"] != name:
            continue
        if normalize_predicate(fact["predicate"]) != "修为":
            continue
        rank = _realm_rank(fact["object"])
        if rank >= 0:
            return rank
    return -1
