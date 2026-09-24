"""RevisionLoop：写作 → 评审 → 改稿 → 复评，直到达标、无进展或到轮数上限。

为什么需要它：一次成稿的「AI 味」和细节问题，靠提示词是压不住的；
把它变成可度量的循环（指标下降、问题清零）才有收敛的判断依据，也才有可审计的过程记录。
停止条件写得很保守：**指标没有改善就停**，绝不为了「多改几轮」把稿子改坏。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.ai.agents.chapter_writer import ChapterWriter
from app.ai.agents.continuity import ContinuityChecker
from app.ai.agents.style_critic import StyleCritic
from app.ai.base import AIProvider
from app.models import Novel
from app.schemas import (
    ChapterCreate,
    ChapterDraft,
    RetrievalBundle,
    WriteChapterRequest,
    WriteChapterResponse,
)
from app.services import chapter_service, style_service
from app.timeutil import count_words

#: 声音保留分允许的最大下滑：超过就认为这次改稿在抹平作者
VOICE_TOLERANCE = 8.0


@dataclass
class RevisionRound:
    round: int
    stage: str
    score: float
    word_count: int
    voice_score: float | None = None
    style_codes: list[str] = field(default_factory=list)
    continuity_codes: list[str] = field(default_factory=list)
    applied: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RevisionResult:
    draft: ChapterDraft
    rounds: list[RevisionRound]
    accepted: bool
    final_score: float
    metric_deltas: dict[str, Any] = field(default_factory=dict)
    retrieved: dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    model: str = ""
    warnings: list[str] = field(default_factory=list)
    saved_chapter_id: str | None = None
    generation_id: str | None = None
    goal: str = ""
    plan_id: str | None = None


class RevisionLoop:
    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider
        self.writer = ChapterWriter(provider)
        self.critic = StyleCritic(provider)
        self.checker = ContinuityChecker(provider)

    def run(
        self,
        session: Session,
        novel: Novel,
        request: WriteChapterRequest,
        *,
        max_rounds: int = 2,
        target_score: float = 85.0,
        use_model_critic: bool = True,
        persist_reviews: bool = True,
        plan_id: str | None = None,
        initial_content: str | None = None,
        initial_title: str | None = None,
    ) -> RevisionResult:
        """initial_content 给定时从既有稿子（例如碎片成文的初稿）开始评审改稿，不重新生成。"""
        warnings: list[str] = []
        if initial_content is not None:
            content = initial_content
            title = initial_title or request.title or f"第{request.chapter_number or 0}章"
            written = WriteChapterResponse(
                draft=ChapterDraft(
                    title=title,
                    content=content,
                    word_count=count_words(content),
                    chapter_number=request.chapter_number,
                    story_time=request.story_time,
                    location=request.location,
                ),
                retrieved=RetrievalBundle(),
                provider=self.provider.name,
                model=self.provider.model,
            )
        else:
            # 写手只产出草稿：保存由闭环统一负责，避免把改稿前的半成品先存成一章
            inner_request = request.model_copy(update={"save": False})
            written = self.writer.write(session, novel, inner_request)
            warnings.extend(written.warnings)
            content = written.draft.content
            title = written.draft.title
        draft_metrics = style_service.measure(content)

        rounds: list[RevisionRound] = []
        best = {"score": -1.0, "content": content, "review": None}
        style_report = self._review(session, novel, content, request, use_model_critic, persist_reviews, "draft")
        warnings.extend(style_report.get("warnings") or [])
        rounds.append(
            self._round_row(0, "draft", style_report, self._continuity_codes(session, novel, request, content))
        )
        best = {"score": style_report["score"], "content": content, "review": style_report}
        if style_report["score"] >= target_score and not rounds[0].continuity_codes:
            return self._result(
                session, novel, request, written, content, title, rounds, True, style_report,
                draft_metrics, warnings, plan_id,
            )

        accepted = False
        for index in range(1, max_rounds + 1):
            continuity_codes = self._continuity_codes(session, novel, request, content)
            if style_report["score"] >= target_score and not continuity_codes:
                accepted = True
                break

            issues = list(style_report["issues"])
            for code in continuity_codes:
                issues.append(
                    {
                        "code": code,
                        "level": "warning",
                        "metric": "continuity",
                        "message": f"一致性检查报出 {code}，请在改稿时一并处理",
                        "excerpt": "",
                        "suggestion": "按审校面板给出的证据修改对应句子",
                    }
                )
            revised, rewrite_warnings, provider_name, model_name = self.critic.rewrite(
                session, novel, content, issues, goals=request.goals
            )
            warnings.extend(rewrite_warnings)
            if not revised.strip() or revised.strip() == content.strip():
                warnings.append(f"第 {index} 轮改稿没有产生变化，停止修订（当前分 {style_report['score']}）")
                rounds.append(self._round_row(index, "no-change", style_report, continuity_codes))
                break

            before_metrics = style_service.measure(content).to_dict()
            previous_voice = (style_report.get("voice") or {}).get("score")
            content = revised.strip()
            style_report = self._review(
                session, novel, content, request, use_model_critic, persist_reviews, f"round-{index}"
            )
            warnings.extend(style_report.get("warnings") or [])
            after_metrics = style_service.measure(content).to_dict()
            deltas = style_service.compare_metrics(before_metrics, after_metrics)
            continuity_codes = self._continuity_codes(session, novel, request, content)
            rounds.append(
                self._round_row(index, "revised", style_report, continuity_codes)
            )

            # 声音护栏：改稿可以把套话磨掉，但不能把「像作者」这件事磨掉
            current_voice = (style_report.get("voice") or {}).get("score")
            voice_lost = (
                previous_voice is not None
                and current_voice is not None
                and current_voice < previous_voice - VOICE_TOLERANCE
            )
            if voice_lost:
                warnings.append(
                    f"第 {index} 轮改稿把作者特征磨掉了一部分（声音保留分 {previous_voice} → "
                    f"{current_voice}），已回退到上一稿"
                )
                content = best["content"]
                style_report = best["review"] or style_report
                rounds[-1].stage = "reverted"
                break

            improved = style_report["score"] > best["score"] or (
                style_report["score"] == best["score"] and deltas["worsened"] == 0
            )
            if not improved:
                warnings.append(
                    f"第 {index} 轮修订后指标未改善（{best['score']} → {style_report['score']}），"
                    "回退到最好的一稿并停止"
                )
                content = best["content"]
                style_report = best["review"] or style_report
                break
            if style_report["score"] > best["score"]:
                best = {"score": style_report["score"], "content": content, "review": style_report}
            if style_report["score"] >= target_score and not continuity_codes:
                accepted = True
                break

        final_score = float(style_report["score"])
        if final_score < target_score and not accepted:
            warnings.append(
                f"修订结束仍未达到目标分 {target_score}（当前 {final_score}）；"
                "可手动编辑或用更强的模型再跑一轮"
            )
        return self._result(
            session, novel, request, written, content, title, rounds, accepted, style_report,
            draft_metrics, warnings, plan_id,
        )

    def _result(
        self,
        session: Session,
        novel: Novel,
        request: WriteChapterRequest,
        written: WriteChapterResponse,
        content: str,
        title: str,
        rounds: list[RevisionRound],
        accepted: bool,
        style_report: dict[str, Any],
        draft_metrics: Any,
        warnings: list[str],
        plan_id: str | None,
    ) -> RevisionResult:
        final_metrics = style_service.measure(content).to_dict()
        deltas = style_service.compare_metrics(draft_metrics.to_dict(), final_metrics)

        saved_chapter_id = written.saved_chapter_id
        if request.save:
            chapter = chapter_service.create_chapter(
                session,
                novel,
                ChapterCreate(
                    chapter_number=written.draft.chapter_number,
                    title=title,
                    content=content,
                    summary=f"由 RevisionLoop 生成（{self.provider.name}/{self.provider.model}）",
                    story_time=request.story_time,
                    location=request.location,
                    status="DRAFT",
                ),
            )
            saved_chapter_id = chapter.id

        return RevisionResult(
            draft=ChapterDraft(
                title=title,
                content=content,
                word_count=count_words(content),
                chapter_number=written.draft.chapter_number,
                story_time=written.draft.story_time,
                location=written.draft.location,
            ),
            rounds=rounds,
            accepted=accepted,
            final_score=float(style_report["score"]),
            metric_deltas=deltas,
            retrieved=written.retrieved.model_dump(),
            provider=self.provider.name,
            model=self.provider.model,
            warnings=warnings,
            saved_chapter_id=saved_chapter_id,
            generation_id=written.generation_id,
            goal=request.goals,
            plan_id=plan_id,
        )

    # ------------------------------------------------------------------ 内部
    @staticmethod
    def _round_row(
        index: int,
        stage: str,
        style_report: dict[str, Any],
        continuity_codes: list[str],
    ) -> RevisionRound:
        metrics = style_report.get("metrics") or {}
        voice = style_report.get("voice") or {}
        return RevisionRound(
            round=index,
            stage=stage,
            score=float(style_report.get("score") or 0.0),
            word_count=int(metrics.get("total_chars") or 0),
            voice_score=float(voice["score"]) if voice.get("score") is not None else None,
            style_codes=[issue.get("code", "") for issue in style_report.get("issues") or []],
            continuity_codes=list(continuity_codes),
        )

    def _review(
        self,
        session: Session,
        novel: Novel,
        content: str,
        request: WriteChapterRequest,
        use_model_critic: bool,
        persist: bool,
        label: str,
    ) -> dict[str, Any]:
        return self.critic.review(
            session,
            novel,
            content,
            use_model=use_model_critic,
            persist=persist,
            label=label,
            goals=request.goals,
        )

    def _continuity_codes(
        self, session: Session, novel: Novel, request: WriteChapterRequest, content: str
    ) -> list[str]:
        """对草稿跑确定性一致性规则（用不落库的临时章节对象，只取错误码）。"""
        from app.models import Chapter

        probe = Chapter(
            id=f"probe-{novel.id}",
            novel_id=novel.id,
            chapter_number=request.chapter_number or 0,
            title=request.title or "草稿",
            content=content,
        )
        report = self.checker.run(session, novel, probe, extraction=None, narrative_pass=False)
        return [issue.code for issue in report.errors]
