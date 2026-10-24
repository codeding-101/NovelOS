from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


def identifier() -> str:
    return uuid4().hex


class PlatformBase(DeclarativeBase):
    pass


class Owner(PlatformBase):
    __tablename__ = "owner_accounts"
    __table_args__ = (CheckConstraint("id = 'owner'", name="single_owner"),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="owner")
    username: Mapped[str] = mapped_column(String(128), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BrowserSession(PlatformBase):
    __tablename__ = "browser_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("owner_accounts.id"))
    csrf_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class LoginLimit(PlatformBase):
    __tablename__ = "login_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderCredential(PlatformBase):
    __tablename__ = "provider_credentials"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    provider_type: Mapped[str] = mapped_column(String(64))
    ciphertext: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AgentCredential(PlatformBase):
    __tablename__ = "agent_credentials"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    name: Mapped[str] = mapped_column(String(128))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    scopes: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(PlatformBase):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    actor: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(128))
    target_id: Mapped[str | None] = mapped_column(String(128))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, index=True
    )


class WorkerHeartbeat(PlatformBase):
    __tablename__ = "worker_heartbeats"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    state: Mapped[str] = mapped_column(String(32), default="idle")
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Work(PlatformBase):
    __tablename__ = "works"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    title: Mapped[str] = mapped_column(String(256))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Asset(PlatformBase):
    __tablename__ = "assets"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('volume','chapter','scene','material')", name="asset_kind"
        ),
        CheckConstraint("head_version >= 1", name="asset_head_positive"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    kind: Mapped[str] = mapped_column(String(32))
    head_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AssetRevision(PlatformBase):
    __tablename__ = "asset_revisions"
    __table_args__ = (
        UniqueConstraint("asset_id", "number", name="asset_revision_number"),
        CheckConstraint("number >= 1", name="revision_number_positive"),
        CheckConstraint("status = 'DRAFT'", name="revision_draft_only"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    asset_id: Mapped[str] = mapped_column(ForeignKey("assets.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(256))
    content: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    restored_from: Mapped[str | None] = mapped_column(ForeignKey("asset_revisions.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class OriginalUpload(PlatformBase):
    """Exact input bytes retained independently of editable text revisions."""

    __tablename__ = "original_uploads"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("asset_revisions.id"), unique=True
    )
    filename: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Attachment(PlatformBase):
    __tablename__ = "attachments"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    filename: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(Integer)
    data: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ProjectionJob(PlatformBase):
    __tablename__ = "projection_jobs"
    __table_args__ = (
        UniqueConstraint("kind", "target_id", name="projection_target"),
        CheckConstraint(
            "kind IN ('revision','attachment','original')", name="projection_kind"
        ),
        CheckConstraint(
            "state IN ('pending','running','failed','done')", name="projection_state"
        ),
        CheckConstraint("attempts >= 0", name="projection_attempts"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    token: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(64))


class SearchDocument(PlatformBase):
    __tablename__ = "search_documents"
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("asset_revisions.id"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)


class AssetReference(PlatformBase):
    __tablename__ = "asset_references"
    __table_args__ = (
        UniqueConstraint(
            "source_revision_id",
            "target_revision_id",
            "relation",
            name="reference_edge",
        ),
        CheckConstraint(
            "relation IN ('reference','depends_on')", name="reference_relation"
        ),
        CheckConstraint(
            "source_revision_id <> target_revision_id", name="reference_not_self"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    source_revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id"))
    target_revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id"))
    relation: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LegacyImport(PlatformBase):
    __tablename__ = "legacy_imports"
    source_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class LegacyRecord(PlatformBase):
    __tablename__ = "legacy_records"
    __table_args__ = (
        UniqueConstraint(
            "source_id", "table_name", "legacy_id", name="legacy_record_identity"
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    source_id: Mapped[str] = mapped_column(ForeignKey("legacy_imports.source_id"))
    table_name: Mapped[str] = mapped_column(String(64))
    legacy_id: Mapped[str] = mapped_column(String(128))
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("asset_revisions.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
