"""全量一致性扫描与总览看板。

扫描有两种模式：
- rules：只跑确定性规则（不调用模型），秒级完成，用于「每次改动后全面体检」；
- full：先逐章跑 ExtractorAgent 再审校，会消耗模型调用，用于「大版本前的深度体检」。
两种模式都把每章结果写进 continuity_reports，看板再汇总成每章问题矩阵与错误类型统计。
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    CanonFact,
    CanonStatus,
    Chapter,
    ContinuityReport,
    ExtractionItem,
    ExtractionRun,
    ItemReviewStatus,
    Novel,
    StyleReview,
    SweepRun,
)
from app.schemas import ExtractionResult, SweepRequest
from app.services import (
    claim_service,
    commitment_service,
    continuity_service,
    extraction_service,
    foreshadow_service,
    fragment_service,
    invariant_service,
    style_service,
    vector_service,
)


def _extraction_from_run(session: Session, chapter_id: str) -> ExtractionResult | None:
    """复用该章上一次抽取结果，让规则模式也能用上模型抽到的事实。"""
    run = session.scalar(
        select(ExtractionRun)
        .where(ExtractionRun.chapter_id == chapter_id)
        .order_by(ExtractionRun.created_at.desc())
    )
    if run is None or not run.payload:
        return None
    payload = dict(run.payload)
    payload.pop("warnings", None)
    try:
        return ExtractionResult.model_validate(payload)
    except Exception:  # noqa: BLE001 - 旧数据可能不兼容，直接退化为无抽取
        return None


def run_sweep(session: Session, novel: Novel, payload: SweepRequest) -> SweepRun:
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel.id).order_by(Chapter.chapter_number)
        )
    )
    if payload.chapter_numbers:
        wanted = set(payload.chapter_numbers)
        chapters = [chapter for chapter in chapters if chapter.chapter_number in wanted]

    sweep = SweepRun(
        novel_id=novel.id,
        mode=payload.mode,
        provider=payload.provider or "",
        model="",
        chapters_total=len(chapters),
        started_at=datetime.now(timezone.utc),
    )
    session.add(sweep)
    session.flush()

    detail_chapters: list[dict[str, Any]] = []
    error_totals: Counter[str] = Counter()
    warning_totals: Counter[str] = Counter()
    errors = 0
    warnings = 0
    checked = 0

    for chapter in chapters:
        extraction: ExtractionResult | None = None
        provider_name = payload.provider
        extraction_note: str | None = None
        if payload.mode == "full":
            try:
                extraction, _run, _warnings, provider_name, model = extraction_service.run_extraction(
                    session, novel, chapter, provider_name=payload.provider
                )
                sweep.provider, sweep.model = provider_name, model
            except Exception as exc:  # noqa: BLE001 - 抽取失败仍要跑确定性规则，不能整章跳过
                extraction_note = f"抽取失败：{exc}"
        else:
            extraction = _extraction_from_run(session, chapter.id)

        report, _ = continuity_service.run_check(
            session,
            novel,
            chapter,
            provider_name=provider_name,
            extraction=extraction,
            narrative_pass=payload.narrative_pass and payload.mode == "full",
        )
        checked += 1
        errors += len(report.errors)
        warnings += len(report.warnings)
        codes = [issue.code for issue in report.errors]
        error_totals.update(codes)
        warning_totals.update(issue.code for issue in report.warnings)
        detail_chapters.append(
            {
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "errors": len(report.errors),
                "warnings": len(report.warnings),
                "codes": codes,
                "report_id": report.report_id,
                "extraction": "model" if extraction is not None else "none",
                "note": extraction_note,
            }
        )

    sweep.chapters_checked = checked
    sweep.errors = errors
    sweep.warnings = warnings
    sweep.finished_at = datetime.now(timezone.utc)

    # 全局不变量与承诺账本是「整本书」层面的，跟着扫描一起跑一次
    invariants = invariant_service.run_invariants(session, novel)
    commitment_service.evaluate(session, novel, update=True)
    sweep.detail = {
        "chapters": detail_chapters,
        "error_totals": dict(error_totals.most_common()),
        "warning_totals": dict(warning_totals.most_common()),
        "invariants": {
            "errors": len(invariants["errors"]),
            "warnings": len(invariants["warnings"]),
            "codes": invariants["codes"],
            "report_id": invariants.get("report_id"),
            "issues": (invariants["errors"] + invariants["warnings"])[:20],
        },
    }
    session.flush()
    return sweep


def latest_sweep(session: Session, novel_id: str) -> SweepRun | None:
    return session.scalar(
        select(SweepRun).where(SweepRun.novel_id == novel_id).order_by(SweepRun.started_at.desc())
    )


def _latest_reports(session: Session, novel_id: str) -> dict[int, ContinuityReport]:
    reports: dict[int, ContinuityReport] = {}
    rows = list(
        session.scalars(
            select(ContinuityReport)
            .where(ContinuityReport.novel_id == novel_id)
            .order_by(ContinuityReport.created_at)
        )
    )
    for report in rows:
        if report.chapter_number is not None:
            reports[report.chapter_number] = report
    return reports


def dashboard(session: Session, novel: Novel) -> dict[str, Any]:
    chapters = list(
        session.scalars(
            select(Chapter).where(Chapter.novel_id == novel.id).order_by(Chapter.chapter_number)
        )
    )
    reports = _latest_reports(session, novel.id)
    error_totals: Counter[str] = Counter()
    health = []
    unchecked: list[int] = []
    error_chapters: list[int] = []
    for chapter in chapters:
        report = reports.get(chapter.chapter_number)
        codes = [issue["code"] for issue in (report.errors or [])] if report else []
        error_totals.update(codes)
        if report is None:
            unchecked.append(chapter.chapter_number)
        if codes:
            error_chapters.append(chapter.chapter_number)
        health.append(
            {
                "chapter_number": chapter.chapter_number,
                "title": chapter.title,
                "word_count": chapter.word_count,
                "checked_at": report.created_at if report else None,
                "errors": len(report.errors or []) if report else 0,
                "warnings": len(report.warnings or []) if report else 0,
                "top_codes": codes[:3],
            }
        )

    debt = foreshadow_service.debt(session, novel)
    commitments = commitment_service.evaluate(session, novel)
    overdue_commitments = [item for item in commitments if item["status"] == "OVERDUE"]
    invariants = invariant_service.check_invariants(session, novel)
    baseline = style_service.default_profile(session, novel.id)
    voice_profile = style_service.default_voice_profile(session, novel.id)
    fragment_stats = fragment_service.stats(session, novel.id)
    reviewed = session.scalar(
        select(func.avg(StyleReview.score)).where(StyleReview.novel_id == novel.id)
    )
    proposed = session.scalar(
        select(func.count(CanonFact.id)).where(
            CanonFact.novel_id == novel.id, CanonFact.status == CanonStatus.PROPOSED
        )
    )
    pending_items = session.scalar(
        select(func.count(ExtractionItem.id))
        .join(ExtractionRun, ExtractionRun.id == ExtractionItem.run_id)
        .where(
            ExtractionRun.novel_id == novel.id,
            ExtractionItem.review_status == ItemReviewStatus.PENDING,
        )
    )
    sweep = latest_sweep(session, novel.id)
    claim_reports = claim_service.list_reports(session, novel.id, limit=20)
    claim_conflicts = sum(item["conflict_count"] for item in claim_reports)
    claim_unverified = sum(item["unverified_count"] for item in claim_reports)
    drift = style_service.drift_report(session, novel) if baseline and baseline.locked else None
    notes: list[str] = []
    if unchecked:
        notes.append(f"还有 {len(unchecked)} 章从未做过一致性检查：" + "、".join(map(str, unchecked[:10])))
    if proposed:
        notes.append(f"有 {proposed} 条 PROPOSED 事实等待确认，未确认前不会进入 Canon 上下文")
    if claim_conflicts:
        notes.append(f"最近的设定断言核对里有 {claim_conflicts} 条与 Canon／世界观规则冲突，建议先修")
    if drift and drift["drifted_count"]:
        notes.append(f"已锁定的文风有 {drift['drifted_count']} 章出现漂移")
    baseline_missing = (
        [key for key in style_service.TRACKED_METRICS if key not in (baseline.metrics or {})]
        if baseline
        else []
    )
    if baseline_missing:
        notes.append(
            f"文风基线建立于 {str((baseline.metrics or {}).get('metrics_version') or '旧版本')}，"
            f"缺少 {len(baseline_missing)} 项当前指标，建议在「质量」面板重建基线"
        )

    return {
        "novel_id": novel.id,
        "word_count": novel.word_count,
        "chapter_count": novel.chapter_count,
        "target_word_count": novel.target_word_count,
        "chapters": health,
        "error_totals": dict(error_totals.most_common()),
        "error_chapters": error_chapters,
        "unchecked_chapters": unchecked,
        "foreshadowing_debt": debt,
        "overdue_foreshadowing": sum(1 for item in debt if item["overdue"]),
        "proposed_backlog": int(proposed or 0),
        "pending_review_items": int(pending_items or 0),
        "vector_index": vector_service.index_stats(session, novel.id),
        "latest_sweep": sweep,
        "invariant_errors": len(invariants["errors"]),
        "invariant_warnings": len(invariants["warnings"]),
        "invariant_codes": invariants["codes"],
        "invariant_issues": (invariants["errors"] + invariants["warnings"])[:20],
        "commitments_open": len([item for item in commitments if item["status"] == "OPEN"]),
        "commitments_overdue": len(overdue_commitments),
        "commitments_overdue_items": overdue_commitments[:10],
        "style_baseline": baseline.name if baseline else "",
        "style_review_average": round(float(reviewed), 1) if reviewed is not None else None,
        "voice_profile": voice_profile.name if voice_profile else "",
        "voice_terms": list((voice_profile.metrics or {}).get("signature_terms") or [])[:20]
        if voice_profile
        else [],
        "fragments_total": fragment_stats["total"],
        "fragments_unplaced": fragment_stats["unplaced"],
        "fragments_realized": fragment_stats["realized"],
        "fragment_realization_rate": fragment_stats["realization_rate"],
        "fragment_kinds": fragment_stats["by_kind"],
        "style_locked": bool(baseline.locked) if baseline else False,
        "style_drift_chapters": drift["drifted_count"] if drift else 0,
        "style_baseline_stale": bool(baseline_missing),
        "style_baseline_missing": baseline_missing,
        "claim_reports": len(claim_reports),
        "claim_conflicts": claim_conflicts,
        "claim_unverified": claim_unverified,
        "latest_claim_conflicts": [
            item["conflicts"][:3] for item in claim_reports[:3] if item["conflict_count"]
        ],
        "notes": notes,
    }
