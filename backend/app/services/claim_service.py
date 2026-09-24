"""声称核对服务：把「AI 写了什么」变成「哪几条与设定不符、哪几条查无此设定」。

调用链：ClaimVerifier 拆句并判定 → 这里负责取正文（草稿或某一章）、落库、给出报告。
红线不变：核对只产出报告，绝不写 Canon；要变成设定必须由作者确认。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Chapter, ClaimReport, Novel
from app.services import extraction_service

#: 报告里最多保留多少条判定，避免超长章节把 JSON 撑爆
MAX_CLAIMS = 200


def verify_text(
    session: Session,
    novel: Novel,
    *,
    text: str,
    chapter: Chapter | None = None,
    as_of_chapter: int | None = None,
    use_model: bool = True,
    persist: bool = True,
    label: str = "DRAFT",
    provider_name: str | None = None,
) -> dict[str, Any]:
    """核对一段正文并返回报告（persist=True 时同时落库）。"""
    agents = extraction_service.build_agents(provider_name)
    verifier = agents["claims"]
    report = verifier.verify(
        session,
        novel,
        text,
        as_of_chapter=as_of_chapter,
        use_model=use_model,
    )
    report["warnings"] = [*agents["warnings"], *report.get("warnings", [])]
    report["novel_id"] = novel.id
    report["chapter_id"] = chapter.id if chapter else None
    report["chapter_number"] = chapter.chapter_number if chapter else None
    report["label"] = label
    for key in ("claims", "conflicts", "unverified", "supported"):
        report[key] = report.get(key, [])[:MAX_CLAIMS]

    if persist:
        row = ClaimReport(
            novel_id=novel.id,
            chapter_id=chapter.id if chapter else None,
            chapter_number=chapter.chapter_number if chapter else None,
            label=label,
            as_of_chapter=as_of_chapter,
            claim_count=report["claim_count"],
            supported_count=report["supported_count"],
            unverified_count=report["unverified_count"],
            conflict_count=report["conflict_count"],
            report={
                "claims": report["claims"],
                "conflicts": report["conflicts"],
                "unverified": report["unverified"],
                "supported": report["supported"],
                "summary": report.get("summary", ""),
                "warnings": report["warnings"],
            },
            provider=report.get("provider", ""),
            model=report.get("model", ""),
        )
        session.add(row)
        session.flush()
        report["id"] = row.id
        report["created_at"] = row.created_at.isoformat()
    return report


def verify_chapter(
    session: Session,
    novel: Novel,
    chapter: Chapter,
    *,
    use_model: bool = True,
    persist: bool = True,
    as_of_chapter: int | None = None,
    provider_name: str | None = None,
) -> dict[str, Any]:
    return verify_text(
        session,
        novel,
        text=chapter.content or "",
        chapter=chapter,
        as_of_chapter=as_of_chapter if as_of_chapter is not None else chapter.chapter_number,
        use_model=use_model,
        persist=persist,
        label="CHAPTER",
        provider_name=provider_name,
    )


def report_to_dict(row: ClaimReport) -> dict[str, Any]:
    payload = row.report or {}
    return {
        "id": row.id,
        "novel_id": row.novel_id,
        "chapter_id": row.chapter_id,
        "chapter_number": row.chapter_number,
        "label": row.label,
        "as_of_chapter": row.as_of_chapter,
        "claim_count": row.claim_count,
        "supported_count": row.supported_count,
        "unverified_count": row.unverified_count,
        "conflict_count": row.conflict_count,
        "claims": payload.get("claims", []),
        "conflicts": payload.get("conflicts", []),
        "unverified": payload.get("unverified", []),
        "supported": payload.get("supported", []),
        "summary": payload.get("summary", ""),
        "warnings": payload.get("warnings", []),
        "provider": row.provider,
        "model": row.model,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_reports(
    session: Session, novel_id: str, *, chapter_number: int | None = None, limit: int = 20
) -> list[dict[str, Any]]:
    stmt = select(ClaimReport).where(ClaimReport.novel_id == novel_id)
    if chapter_number is not None:
        stmt = stmt.where(ClaimReport.chapter_number == chapter_number)
    rows = session.scalars(stmt.order_by(ClaimReport.created_at.desc()).limit(limit))
    return [report_to_dict(row) for row in rows]


def get_report(session: Session, report_id: str) -> ClaimReport | None:
    return session.get(ClaimReport, report_id)


def latest_for_chapter(session: Session, novel_id: str, chapter_number: int) -> dict[str, Any] | None:
    reports = list_reports(session, novel_id, chapter_number=chapter_number, limit=1)
    return reports[0] if reports else None
