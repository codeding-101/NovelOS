"""Transactional outbox for disposable file copies and literal-text search indexes."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Protocol

from sqlalchemy import and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.platform.models import (
    Asset,
    AssetRevision,
    Attachment,
    OriginalUpload,
    ProjectionJob,
    SearchDocument,
    identifier,
    now,
)

MAX_ATTEMPTS = 5
LEASE_SECONDS = 60


class FileStore(Protocol):
    def put(self, sha256: str, data: bytes) -> None: ...


class LocalFileStore:
    """Generated content hashes only; never interpret an uploaded filename as a path."""

    def __init__(self, root: Path):
        self.root = root.resolve()

    def path(self, sha256: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Invalid content hash")
        path = self.root / sha256[:2] / sha256
        if not path.resolve().is_relative_to(self.root):
            raise ValueError("Storage path escapes root")
        return path

    def put(self, sha256: str, data: bytes) -> None:
        if hashlib.sha256(data).hexdigest() != sha256:
            raise ValueError("Source integrity mismatch")
        path = self.path(sha256)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path(sha256)  # Recheck after directory creation.
        if path.exists() and path.stat().st_size == len(data):
            with path.open("rb") as source:
                if hashlib.file_digest(source, "sha256").hexdigest() == sha256:
                    return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=path.parent, prefix=".novelos-", delete=False
            ) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path(sha256))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def insert_for(session, model):
    return (pg_insert if session.bind.dialect.name == "postgresql" else sqlite_insert)(
        model
    )


def enqueue(session, work_id: str, kind: str, target_id: str):
    session.add(ProjectionJob(work_id=work_id, kind=kind, target_id=target_id))


def rebuild(session, work_id: str) -> int:
    """Reset derived jobs, fencing any old claim; never delete authoritative content."""
    count = 0
    queries = [
        (
            "revision",
            select(AssetRevision.id)
            .join(Asset, Asset.id == AssetRevision.asset_id)
            .where(Asset.work_id == work_id),
        ),
        (
            "original",
            select(OriginalUpload.id)
            .join(AssetRevision, AssetRevision.id == OriginalUpload.revision_id)
            .join(Asset, Asset.id == AssetRevision.asset_id)
            .where(Asset.work_id == work_id),
        ),
        ("attachment", select(Attachment.id).where(Attachment.work_id == work_id)),
    ]
    for kind, query in queries:
        for target in session.scalars(query):
            statement = insert_for(session, ProjectionJob).values(
                id=identifier(),
                work_id=work_id,
                kind=kind,
                target_id=target,
                state="pending",
                attempts=0,
                available_at=now(),
            )
            session.execute(
                statement.on_conflict_do_update(
                    index_elements=["kind", "target_id"],
                    set_={
                        "state": "pending",
                        "attempts": 0,
                        "available_at": now(),
                        "lease_until": None,
                        "token": None,
                        "error_code": None,
                    },
                )
            )
            count += 1
    return count


def claim(db):
    timestamp = now()
    ready = or_(
        and_(ProjectionJob.state == "pending", ProjectionJob.available_at <= timestamp),
        and_(ProjectionJob.state == "running", ProjectionJob.lease_until <= timestamp),
    )
    with db.sessions.begin() as session:
        session.execute(
            update(ProjectionJob)
            .where(ready, ProjectionJob.attempts >= MAX_ATTEMPTS)
            .values(
                state="failed",
                token=None,
                lease_until=None,
                error_code="ATTEMPTS_EXHAUSTED",
            )
        )
        job_id = session.scalar(
            select(ProjectionJob.id)
            .where(ready, ProjectionJob.attempts < MAX_ATTEMPTS)
            .order_by(ProjectionJob.available_at, ProjectionJob.id)
            .limit(1)
        )
        if job_id is None:
            return None
        token = identifier()
        claimed = session.execute(
            update(ProjectionJob)
            .where(
                ProjectionJob.id == job_id, ready, ProjectionJob.attempts < MAX_ATTEMPTS
            )
            .values(
                state="running",
                token=token,
                attempts=ProjectionJob.attempts + 1,
                lease_until=timestamp + timedelta(seconds=LEASE_SECONDS),
            )
        )
        if claimed.rowcount != 1:
            return None
        job = session.get(ProjectionJob, job_id)
        return dict(
            id=job.id,
            token=token,
            kind=job.kind,
            target_id=job.target_id,
            attempts=job.attempts,
        )


def deliver(db, store: FileStore, job: dict):
    try:
        with db.sessions() as session:
            model = {
                "revision": AssetRevision,
                "original": OriginalUpload,
                "attachment": Attachment,
            }[job["kind"]]
            record = session.get(model, job["target_id"])
            if record is None:
                raise ValueError("Projection source missing")
            data = (
                record.content.encode("utf-8")
                if job["kind"] == "revision"
                else record.data
            )
            sha256 = record.sha256
            text = (
                (record.title + "\n" + record.content).casefold()
                if job["kind"] == "revision"
                else None
            )
        store.put(sha256, data)
        with db.sessions.begin() as session:
            owned = session.execute(
                update(ProjectionJob)
                .where(
                    ProjectionJob.id == job["id"],
                    ProjectionJob.token == job["token"],
                    ProjectionJob.state == "running",
                    ProjectionJob.lease_until > now(),
                )
                .values(state="done", token=None, lease_until=None, error_code=None)
            )
            if owned.rowcount != 1:
                return False
            if text is not None:
                statement = insert_for(session, SearchDocument).values(
                    revision_id=job["target_id"], text=text
                )
                session.execute(
                    statement.on_conflict_do_update(
                        index_elements=["revision_id"], set_={"text": text}
                    )
                )
        return True
    except Exception as exc:
        # Persist safe categories only; storage/database exceptions can contain private paths or text.
        code = (
            "SOURCE_INTEGRITY" if isinstance(exc, ValueError) else "PROJECTION_FAILED"
        )
        with db.sessions.begin() as session:
            session.execute(
                update(ProjectionJob)
                .where(
                    ProjectionJob.id == job["id"],
                    ProjectionJob.token == job["token"],
                    ProjectionJob.state == "running",
                )
                .values(
                    state="failed" if job["attempts"] >= MAX_ATTEMPTS else "pending",
                    available_at=now()
                    + timedelta(seconds=min(300, 2 ** job["attempts"])),
                    token=None,
                    lease_until=None,
                    error_code=code,
                )
            )
        return False


def process_batch(db, store: FileStore, limit: int = 20) -> int:
    processed = 0
    for _ in range(limit):
        job = claim(db)
        if job is None:
            break
        deliver(db, store, job)
        processed += 1
    return processed
