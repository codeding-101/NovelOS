"""Authenticated draft asset operations; every write includes its audit record."""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import json
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import Literal
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, update

from app.platform.models import (
    Asset,
    AssetRevision,
    AuditEvent,
    OriginalUpload,
    Work,
    Attachment,
    AssetReference,
    LegacyRecord,
)
from app.platform.projections import enqueue

router = APIRouter(prefix="/api/v1/works", tags=["works"])
TEXT_LIMIT = 2 * 1024 * 1024
Kind = Literal["volume", "chapter", "scene", "material"]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkInput(Input):
    title: str = Field(min_length=1, max_length=256)
    description: str = Field(default="", max_length=8192)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        value.encode("utf-8")
        if not value.strip() or "\x00" in value:
            raise ValueError("Invalid title")
        return value.strip()

    @field_validator("description")
    @classmethod
    def clean_description(cls, value):
        value.encode("utf-8")
        if "\x00" in value:
            raise ValueError("Invalid description")
        return value


class TextInput(Input):
    title: str = Field(min_length=1, max_length=256)
    content: str = Field(default="", max_length=TEXT_LIMIT)

    _title = field_validator("title")(WorkInput.clean_title.__func__)

    @field_validator("content")
    @classmethod
    def valid_text(cls, value):
        # Preserve BOM/newlines/whitespace. NUL is unsupported by PostgreSQL TEXT.
        if "\x00" in value or len(value.encode("utf-8")) > TEXT_LIMIT:
            raise ValueError("Invalid text or text too large")
        return value


class AssetInput(TextInput):
    kind: Kind = "chapter"
    parent_id: str | None = Field(default=None, max_length=32)


class SaveInput(TextInput):
    expected_version: int = Field(ge=1)


class RestoreInput(Input):
    expected_version: int = Field(ge=1)


