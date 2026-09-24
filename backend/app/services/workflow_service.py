"""章节完成工作流（需求第六节的 15 个步骤）。

第 1—10 步在本服务中一次跑完（保存正文 → 抽取 → 标记 PROPOSED → 一致性检查 → 输出报告），
第 11—15 步（更新 Canon / 人物状态 / 事件 / 时间线 / 伏笔）必须由作者在审校面板确认后
调用 apply_run 才会执行 —— 这就是「AI 不得自动修改 CANON」的落地方式。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.ai.base import AIError
from app.models import Chapter, ChapterStatus, Novel, RunStatus
from app.schemas import ChapterCompletionReport, ExtractionRunDetail, WorkflowStep
from app.services import (
    chapter_service,
    claim_service,
    continuity_service,
    extraction_service,
    search_service,
)
from app.timeutil import count_words


def complete_chapter(
    session: Session,
    novel: Novel,
    chapter: Chapter,
    *,
    provider_name: str | None = None,
    narrative_pass: bool = True,
) -> ChapterCompletionReport:
    steps: list[WorkflowStep] = []

    # 1. 保存正文（重算字数并同步 Markdown 与检索索引）
    chapter.word_count = count_words(chapter.content or "")
    chapter_service.write_chapter_file(novel, chapter)
    search_service.index_chapter(session, chapter)
    session.flush()
    steps.append(
        WorkflowStep(
            step=1,
            name="保存正文",
            status="ok",
            detail=f"已保存并同步 Markdown 文件，当前 {chapter.word_count} 字",
        )
    )

    # 2-8. 调用 ExtractorAgent 并登记各类抽取结果（新事实一律为 PROPOSED）
    extraction_error: str | None = None
    result = None
    run = None
    warnings: list[str] = []
    try:
        result, run, warnings, provider_name_used, model = extraction_service.run_extraction(
            session, novel, chapter, provider_name=provider_name
        )
    except AIError as exc:
        extraction_error = str(exc)
    if result is None:
        for offset, label in enumerate(
            ("人物状态", "事件", "时间线", "新事实(PROPOSED)", "伏笔", "地点", "关系变化", "承诺")
        ):
            steps.append(
                WorkflowStep(
                    step=2 + offset,
                    name=f"提取{label}",
                    status="error",
                    detail=f"抽取失败，本次未产生待确认项：{extraction_error}",
                )
            )
        steps.append(
            WorkflowStep(
                step=10,
                name="新事实标记为 PROPOSED",
                status="skipped",
                detail="抽取阶段失败，没有新事实需要标记；一致性检查将改用确定性规则执行",
            )
        )
    else:
        counts = {
            "人物状态": len(result.characters_changed),
            "事件": len(result.events),
            "时间线": len(result.timeline),
            "新事实(PROPOSED)": len(result.new_facts),
            "伏笔": len(result.foreshadowing),
            "地点": len(result.locations),
            "关系变化": len(result.relationships_changed),
            "承诺": len(result.commitments),
        }
        for offset, (label, count) in enumerate(counts.items()):
            steps.append(
                WorkflowStep(
                    step=2 + offset,
                    name=f"提取{label}",
                    status="ok" if count else "warning",
                    detail=f"{count} 条" + ("" if count else "（本章未提取到，可能正文未显式表达）"),
                )
            )
        steps.append(
            WorkflowStep(
                step=10,
                name="新事实标记为 PROPOSED",
                status="ok",
                detail=f"{len(result.new_facts)} 条已写入 PROPOSED，等待作者确认后才会成为 CANON",
            )
        )

    # 9-10. 一致性检查
    report, check_warnings = continuity_service.run_check(
        session,
        novel,
        chapter,
        provider_name=provider_name,
        extraction=result,
        narrative_pass=narrative_pass,
    )
    warnings.extend(check_warnings)

    # 11b. 设定断言核对（V0.5）：与 Canon／世界观规则逐条比对，只出报告不写 Canon
    claim_report = None
    try:
        claim_report = claim_service.verify_chapter(
            session, novel, chapter, provider_name=provider_name
        )
        warnings.extend(claim_report.get("warnings") or [])
    except AIError as exc:
        warnings.append(f"设定断言核对未完成（{exc}）；可稍后在质量面板重跑")

    steps.append(
        WorkflowStep(
            step=11,
            name="运行 ContinuityChecker",
            status="error" if report.errors else ("warning" if report.warnings else "ok"),
            detail=(
                f"{len(report.errors)} 个错误、{len(report.warnings)} 个警告"
                + (f"；{len(report.dropped_issues)} 条无证据的模型候选已丢弃" if report.dropped_issues else "")
                + (
                    f"；设定断言核对：{claim_report['conflict_count']} 条冲突、"
                    f"{claim_report['unverified_count']} 条待确认"
                    if claim_report
                    else "；设定断言核对未完成"
                )
            ),
        )
    )

    run_detail: ExtractionRunDetail | None = None
    pending = 0
    if run is not None:
        session.refresh(run)
        run_detail = extraction_service.run_to_detail(run)
        pending = sum(1 for item in run_detail.items if item.review_status == "PENDING")

    chapter.status = ChapterStatus.COMPLETED
    session.flush()

    return ChapterCompletionReport(
        chapter_id=chapter.id,
        chapter_number=chapter.chapter_number,
        steps=steps,
        extraction_run=run_detail,
        continuity=report,
        claims=claim_report,
        pending_items=pending,
        proposed_facts=len(result.new_facts) if result else 0,
        errors=len(report.errors),
        warnings=len(report.warnings),
        notes=warnings,
        message=(
            (
                f"提取阶段失败（{extraction_error}）。一致性检查已改用确定性规则完成；"
                "修正后可在右侧「AI 助手 → 运行抽取」重试。"
            )
            if extraction_error
            else (
                "第 1—10 步已完成（含承诺账本）。第 11—15 步"
                "（更新 Canon / 人物状态 / 事件 / 时间线 / 伏笔）"
                "需要你在审校面板确认条目后点击「应用确认项」。"
            )
        ),
    )


def run_status_label(run_status: str) -> str:
    return {
        RunStatus.PENDING_REVIEW: "待审校",
        RunStatus.PARTIALLY_APPLIED: "部分应用",
        RunStatus.APPLIED: "已应用",
        RunStatus.REJECTED: "已全部驳回",
    }.get(run_status, run_status)
