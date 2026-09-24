"""MemorySearch：自然语言问题 → 检索章节/事件/Canon/人物 → 带证据的回答。

安全约束：
- 检索不到任何证据时直接返回 UNKNOWN，不调用模型（不猜）。
- 模型回答中出现的章节引用必须落在检索到的证据范围内，否则判定为幻觉，
  改用确定性的证据摘要回答并记 warning。
- 证据中包含 PROPOSED 事实时，回答必须显式声明「尚未确认」，否则补上声明。
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
    Chapter,
    Character,
    Event,
    MemoryQuery,
    Novel,
    TimelineEntry,
)
from app.schemas import AskResponse, EvidenceItem
from app.services import retrieval_service, search_service
from app.timeutil import format_chapter_ref

_STOP_PHRASES = (
    "什么时候", "什么时候第一次", "第一次", "哪一章", "哪一章里", "哪里", "在哪", "是谁", "是什么",
    "为什么", "怎么", "如何", "多少", "是否", "有没有", "以及", "并且", "关于", "请问", "告诉",
    "帮我", "查一下", "找一下", "后来", "然后", "当时", "之前", "之后", "说了什么",
)
_STOP_CHARS = "的了吗呢吧啊呀么？?。，,、！!：:；;“”\"'（）()《》 　是什么谁哪怎样"
_CHAPTER_REF_RE = re.compile(r"第\s*(\d+)\s*章")
_PROPOSED_MARKERS = ("未确认", "尚未确认", "待确认", "PROPOSED", "草稿信息", "候选")
_UNKNOWN_MARKERS = ("UNKNOWN", "没有记载", "没有记录", "未收录", "没有相关", "无从")

#: 这些检索词只是叙事动词，不足以作为「问题里的内容要求」
_WEAK_TERMS = (
    "见到", "见过", "看到", "知道", "认为", "觉得", "说过", "提到", "成为", "有关", "参加",
    "属于", "使用", "拥有", "如何", "为什么", "什么", "怎么",
)


class MemorySearch:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 关键词
    def known_terms(self, session: Session, novel: Novel) -> list[str]:
        """库里已知的实体与属性词：人物、地点、事实主谓宾、事件名、时间线事件、世界规则主语。"""
        terms: set[str] = set()
        for character in session.scalars(select(Character).where(Character.novel_id == novel.id)):
            terms.add(character.name)
        for chapter in session.scalars(select(Chapter).where(Chapter.novel_id == novel.id)):
            if chapter.location:
                terms.add(chapter.location)
        for fact in session.scalars(select(CanonFact).where(CanonFact.novel_id == novel.id)):
            terms.update({fact.subject, fact.predicate, fact.object})
        for event in session.scalars(select(Event).where(Event.novel_id == novel.id)):
            if event.location:
                terms.add(event.location)
            terms.update(head for head in [(event.description or "").split("，")[0]] if head)
        for entry in session.scalars(select(TimelineEntry).where(TimelineEntry.novel_id == novel.id)):
            terms.add(entry.event)
            if entry.location:
                terms.add(entry.location)
        return sorted((term for term in terms if term), key=len, reverse=True)

    def detect_terms(
        self, session: Session, novel: Novel, question: str
    ) -> tuple[list[str], list[str], list[str]]:
        """返回 (命中的已知实体词, 其余可检索词, 已收录内容里查不到的疑问词)。"""
        known = self.known_terms(session, novel)
        entities: list[str] = []
        residue = question
        for term in known:
            if len(term) >= 2 and term in residue:
                entities.append(term)
                residue = residue.replace(term, " ")
        for phrase in _STOP_PHRASES:
            residue = residue.replace(phrase, " ")
        for char in _STOP_CHARS:
            residue = residue.replace(char, " ")

        extra: list[str] = []
        uncovered: list[str] = []
        for chunk in residue.split():
            chunk = chunk.strip()
            if len(chunk) < 2 or chunk in _WEAK_TERMS:
                continue
            hits = search_service.search_chapters(session, novel.id, chunk, limit=1)
            if hits["hits"]:
                extra.append(chunk)
            else:
                uncovered.append(chunk)
        return list(dict.fromkeys(entities)), extra, uncovered

    # ------------------------------------------------------------------ 检索
    #: 结构化证据的基础权重（乘以命中词的权重）
    _BASE_SCORE = {
        "CANON_FACT": 3.0,
        "TIMELINE": 2.5,
        "EVENT": 2.0,
        "CHARACTER": 2.0,
        "FORESHADOWING": 1.5,
    }

    def _score_hit(self, hit: Any) -> float:
        if hit.ref_type == "CHAPTER":
            if hit.keyword_score > 0:
                return hit.keyword_score
            return 1.5 if hit.vector_score >= retrieval_service.VECTOR_MIN_SCORE else 0.0
        if hit.term_weight <= 0:
            # 只被向量召回：达到阈值才算结构化证据，否则不给分
            return 1.5 if hit.vector_score >= retrieval_service.VECTOR_STRUCTURED_SCORE else 0.0
        score = self._BASE_SCORE.get(hit.ref_type, 1.0) * hit.term_weight
        if hit.ref_type == "CANON_FACT" and hit.status == "CANON":
            score += 1.5
        if "vector" in hit.channels:
            score += 1.2  # 关键词与语义双通道都命中，可信度更高
        return score

    def retrieve(
        self, session: Session, novel: Novel, question: str, max_evidence: int
    ) -> tuple[list[EvidenceItem], list[str], str]:
        entities, extra, uncovered = self.detect_terms(session, novel, question)
        terms = entities + extra
        if not terms:
            return [], uncovered, "UNKNOWN"
        weights = {term: (1.0 if term in entities else 0.4) for term in terms}

        result = retrieval_service.hybrid_search(
            session,
            novel,
            question,
            terms=terms,
            weights=weights,
            limit=max(max_evidence * 3, 12),
        )

        scored: list[tuple[float, Any]] = []
        for hit in result["raw"]:
            score = self._score_hit(hit)
            if score <= 0:
                continue
            scored.append((score, hit))
        scored.sort(key=lambda pair: -pair[0])
        scored = scored[:max_evidence]

        evidence = [
            EvidenceItem(
                ref_type=hit.ref_type,
                ref_id=hit.ref_id,
                title=hit.title,
                chapter_number=hit.chapter_number,
                chapter_title=None,
                excerpt=hit.excerpt,
                score=round(score, 3),
            )
            for score, hit in scored
        ]

        strong = [
            (score, hit) for score, hit in scored if hit.ref_type != "CHAPTER" and score >= 3.0
        ]
        structured = [
            (score, hit)
            for score, hit in scored
            if hit.ref_type != "CHAPTER"
            and (hit.term_weight > 0 or hit.vector_score >= retrieval_service.VECTOR_STRUCTURED_SCORE)
        ]
        if not evidence:
            confidence = "UNKNOWN"
        elif uncovered and not structured:
            # 只有泛用词命中的正文片段，回答不了问题：按 UNKNOWN 处理，不做推测
            confidence = "UNKNOWN"
        elif uncovered:
            confidence = "LOW"
        elif strong:
            confidence = "HIGH"
        else:
            confidence = "MEDIUM"
        return evidence, uncovered, confidence

    # ------------------------------------------------------------------ 问答
    def ask(
        self,
        session: Session,
        novel: Novel,
        question: str,
        *,
        max_evidence: int = 8,
        persist: bool = True,
    ) -> AskResponse:
        evidence, uncovered, retrieval_confidence = self.retrieve(
            session, novel, question, max_evidence
        )
        warnings: list[str] = []
        if uncovered:
            warnings.append(
                "问题中的「"
                + "」「".join(uncovered)
                + "」在已收录内容里查不到任何记载，这部分只能回答 UNKNOWN"
            )

        if not evidence or retrieval_confidence == "UNKNOWN":
            note = (
                f"（问题中的「{'」「'.join(uncovered)}」在前文没有记载）" if uncovered else ""
            )
            response = AskResponse(
                question=question,
                answer="UNKNOWN" + note,
                confidence="UNKNOWN",
                evidence=evidence,
                provider=self.provider.name,
                model=self.provider.model,
                warnings=warnings
                + ["未检索到能回答该问题的证据，按安全原则不进行推测，也不调用模型作答"],
            )
            self._persist(session, novel, response, persist)
            return response

        proposed_items = [
            item for item in evidence if item.ref_type == "CANON_FACT" and "PROPOSED" in item.excerpt
        ]
        context = {
            "question": question,
            "evidence": [
                {
                    "ref_type": item.ref_type,
                    "title": item.title,
                    "excerpt": item.excerpt,
                    "chapter_number": item.chapter_number,
                    "fact_status": "PROPOSED" if item in proposed_items else "CANON",
                }
                for item in evidence
            ],
            "retrieval_confidence": retrieval_confidence,
            "uncovered_terms": uncovered,
        }
        evidence_block = "\n".join(
            f"- [{item.ref_type}] {item.title}（{format_chapter_ref(item.chapter_number)}）：{item.excerpt}"
            for item in evidence
        )
        if uncovered:
            evidence_block += (
                "\n- [未收录] 问题中的「" + "」「".join(uncovered) + "」在已收录内容里没有任何记载。"
            )
        request = AIRequest(
            task="answer",
            system=prompts.ANSWER_SYSTEM,
            prompt=prompts.ANSWER_USER.format(question=question, evidence=evidence_block),
            context=context,
            temperature=0.2,
            max_tokens=1024,
        )
        response = self.provider.generate(request)
        warnings.extend(response.warnings)
        answer = (response.text or "").strip()
        if response.parsed and isinstance(response.parsed.get("answer"), str):
            answer = response.parsed["answer"].strip()
        confidence = retrieval_confidence
        self_reported = (response.parsed or {}).get("confidence")
        if isinstance(self_reported, str) and self_reported in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
            confidence = self_reported

        # 证据门槛 1：引用必须落在检索结果里
        allowed = {item.chapter_number for item in evidence if item.chapter_number}
        cited = {int(match) for match in _CHAPTER_REF_RE.findall(answer)}
        invented = cited - allowed
        if invented:
            warnings.append(
                "模型回答引用了未在证据中出现的章节，已改用证据摘要作答："
                + "、".join(format_chapter_ref(number) for number in sorted(invented))
            )
            answer = self._fallback_answer(evidence)
            confidence = retrieval_confidence if retrieval_confidence != "HIGH" else "MEDIUM"

        # 证据门槛 2：PROPOSED 必须显式声明未确认
        if proposed_items and not any(marker in answer for marker in _PROPOSED_MARKERS):
            warnings.append("回答涉及未确认的 PROPOSED 事实但未声明，已自动补充声明")
            answer = (
                answer
                + "\n\n（说明：以上包含尚未确认的候选信息（PROPOSED），需作者确认后才会成为 Canon 设定。）"
            )
            if confidence == "HIGH":
                confidence = "MEDIUM"

        # 证据门槛 3：问题中查不到记载的部分必须声明为 UNKNOWN
        if uncovered and not any(marker in answer for marker in _UNKNOWN_MARKERS):
            warnings.append("回答未声明问题中无记载的部分，已自动补充说明")
            answer = (
                answer
                + "\n\n（说明：问题中的「"
                + "」「".join(uncovered)
                + "」在已收录内容里没有任何记载，这部分无法回答。）"
            )

        if not answer:
            answer = self._fallback_answer(evidence)

        result = AskResponse(
            question=question,
            answer=answer,
            confidence=confidence,  # type: ignore[arg-type]
            evidence=evidence,
            provider=response.provider,
            model=response.model,
            warnings=warnings,
        )
        self._persist(session, novel, result, persist)
        return result

    # ------------------------------------------------------------------ agent 模式
    def ask_agent(
        self,
        session: Session,
        novel: Novel,
        question: str,
        *,
        max_evidence: int = 8,
        persist: bool = True,
    ) -> AskResponse:
        """模型自己通过 AITool 取数：先让模型选题与调用工具，再按同一套证据门槛收口。

        工具调用失败或模型不支持工具时，自动退回 simple 模式（同一套检索），并记 warning。
        """
        from app.ai.agents.tool_loop import ToolCallingAgent
        from app.ai.tools import AIToolKit

        entities, extra, uncovered = self.detect_terms(session, novel, question)
        kit = AIToolKit(session, novel)
        agent = ToolCallingAgent(self.provider, kit)
        loop = agent.run(
            system=prompts.AGENT_SYSTEM,
            question=question,
            context={"entities": entities + extra, "uncovered": uncovered},
        )
        warnings = list(loop.warnings)
        if not loop.answer.strip():
            fallback = self.ask(session, novel, question, max_evidence=max_evidence, persist=False)
            fallback.mode = "simple(agent 回退)"
            fallback.warnings = warnings + fallback.warnings + [
                "agent 模式没有得到最终回答，已回退到常规检索模式"
            ]
            self._persist(session, novel, fallback, persist)
            return fallback

        evidence, _, retrieval_confidence = self.retrieve(session, novel, question, max_evidence)
        proposed_items = [
            item for item in evidence if item.ref_type == "CANON_FACT" and "PROPOSED" in item.excerpt
        ]
        answer = loop.answer
        confidence = "HIGH" if loop.citations else retrieval_confidence

        # 证据门槛：agent 回答里的章节引用必须来自工具返回值或检索到的证据
        allowed = set(loop.citations) | {
            item.chapter_number for item in evidence if item.chapter_number
        }
        cited = {int(match) for match in _CHAPTER_REF_RE.findall(answer)}
        invented = cited - allowed
        if invented:
            warnings.append(
                "agent 回答引用了工具与检索结果里都没有的章节，已改用工具结果作答："
                + "、".join(format_chapter_ref(number) for number in sorted(invented))
            )
            answer = self._fallback_answer(evidence)
            confidence = retrieval_confidence if retrieval_confidence != "HIGH" else "MEDIUM"
        if proposed_items and not any(marker in answer for marker in _PROPOSED_MARKERS):
            warnings.append("回答涉及未确认的 PROPOSED 事实但未声明，已自动补充声明")
            answer += (
                "\n\n（说明：以上包含尚未确认的候选信息（PROPOSED），需作者确认后才会成为 Canon 设定。）"
            )
            if confidence == "HIGH":
                confidence = "MEDIUM"
        if uncovered and not any(marker in answer for marker in _UNKNOWN_MARKERS):
            warnings.append("回答未声明问题中无记载的部分，已自动补充说明")
            answer += (
                "\n\n（说明：问题中的「"
                + "」「".join(uncovered)
                + "」在已收录内容里没有任何记载，这部分无法回答。）"
            )

        response = AskResponse(
            question=question,
            answer=answer,
            confidence=confidence,  # type: ignore[arg-type]
            evidence=evidence,
            provider=loop.provider,
            model=loop.model,
            warnings=warnings,
            mode="agent",
            tool_calls=loop.tool_calls,
        )
        self._persist(session, novel, response, persist)
        return response

    @staticmethod
    def _fallback_answer(evidence: list[EvidenceItem]) -> str:
        lines = []
        for item in evidence[:3]:
            ref = format_chapter_ref(item.chapter_number)
            lines.append(f"{item.title}（{ref}）：{item.excerpt}")
        return "根据已检索到的证据：" + "；".join(lines) + "。"

    @staticmethod
    def _persist(session: Session, novel: Novel, response: AskResponse, persist: bool) -> None:
        if not persist:
            return
        session.add(
            MemoryQuery(
                novel_id=novel.id,
                question=response.question,
                answer=response.answer,
                confidence=response.confidence,
                evidence=[item.model_dump() for item in response.evidence],
                provider=response.provider,
                model=response.model,
            )
        )
        session.flush()
