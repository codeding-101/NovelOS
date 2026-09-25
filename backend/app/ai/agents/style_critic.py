"""StyleCritic：把文风度量变成可执行的修改意见。

两层：
1. 规则层（确定性）：style_service 的指标与阈值，问题定位到原文片段；
2. 模型层（可选）：规则覆盖不到的读感问题（视角漂移、角色腔调一致、信息复述、
   情绪靠旁白直说、本章无推进），同样强制要求原文片段 —— 没有依据的候选直接丢弃。

另提供 rewrite()：把问题清单交给模型（或离线规则）改稿，只改被指出的问题。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from app.ai import prompts
from app.ai.base import AIProvider, AIRequest
from app.models import Chapter, Novel, StyleReview
from app.services import style_service
from app.timeutil import count_words

MIN_QUOTE_LEN = 4
#: 一次改稿允许的声音下滑上限：超过就认为它在抹平作者，改回原稿
VOICE_REWRITE_TOLERANCE = 8.0


class StyleFinding(BaseModel):
    code: str = Field(min_length=1, max_length=48)
    level: str = "warning"
    message: str = Field(min_length=1)
    quote: str = Field(min_length=MIN_QUOTE_LEN)
    suggestion: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "level": "warning" if self.level == "warning" else "info",
            "metric": "model",
            "message": self.message,
            "value": 0.0,
            "reference": None,
            "excerpt": self.quote,
            "suggestion": self.suggestion,
        }


class StyleModelResult(BaseModel):
    issues: list[StyleFinding] = Field(default_factory=list)
    summary: str = ""


class StyleCritic:
    task = "style_review"

    def __init__(self, provider: AIProvider) -> None:
        self.provider = provider

    # ------------------------------------------------------------------ 评审
    def review(
        self,
        session: Session,
        novel: Novel,
        text: str,
        *,
        use_model: bool = True,
        persist: bool = False,
        chapter: Chapter | None = None,
        label: str = "",
        goals: str = "",
    ) -> dict[str, Any]:
        profile = style_service.default_profile(session, novel.id)
        voice_profile = style_service.default_voice_profile(session, novel.id)
        report = style_service.review_text(text, profile=profile, voice_profile=voice_profile)
        warnings: list[str] = []
        model_summary = ""
        if use_model and self.provider.kind == "llm":
            findings, warnings, model_summary = self._model_pass(
                session, novel, text, report, profile=profile, goals=goals
            )
            for finding in findings:
                if not any(
                    finding["code"] == issue["code"] and finding["excerpt"][:8] == issue["excerpt"][:8]
                    for issue in report["issues"]
                ):
                    report["issues"].append(finding)
            penalty = sum(8 if item["level"] == "warning" else 3 for item in findings)
            report["score"] = round(max(0.0, report["score"] - penalty), 1)
        report["warnings"] = warnings
        report["model_summary"] = model_summary
        report["provider"] = self.provider.name
        report["model"] = self.provider.model
        if persist:
            record = StyleReview(
                novel_id=novel.id,
                chapter_id=chapter.id if chapter else None,
                chapter_number=chapter.chapter_number if chapter else None,
                label=label,
                score=report["score"],
                metrics=report["metrics"],
                issues=report["issues"],
                provider=self.provider.name,
                model=self.provider.model,
            )
            session.add(record)
            session.flush()
            report["review_id"] = record.id
        return report

    def _model_pass(
        self,
        session: Session,
        novel: Novel,
        text: str,
        report: dict[str, Any],
        *,
        profile: Any = None,
        goals: str = "",
    ) -> tuple[list[dict[str, Any]], list[str], str]:
        metrics = report["metrics"]
        voice = report.get("voice") or {}
        baseline = (
            f"句长变异系数 {metrics['burstiness']}、对白占比 {metrics['dialogue_ratio']}、"
            f"套话密度 {metrics['cliche_per_1k']}/千字、章末钩子分 {metrics['hook_score']}"
            + (f"；基线来自 {profile.name}（{metrics['total_chars']} 字样本）" if profile else "；暂无基线")
        )
        if voice.get("available"):
            baseline += (
                f"；作者特征词（来自他自己的文字，必须尽量保留）："
                f"{'、'.join(voice.get('signature_hits') or []) or '（本段没有命中）'}"
                f"｜声音保留分 {voice.get('score')}"
            )
        rule_issues = "\n".join(
            f"{issue['code']}：{issue['message']}｜原文：{issue['excerpt'][:30]}"
            for issue in report["issues"]
        ) or "（规则层未发现问题）"
        request = AIRequest(
            task=self.task,
            system=prompts.STYLE_SYSTEM,
            prompt=prompts.STYLE_USER.format(
                baseline=baseline,
                rule_issues=rule_issues,
                content=(text or "")[:8000],
            ),
            context={
                "novel_id": novel.id,
                "metrics": metrics,
                "goals": goals,
                "content": text,
            },
            json_schema=StyleModelResult.model_json_schema(),
            temperature=0.2,
            max_tokens=8192,
        )
        response = self.provider.generate(request)
        warnings = list(response.warnings)
        if response.parsed is None:
            # 和其它 Agent 一样：解析失败要显式重试一次，而不是悄悄丢掉这一层
            warnings.append("读感评审首次输出无法解析为 JSON，已请求模型修复重试一次")
            repair = AIRequest(
                task=self.task,
                system=prompts.STYLE_SYSTEM,
                prompt=(
                    prompts.STYLE_USER.format(
                        baseline=baseline,
                        rule_issues=rule_issues,
                        content=(text or "")[:8000],
                    )
                    + "\n\n【上一次输出无法解析】\n"
                    + (response.text or "")[:1500]
                    + '\n\n请只输出 {"issues": [...], "summary": "..."} 这个 JSON 对象，不要任何解释。'
                ),
                context=request.context,
                json_schema=StyleModelResult.model_json_schema(),
                temperature=0.0,
                max_tokens=8192,
            )
            response = self.provider.generate(repair)
            warnings.extend(response.warnings)
        parsed = response.parsed or {}
        issues: list[dict[str, Any]] = []
        unverified = 0
        for item in parsed.get("issues", []) if isinstance(parsed, dict) else []:
            try:
                finding = StyleFinding.model_validate(item)
            except ValidationError:
                unverified += 1
                continue
            if finding.quote and finding.quote not in (text or ""):
                # 摘录必须能在正文里逐字找到，否则视为编造
                unverified += 1
                continue
            issues.append(finding.to_dict())
        if unverified:
            warnings.append(
                f"已丢弃 {unverified} 条没有可核对原文片段的模型意见（防止凭印象改稿）"
            )
        summary = str(parsed.get("summary") or "") if isinstance(parsed, dict) else ""
        return issues, warnings, summary

    # ------------------------------------------------------------------ 改稿
    def rewrite(
        self,
        session: Session,
        novel: Novel,
        text: str,
        issues: list[dict[str, Any]],
        *,
        goals: str = "",
        instructions: str = "",
        max_tokens: int = 8192,
        voice_guard: bool = True,
    ) -> tuple[str, list[str], str, str]:
        """返回 (改后正文, warnings, provider, model)。

        voice_guard：改稿前后比较「声音保留分」，若这次改稿把作者的特征磨掉太多，
        就直接保留原稿并说明 —— 去套话可以，把人磨平不行。
        """
        if not issues:
            return text, [], self.provider.name, self.provider.model
        issue_list = "\n".join(
            f"- [{issue.get('code')}] {issue.get('message')}"
            + (f"｜原文：{str(issue.get('excerpt') or '')[:40]}" if issue.get("excerpt") else "")
            + (f"｜改法：{issue.get('suggestion')}" if issue.get("suggestion") else "")
            for issue in issues[:20]
        )
        request = AIRequest(
            task="style_revise",
            system=prompts.STYLE_REVISE_SYSTEM,
            prompt=prompts.STYLE_REVISE_USER.format(
                goals=goals or "（未提供）",
                issues=issue_list,
                instructions=instructions or "（无额外要求）",
                content=(text or "")[:20000],
            ),
            context={
                "novel_id": novel.id,
                "content": text,
                "issues": issues,
                "goals": goals,
                "instructions": instructions,
            },
            temperature=0.4,
            max_tokens=max_tokens,
        )
        response = self.provider.generate(request)
        raw = response.text or ""
        if response.parsed and isinstance(response.parsed.get("content"), str):
            raw = response.parsed["content"]
        warnings = list(response.warnings)
        revised = raw.strip()
        if not revised:
            warnings.append("改稿返回为空，保留原稿")
            return text, warnings, response.provider, response.model

        # 退化的改稿直接不收：暴跌=模型交差式偷工（会静默丢内容），暴涨=可能另写了一章。
        # 中间地带（比如把总结句改成动作与对白）允许，但增长明显时要提醒作者核对。
        original_chars = max(count_words(text), 1)
        revised_chars = count_words(revised)
        ratio = revised_chars / original_chars
        if ratio < 0.6 or ratio > 1.8:
            warnings.append(
                f"这次改稿的篇幅从 {original_chars} 字变成 {revised_chars} 字，"
                "偏离过大，已保留原稿"
            )
            return text, warnings, response.provider, response.model
        if ratio > 1.2:
            warnings.append(
                f"改稿篇幅增长较多（{original_chars} → {revised_chars} 字，+{round((ratio - 1) * 100)}%），"
                "请核对是不是把结论扩写成了新情节"
            )

        if voice_guard:
            profile = style_service.default_voice_profile(session, novel.id)
            before = style_service.voice_score(text, profile)
            after = style_service.voice_score(revised, profile)
            if (
                before.get("score") is not None
                and after.get("score") is not None
                and after["score"] < before["score"] - VOICE_REWRITE_TOLERANCE
            ):
                warnings.append(
                    f"这次改稿把作者的声音磨掉了（声音保留分 {before['score']} → {after['score']}），"
                    "已保留原稿；如确需强改，可关掉声音护栏"
                )
                return text, warnings, response.provider, response.model
        return revised, warnings, response.provider, response.model
