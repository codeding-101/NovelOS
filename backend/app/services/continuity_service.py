"""一致性检查编排：跑 ContinuityChecker 并把报告落库。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.agents import ContinuityChecker
from app.ai.factory import resolve_provider
from app.models import CanonFact, CanonStatus, Chapter, ContinuityReport, Novel
from app.schemas import ContinuityReportModel, ExtractionResult
from app.services import query_service


def run_check(
    session: Session,
    novel: Novel,
    chapter: Chapter,
    *,
    provider_name: str | None = None,
    extraction: ExtractionResult | None = None,
    narrative_pass: bool = True,
    persist: bool = True,
) -> tuple[ContinuityReportModel, list[str]]:
    provider, warnings = resolve_provider(provider_name)
    checker = ContinuityChecker(provider)
    proposed = [
        query_service.canon_fact_to_dict(fact)
        for fact in session.scalars(
            select(CanonFact).where(
                CanonFact.novel_id == novel.id,
                CanonFact.status == CanonStatus.PROPOSED,
                CanonFact.source_chapter == chapter.chapter_number,
            )
        )
    ]
    report = checker.run(
        session,
        novel,
        chapter,
        extraction=extraction,
        proposed_facts=proposed,
        narrative_pass=narrative_pass,
    )
    if persist:
        record = ContinuityReport(
            novel_id=novel.id,
            chapter_id=chapter.id,
            chapter_number=chapter.chapter_number,
            provider=report.provider,
            model=report.model,
            errors=[issue.model_dump() for issue in report.errors],
            warnings=[issue.model_dump() for issue in report.warnings],
            dropped_issues=report.dropped_issues,
        )
        session.add(record)
        session.flush()
        report.report_id = record.id
        report.created_at = record.created_at
    return report, warnings


def latest_report(session: Session, novel_id: str, chapter_id: str | None = None) -> ContinuityReport | None:
    stmt = select(ContinuityReport).where(ContinuityReport.novel_id == novel_id)
    if chapter_id:
        stmt = stmt.where(ContinuityReport.chapter_id == chapter_id)
    return session.scalar(stmt.order_by(ContinuityReport.created_at.desc()).limit(1))
