"""Attachment, reference and projection controls for an authenticated owner."""

from __future__ import annotations

import base64
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import Field, field_validator
from sqlalchemy import func, select, update

from app.platform.models import (
    Asset,
    AssetRevision,
    AssetReference,
    Attachment,
    LegacyRecord,
    ProjectionJob,
    SearchDocument,
    now,
)
from app.platform.projections import enqueue, insert_for, rebuild
from app.platform.works import Input, audit, digest, require_asset, require_work

router = APIRouter(prefix="/api/v1/works", tags=["asset-tools"])
ATTACHMENT_LIMIT = 4 * 1024 * 1024


class AttachmentInput(Input):
    filename: str = Field(min_length=1, max_length=256)
    data_base64: str = Field(max_length=4 * ((ATTACHMENT_LIMIT + 2) // 3))
    asset_id: str | None = Field(default=None, max_length=32)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value):
        value.encode("utf-8")
        if (
            value in {".", ".."}
            or value != PurePosixPath(value).name
            or any(c in value for c in "\\:")
            or any(ord(c) < 32 for c in value)
        ):
            raise ValueError(
                "Only a filename without path or control characters is accepted"
            )
        return value


class ReferenceInput(Input):
    source_revision_id: str = Field(min_length=1, max_length=32)
    target_revision_id: str = Field(min_length=1, max_length=32)
    relation: Literal["reference", "depends_on"] = "reference"


@router.post("/{work_id}/attachments", status_code=201)
def upload_attachment(work_id: str, body: AttachmentInput, request: Request):
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except ValueError:
        raise HTTPException(422, "附件编码无效") from None
    if len(data) > ATTACHMENT_LIMIT:
        raise HTTPException(413, "附件不能超过 4 MiB")
    with request.app.state.database.sessions.begin() as session:
        require_work(session, work_id)
        if body.asset_id:
            require_asset(session, work_id, body.asset_id)
        item = Attachment(
            work_id=work_id,
            asset_id=body.asset_id,
            filename=body.filename,
            data=data,
            sha256=digest(data),
            size=len(data),
        )
        session.add(item)
        session.flush()
        enqueue(session, work_id, "attachment", item.id)
        audit(
            session,
            "attachment.created",
            work_id,
            item.id,
            sha256=item.sha256,
            size=item.size,
        )
        return dict(
            id=item.id, filename=item.filename, sha256=item.sha256, size=item.size
        )


@router.get("/{work_id}/attachments")
def list_attachments(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            dict(row)
            for row in session.execute(
                select(
                    Attachment.id,
                    Attachment.filename,
                    Attachment.asset_id,
                    Attachment.sha256,
                    Attachment.size,
                    Attachment.created_at,
                )
                .where(Attachment.work_id == work_id)
                .order_by(Attachment.created_at, Attachment.id)
                .offset(offset)
                .limit(limit)
            ).mappings()
        ]


@router.get("/{work_id}/attachments/{attachment_id}/download")
def download_attachment(work_id: str, attachment_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        item = session.get(Attachment, attachment_id)
        if item is None or item.work_id != work_id:
            raise HTTPException(404, "附件不存在")
        if digest(item.data) != item.sha256:
            raise HTTPException(503, "附件校验失败，请检查数据库备份")
        return Response(
            item.data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename=attachment.bin; filename*=UTF-8''{quote(item.filename, safe='')}",
                "Content-Security-Policy": "sandbox",
            },
        )


@router.get("/{work_id}/projections")
def projection_status(work_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        counts = dict(
            session.execute(
                select(ProjectionJob.state, func.count())
                .where(ProjectionJob.work_id == work_id)
                .group_by(ProjectionJob.state)
            ).all()
        )
        indexed = session.scalar(
            select(func.count())
            .select_from(Asset)
            .join(
                AssetRevision,
                (AssetRevision.asset_id == Asset.id)
                & (AssetRevision.number == Asset.head_version),
            )
            .join(SearchDocument, SearchDocument.revision_id == AssetRevision.id)
            .where(Asset.work_id == work_id)
        )
        total = session.scalar(
            select(func.count()).select_from(Asset).where(Asset.work_id == work_id)
        )
        failures = session.execute(
            select(
                ProjectionJob.id,
                ProjectionJob.kind,
                ProjectionJob.attempts,
                ProjectionJob.state,
                ProjectionJob.error_code,
            )
            .where(
                ProjectionJob.work_id == work_id, ProjectionJob.error_code.is_not(None)
            )
            .order_by(ProjectionJob.id)
            .limit(50)
        ).mappings()
        return {
            "counts": counts,
            "indexed_current_assets": indexed,
            "total_assets": total,
            "failures": [dict(row) for row in failures],
        }


@router.post("/{work_id}/projections/rebuild")
def rebuild_projections(work_id: str, request: Request):
    with request.app.state.database.sessions.begin() as session:
        require_work(session, work_id)
        count = rebuild(session, work_id)
        audit(session, "projections.rebuild_requested", work_id, work_id, queued=count)
        return {"queued": count}


@router.post("/{work_id}/projections/{job_id}/retry")
def retry_projection(work_id: str, job_id: str, request: Request):
    with request.app.state.database.sessions.begin() as session:
        result = session.execute(
            update(ProjectionJob)
            .where(
                ProjectionJob.id == job_id,
                ProjectionJob.work_id == work_id,
                ProjectionJob.state == "failed",
            )
            .values(
                state="pending",
                attempts=0,
                available_at=now(),
                lease_until=None,
                token=None,
                error_code=None,
            )
        )
        if result.rowcount != 1:
            raise HTTPException(409, "该失败任务不存在或已被重试")
        audit(session, "projection.retry_requested", work_id, job_id)
        return {"state": "pending"}


@router.get("/{work_id}/search")
def search(
    work_id: str,
    request: Request,
    q: str = Query(min_length=1, max_length=200),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    if not q.strip() or "\x00" in q:
        raise HTTPException(422, "请输入有效搜索词")
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        query = select(
            Asset.id,
            Asset.kind,
            AssetRevision.id.label("revision_id"),
            AssetRevision.title,
            AssetRevision.number,
        )
        query = query.join(
            AssetRevision,
            (AssetRevision.asset_id == Asset.id)
            & (AssetRevision.number == Asset.head_version),
        ).join(SearchDocument, SearchDocument.revision_id == AssetRevision.id)
        query = query.where(
            Asset.work_id == work_id,
            SearchDocument.text.contains(q.casefold(), autoescape=True),
        )
        return [
            dict(row)
            for row in session.execute(
                query.order_by(Asset.created_at, Asset.id).offset(offset).limit(limit)
            ).mappings()
        ]


@router.post("/{work_id}/references", status_code=201)
def add_reference(work_id: str, body: ReferenceInput, request: Request):
    if body.source_revision_id == body.target_revision_id:
        raise HTTPException(422, "不能引用同一版本自身")
    with request.app.state.database.sessions.begin() as session:
        for revision_id in (body.source_revision_id, body.target_revision_id):
            revision = session.get(AssetRevision, revision_id)
            if revision is None:
                raise HTTPException(404, "版本不存在")
            require_asset(session, work_id, revision.asset_id)
        statement = insert_for(session, AssetReference).values(
            work_id=work_id, **body.model_dump()
        )
        result = session.execute(
            statement.on_conflict_do_nothing(
                index_elements=["source_revision_id", "target_revision_id", "relation"]
            )
        )
        item = session.scalar(
            select(AssetReference).where(
                AssetReference.source_revision_id == body.source_revision_id,
                AssetReference.target_revision_id == body.target_revision_id,
                AssetReference.relation == body.relation,
            )
        )
        if result.rowcount:
            audit(session, "reference.created", work_id, item.id, **body.model_dump())
        return {"id": item.id}


@router.get("/{work_id}/references")
def list_references(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        result = []
        for ref in session.scalars(
            select(AssetReference)
            .where(AssetReference.work_id == work_id)
            .order_by(AssetReference.created_at, AssetReference.id)
            .offset(offset)
            .limit(limit)
        ):
            source = session.get(AssetRevision, ref.source_revision_id)
            target = session.get(AssetRevision, ref.target_revision_id)
            result.append(
                dict(
                    id=ref.id,
                    relation=ref.relation,
                    source_title=source.title,
                    source_version=source.number,
                    source_revision_id=source.id,
                    target_title=target.title,
                    target_version=target.number,
                    target_revision_id=target.id,
                    source_is_current=session.get(Asset, source.asset_id).head_version
                    == source.number,
                    target_is_current=session.get(Asset, target.asset_id).head_version
                    == target.number,
                )
            )
        return result


@router.get("/{work_id}/legacy-records")
def list_legacy_records(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        return [
            dict(row)
            for row in session.execute(
                select(
                    LegacyRecord.id,
                    LegacyRecord.source_id,
                    LegacyRecord.table_name,
                    LegacyRecord.legacy_id,
                    LegacyRecord.asset_id,
                    LegacyRecord.revision_id,
                    LegacyRecord.sha256,
                )
                .where(LegacyRecord.work_id == work_id)
                .order_by(LegacyRecord.table_name, LegacyRecord.legacy_id)
                .offset(offset)
                .limit(limit)
            ).mappings()
        ]


@router.get("/{work_id}/legacy-records/{record_id}")
def legacy_record(work_id: str, record_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        record = session.get(LegacyRecord, record_id)
        if record is None or record.work_id != work_id:
            raise HTTPException(404, "导入记录不存在")
        return {
            "id": record.id,
            "table_name": record.table_name,
            "payload": record.payload,
            "formal_memory": False,
            "sha256": record.sha256,
        }
