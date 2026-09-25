"""读者数据回环接口：导入平台后台的章节数据，并给出「判定 vs 实际」的对照。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.database import get_session
from app.models import Novel, ReaderMetric
from app.schemas import ReaderImportRequest, ReaderImportResult, ReaderAnalysisOut
from app.services import reader_service

novel_router = APIRouter(prefix="/api/novels", tags=["读者数据"])


def _novel(session: Session, novel_id: str) -> Novel:
    novel = session.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(status_code=404, detail="小说不存在")
    return novel


@novel_router.post("/{novel_id}/reader-metrics", response_model=ReaderImportResult)
def import_reader_metrics(
    novel_id: str, payload: ReaderImportRequest, session: Session = Depends(get_session)
) -> dict:
    """导入章节数据（阅读人数 / 完读率 / 追读率 / 收益 / 评论数）。

    直接从平台后台复制粘贴即可：制表符、逗号、多空格都能分列，带百分号与千分位也能认。
    同一章号会覆盖，并保留原始行以便回溯。
    """
    novel = _novel(session, novel_id)
    result = reader_service.import_rows(session, novel, payload.text, note=payload.note or "")
    if result["imported"] == 0:
        raise HTTPException(status_code=400, detail=result["message"])
    session.commit()
    return result


@novel_router.get("/{novel_id}/reader-metrics", response_model=ReaderAnalysisOut)
def reader_analysis(novel_id: str, session: Session = Depends(get_session)) -> dict:
    """读者的实际表现与我们的规则判定并排看：判定有没有效、我们漏了什么、读者在哪一章掉的。"""
    novel = _novel(session, novel_id)
    return reader_service.reader_analysis(session, novel)


@novel_router.delete("/{novel_id}/reader-metrics")
def clear_reader_metrics(
    novel_id: str, chapter_number: int | None = None, session: Session = Depends(get_session)
) -> dict:
    """清掉导入的数据（给一章或整本）。"""
    novel = _novel(session, novel_id)
    stmt = delete(ReaderMetric).where(ReaderMetric.novel_id == novel.id)
    if chapter_number is not None:
        stmt = stmt.where(ReaderMetric.chapter_number == chapter_number)
    result = session.execute(stmt)
    session.commit()
    return {"deleted": result.rowcount or 0}


@novel_router.get("/{novel_id}/reader-metrics/raw")
def reader_raw(novel_id: str, session: Session = Depends(get_session)) -> list[dict]:
    """已导入的原始行，便于核对是不是抄错了列。"""
    _novel(session, novel_id)
    rows = session.scalars(
        select(ReaderMetric)
        .where(ReaderMetric.novel_id == novel_id)
        .order_by(ReaderMetric.chapter_number)
    )
    return [
        {
            "chapter_number": item.chapter_number,
            "reads": item.reads,
            "completion_rate": item.completion_rate,
            "retention_rate": item.retention_rate,
            "revenue": item.revenue,
            "comments": item.comments,
            "raw": item.raw,
            "recorded_at": item.recorded_at.isoformat() if item.recorded_at else None,
        }
        for item in rows
    ]