class ImportInput(Input):
    filename: str = Field(min_length=1, max_length=256)
    data_base64: str = Field(max_length=4 * ((TEXT_LIMIT + 2) // 3))
    kind: Literal["chapter", "material"] = "chapter"
    parent_id: str | None = Field(default=None, max_length=32)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require_work(session, work_id):
    item = session.get(Work, work_id)
    if item is None:
        raise HTTPException(404, "作品不存在")
    return item


def require_asset(session, work_id, asset_id):
    item = session.get(Asset, asset_id)
    if item is None or item.work_id != work_id:
        raise HTTPException(404, "该作品中没有此资产")
    return item


def require_revision(session, asset, revision_id):
    revision = session.get(AssetRevision, revision_id)
    if revision is None or revision.asset_id != asset.id:
        raise HTTPException(404, "该资产中没有此版本")
    return revision


def audit(session, action, work_id, target_id, **details):
    session.add(
        AuditEvent(
            actor="owner",
            action=action,
            target_id=target_id,
            details={"work_id": work_id, **details},
        )
    )


def revision_data(revision, include_content=True):
    result = {
        key: getattr(revision, key)
        for key in (
            "id",
            "asset_id",
            "number",
            "title",
            "sha256",
            "status",
            "restored_from",
            "created_at",
        )
    }
    if include_content:
        result["content"] = revision.content
    return result


def create_asset(session, work_id, body: AssetInput):
    require_work(session, work_id)
    if body.parent_id:
        parent = require_asset(session, work_id, body.parent_id)
        allowed = {"chapter": "volume", "scene": "chapter"}
        if allowed.get(body.kind) != parent.kind:
            raise HTTPException(422, "章节只能放在卷下，场景只能放在章节下")
    elif body.kind == "scene":
        raise HTTPException(422, "场景必须属于章节")
    asset = Asset(work_id=work_id, parent_id=body.parent_id, kind=body.kind)
    session.add(asset)
    session.flush()
    revision = AssetRevision(
        asset_id=asset.id,
        number=1,
        title=body.title,
        content=body.content,
        sha256=digest(body.content.encode()),
    )
    session.add(revision)
    session.flush()
    audit(session, "asset.created", work_id, asset.id, revision_id=revision.id)
    enqueue(session, work_id, "revision", revision.id)
    return asset, revision


def append_revision(
    session, asset, expected_version, title, content, restored_from=None
):
    # One atomic compare-and-swap; the loser rolls back without a revision or audit entry.
    result = session.execute(
        update(Asset)
        .where(Asset.id == asset.id, Asset.head_version == expected_version)
        .values(head_version=expected_version + 1)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        raise HTTPException(
            409, "正文已有新版本。请保留当前输入，读取最新版本并比较后再保存。"
        )
    revision = AssetRevision(
        asset_id=asset.id,
        number=expected_version + 1,
        title=title,
        content=content,
        sha256=digest(content.encode()),
        restored_from=restored_from,
    )
    session.add(revision)
    session.flush()
    enqueue(session, asset.work_id, "revision", revision.id)
    audit(
        session,
        "asset.restored" if restored_from else "asset.saved",
        asset.work_id,
        asset.id,
        revision_id=revision.id,
        previous_version=expected_version,
        restored_from=restored_from,
    )
    return revision


@router.get("")
def list_works(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        return [
            dict(
                id=w.id,
                title=w.title,
                description=w.description,
                created_at=w.created_at,
            )
            for w in session.scalars(
                select(Work)
                .order_by(Work.created_at, Work.id)
                .offset(offset)
                .limit(limit)
            )
        ]


@router.post("", status_code=201)
def new_work(body: WorkInput, request: Request):
    with request.app.state.database.sessions.begin() as session:
        work = Work(**body.model_dump())
        session.add(work)
        session.flush()
        audit(session, "work.created", work.id, work.id)
        return dict(id=work.id, title=work.title, description=work.description)


@router.get("/{work_id}/assets")
def list_assets(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(200, ge=1, le=1000),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        rows = session.execute(
            select(Asset, AssetRevision.id, AssetRevision.title, AssetRevision.sha256)
            .join(
                AssetRevision,
                (AssetRevision.asset_id == Asset.id)
                & (AssetRevision.number == Asset.head_version),
            )
            .where(Asset.work_id == work_id)
            .order_by(Asset.created_at, Asset.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            dict(
                id=a.id,
                kind=a.kind,
                parent_id=a.parent_id,
                head_version=a.head_version,
                revision_id=rid,
                title=title,
                sha256=sha,
                status="DRAFT",
            )
            for a, rid, title, sha in rows
        ]


@router.post("/{work_id}/assets", status_code=201)
def new_asset(work_id: str, body: AssetInput, request: Request):
    with request.app.state.database.sessions.begin() as session:
        asset, revision = create_asset(session, work_id, body)
        return {"asset_id": asset.id, "revision": revision_data(revision)}


@router.get("/{work_id}/assets/{asset_id}")
def get_asset(work_id: str, asset_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        asset = require_asset(session, work_id, asset_id)
        revision = session.scalar(
            select(AssetRevision).where(
                AssetRevision.asset_id == asset.id,
                AssetRevision.number == asset.head_version,
            )
        )
        return {
            "id": asset.id,
            "kind": asset.kind,
            "parent_id": asset.parent_id,
            "revision": revision_data(revision),
        }


@router.post("/{work_id}/assets/{asset_id}/revisions", status_code=201)
def save_asset(work_id: str, asset_id: str, body: SaveInput, request: Request):
    with request.app.state.database.sessions.begin() as session:
        asset = require_asset(session, work_id, asset_id)
        return revision_data(
            append_revision(
                session, asset, body.expected_version, body.title, body.content
            )
        )


@router.get("/{work_id}/assets/{asset_id}/revisions")
def history(
    work_id: str,
    asset_id: str,
    request: Request,
    before: int | None = Query(None, ge=1),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        asset = require_asset(session, work_id, asset_id)
        columns = [
            getattr(AssetRevision, key)
            for key in (
                "id",
                "asset_id",
                "number",
                "title",
                "sha256",
                "status",
                "restored_from",
                "created_at",
            )
        ]
        query = select(*columns).where(AssetRevision.asset_id == asset.id)
        if before is not None:
            query = query.where(AssetRevision.number < before)
        return [
            dict(row)
            for row in session.execute(
                query.order_by(AssetRevision.number.desc()).limit(limit)
            ).mappings()
        ]


@router.get("/{work_id}/assets/{asset_id}/revisions/{revision_id}")
def get_revision(work_id: str, asset_id: str, revision_id: str, request: Request):
    with request.app.state.database.sessions() as session:
        asset = require_asset(session, work_id, asset_id)
        return revision_data(require_revision(session, asset, revision_id))


@router.post(
    "/{work_id}/assets/{asset_id}/revisions/{revision_id}/restore", status_code=201
)
def restore(
    work_id: str, asset_id: str, revision_id: str, body: RestoreInput, request: Request
):
    with request.app.state.database.sessions.begin() as session:
        asset = require_asset(session, work_id, asset_id)
        source = require_revision(session, asset, revision_id)
        return revision_data(
            append_revision(
                session,
                asset,
                body.expected_version,
                source.title,
                source.content,
                source.id,
            )
        )


@router.get("/{work_id}/assets/{asset_id}/diff")
def diff(work_id: str, asset_id: str, request: Request, source: str, target: str):
    with request.app.state.database.sessions() as session:
        asset = require_asset(session, work_id, asset_id)
        a = require_revision(session, asset, source)
        b = require_revision(session, asset, target)
        if (
            len(a.content) + len(b.content) > 200_000
            or a.content.count("\n") + b.content.count("\n") > 5000
        ):
            raise HTTPException(
                413,
                "版本较大，请分别查看历史正文；当前差异预览最多支持合计 20 万字符和 5000 行",
            )
        return {
            "source": a.id,
            "target": b.id,
            "source_title": a.title,
            "target_title": b.title,
            "diff": "".join(
                line
                if line.endswith("\n")
                else line + "\n\\ No newline at end of file\n"
                for line in difflib.unified_diff(
                    a.content.splitlines(keepends=True),
                    b.content.splitlines(keepends=True),
                    fromfile=f"v{a.number}",
                    tofile=f"v{b.number}",
                )
            ),
        }


@router.post("/{work_id}/import", status_code=201)
def import_text(work_id: str, body: ImportInput, request: Request):
    filename = body.filename
    if (
        filename != PurePosixPath(filename).name
        or any(c in filename for c in "\\:\x00")
        or PurePosixPath(filename).suffix.lower() not in {".txt", ".md"}
    ):
        raise HTTPException(422, "请选择不含目录的 TXT 或 Markdown 文件名")
    try:
        raw = base64.b64decode(body.data_base64, validate=True)
        text = raw.decode("utf-8")
        data = AssetInput(
            title=PurePosixPath(filename).stem,
            content=text,
            kind=body.kind,
            parent_id=body.parent_id,
        )
    except (ValueError, UnicodeError, binascii.Error):
        raise HTTPException(
            422, "文件必须是 UTF-8 文本，且不超过 2 MiB；原文件未被修改"
        ) from None
    with request.app.state.database.sessions.begin() as session:
        asset, revision = create_asset(session, work_id, data)
        original = OriginalUpload(
            revision_id=revision.id, filename=filename, sha256=digest(raw), data=raw
        )
        session.add(original)
        session.flush()
        enqueue(session, work_id, "original", original.id)
        audit(
            session,
            "asset.imported",
            work_id,
            asset.id,
            revision_id=revision.id,
            original_sha256=digest(raw),
            filename=filename,
        )
        return {"asset_id": asset.id, "revision": revision_data(revision)}


@router.get("/{work_id}/audit")
def work_audit(
    work_id: str,
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
):
    with request.app.state.database.sessions() as session:
        require_work(session, work_id)
        records = session.scalars(
            select(AuditEvent)
            .where(AuditEvent.details["work_id"].as_string() == work_id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id)
            .offset(offset)
            .limit(limit)
        )
        return [
            dict(
                id=r.id,
                actor=r.actor,
                action=r.action,
                target_id=r.target_id,
                details=r.details,
                created_at=r.created_at,
            )
            for r in records
        ]


@router.get("/{work_id}/export")
def export_work(work_id: str, request: Request):
    archive = SpooledTemporaryFile(max_size=4 * 1024 * 1024)
    try:
        with request.app.state.database.sessions.begin() as session:
            work = require_work(session, work_id)
            # Capture heads in one statement. Subsequent saves cannot change immutable history.
            assets = list(
                session.scalars(
                    select(Asset)
                    .where(Asset.work_id == work_id)
                    .order_by(Asset.created_at, Asset.id)
                )
            )
            manifest = {
                "format_version": 2,
                "status": "DRAFT",
                "finalization": False,
                "work": {
                    "id": work.id,
                    "title": work.title,
                    "description": work.description,
                },
                "assets": [],
                "files": [],
                "references": [],
            }
            byte_count = 0
            exported_revisions = set()
            asset_ids = {asset.id for asset in assets}

            def put(zipfile, path, data, expected_hash=None):
                nonlocal byte_count
                if expected_hash is not None and digest(data) != expected_hash:
                    raise HTTPException(503, "原文或附件校验失败，导出已中止")
                byte_count += len(data)
                if byte_count > 100 * 1024 * 1024 or len(manifest["files"]) >= 10_000:
                    raise HTTPException(
                        413, "本次草稿导出超过 100 MiB 或 10000 个文件上限"
                    )
                zipfile.writestr(path, data)
                manifest["files"].append(
                    {"path": path, "sha256": digest(data), "bytes": len(data)}
                )

            with ZipFile(archive, "w", ZIP_DEFLATED) as zipfile:
                for asset in assets:
                    manifest["assets"].append(
                        dict(
                            id=asset.id,
                            kind=asset.kind,
                            parent_id=asset.parent_id,
                            head_version=asset.head_version,
                        )
                    )
                    query = (
                        select(AssetRevision)
                        .where(
                            AssetRevision.asset_id == asset.id,
                            AssetRevision.number <= asset.head_version,
                        )
                        .order_by(AssetRevision.number)
                    )
                    for revision in session.scalars(query).yield_per(1):
                        exported_revisions.add(revision.id)
                        prefix = f"assets/{asset.id}/v{revision.number}"
                        put(
                            zipfile,
                            prefix + ".txt",
                            revision.content.encode("utf-8"),
                            revision.sha256,
                        )
                        put(
                            zipfile,
                            prefix + ".json",
                            json.dumps(
                                revision_data(revision, False),
                                ensure_ascii=False,
                                default=str,
                                indent=2,
                            ).encode(),
                        )
                        original = session.scalar(
                            select(OriginalUpload).where(
                                OriginalUpload.revision_id == revision.id
                            )
                        )
                        if original is not None:
                            put(
                                zipfile,
                                f"originals/{original.id}.bin",
                                original.data,
                                original.sha256,
                            )
                            manifest["files"][-1].update(
                                filename=original.filename, revision_id=revision.id
                            )
                for attachment in session.scalars(
                    select(Attachment).where(Attachment.work_id == work_id)
                ).yield_per(1):
                    if (
                        attachment.asset_id is not None
                        and attachment.asset_id not in asset_ids
                    ):
                        continue
                    put(
                        zipfile,
                        f"attachments/{attachment.id}.bin",
                        attachment.data,
                        attachment.sha256,
                    )
                    manifest["files"][-1].update(
                        filename=attachment.filename, asset_id=attachment.asset_id
                    )
                for ref in session.scalars(
                    select(AssetReference).where(AssetReference.work_id == work_id)
                ):
                    if (
                        ref.source_revision_id in exported_revisions
                        and ref.target_revision_id in exported_revisions
                    ):
                        manifest["references"].append(
                            dict(
                                id=ref.id,
                                source_revision_id=ref.source_revision_id,
                                target_revision_id=ref.target_revision_id,
                                relation=ref.relation,
                            )
                        )
                for record in session.scalars(
                    select(LegacyRecord).where(LegacyRecord.work_id == work_id)
                ).yield_per(1):
                    metadata = dict(
                        source_id=record.source_id,
                        table=record.table_name,
                        legacy_id=record.legacy_id,
                        asset_id=record.asset_id,
                        revision_id=record.revision_id,
                        payload_sha256=record.sha256,
                        payload=record.payload,
                        formal_memory=False,
                    )
                    put(
                        zipfile,
                        f"legacy/{record.id}.json",
                        json.dumps(metadata, ensure_ascii=False, indent=2).encode(),
                    )
                zipfile.writestr(
                    "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
                )
            audit(
                session,
                "work.exported",
                work_id,
                work_id,
                files=len(manifest["files"]),
                bytes=byte_count,
                status="DRAFT",
            )
        archive.seek(0)
    except BaseException:
        archive.close()
        raise

    def stream():
        try:
            while chunk := archive.read(65536):
                yield chunk
        finally:
            archive.close()

    return StreamingResponse(
        stream(),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="novelos-draft-{work_id}.zip"'
        },
    )
