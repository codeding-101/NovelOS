from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
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


class Executor(PlatformBase):
    __tablename__ = "executors"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    mode: Mapped[str] = mapped_column(String(32))
    capabilities: Mapped[list] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(default=True, server_default="1")
    available: Mapped[bool] = mapped_column(default=True, server_default="1")
    priority: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    currency: Mapped[str | None] = mapped_column(String(3))
    input_rate: Mapped[int | None] = mapped_column(BigInteger)
    output_rate: Mapped[int | None] = mapped_column(BigInteger)
    context_tokens: Mapped[int | None] = mapped_column(BigInteger)
    credential_id: Mapped[str | None] = mapped_column(String(32))
    registration_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    registration_hash: Mapped[str | None] = mapped_column(String(64))


class ExecutionPolicy(PlatformBase):
    __tablename__ = "execution_policies"
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    allowed_ids: Mapped[list] = mapped_column(JSON)
    preferred_ids: Mapped[list] = mapped_column(JSON)
    auto_local_diagnostics: Mapped[bool] = mapped_column(default=False)


class TaskRoutingDecision(PlatformBase):
    __tablename__ = "task_routing_decisions"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creative_tasks.id"), primary_key=True
    )
    requested_executor_id: Mapped[str] = mapped_column(String(32))
    selected_executor_id: Mapped[str] = mapped_column(ForeignKey("executors.id"))
    policy_version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict] = mapped_column(JSON)


class CreativeTask(PlatformBase):
    __tablename__ = "creative_tasks"
    __table_args__ = (
        UniqueConstraint("work_id", "request_key", name="task_request_key"),
        CheckConstraint(
            "state IN ('QUEUED','RUNNING','RETRY_WAIT','PAUSED','SUCCEEDED','FAILED','CANCELLED')",
            name="task_state",
        ),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 5",
            name="task_attempt_limits",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), index=True)
    executor_id: Mapped[str] = mapped_column(ForeignKey("executors.id"))
    input_revision_id: Mapped[str] = mapped_column(ForeignKey("asset_revisions.id"))
    task_type: Mapped[str] = mapped_column(String(32))
    objective: Mapped[str] = mapped_column(Text)
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="QUEUED", index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    active_attempt_id: Mapped[str | None] = mapped_column(String(32))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskDependency(PlatformBase):
    __tablename__ = "task_dependencies"
    task_id: Mapped[str] = mapped_column(
        ForeignKey("creative_tasks.id"), primary_key=True
    )
    depends_on_id: Mapped[str] = mapped_column(
        ForeignKey("creative_tasks.id"), primary_key=True
    )


class TaskAttempt(PlatformBase):
    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("task_id", "number", name="task_attempt_number"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    task_id: Mapped[str] = mapped_column(ForeignKey("creative_tasks.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="RUNNING")
    lease_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskCheckpoint(PlatformBase):
    __tablename__ = "task_checkpoints"
    __table_args__ = (
        UniqueConstraint("attempt_id", "sequence", name="checkpoint_sequence"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    task_id: Mapped[str] = mapped_column(ForeignKey("creative_tasks.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("task_attempts.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    step: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskSubmission(PlatformBase):
    __tablename__ = "task_submissions"
    __table_args__ = (
        UniqueConstraint("attempt_id", "submission_key", name="result_submission_key"),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    task_id: Mapped[str] = mapped_column(ForeignKey("creative_tasks.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("task_attempts.id"))
    submission_key: Mapped[str] = mapped_column(String(128))
    fingerprint: Mapped[str] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(24))
    reason: Mapped[str | None] = mapped_column(String(64))
    envelope: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class WorkBudget(PlatformBase):
    __tablename__ = "work_budgets"
    __table_args__ = (
        CheckConstraint("currency IN ('CNY','USD')", name="budget_currency"),
        CheckConstraint(
            "limit_microunits >= 0 AND limit_tokens >= 0 AND spent_microunits >= 0 AND held_microunits >= 0 AND spent_tokens >= 0 AND held_tokens >= 0",
            name="budget_nonnegative",
        ),
    )
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), primary_key=True)
    currency: Mapped[str] = mapped_column(String(3))
    limit_microunits: Mapped[int] = mapped_column(BigInteger)
    limit_tokens: Mapped[int] = mapped_column(BigInteger)
    spent_microunits: Mapped[int] = mapped_column(BigInteger, default=0)
    held_microunits: Mapped[int] = mapped_column(BigInteger, default=0)
    spent_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    held_tokens: Mapped[int] = mapped_column(BigInteger, default=0)
    version: Mapped[int] = mapped_column(Integer, default=1)


class BudgetReservation(PlatformBase):
    __tablename__ = "budget_reservations"
    __table_args__ = (
        UniqueConstraint("attempt_id", "operation_key", name="budget_operation"),
        UniqueConstraint("executor_id", "receipt_key", name="budget_receipt"),
        CheckConstraint(
            "state IN ('RESERVED','DISPATCHED','UNKNOWN','SETTLED','RELEASED')",
            name="reservation_state",
        ),
        CheckConstraint(
            "reserved_microunits >= 0 AND reserved_tokens >= 0",
            name="reservation_nonnegative",
        ),
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("work_budgets.work_id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("creative_tasks.id"), index=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("task_attempts.id"), index=True)
    executor_id: Mapped[str] = mapped_column(ForeignKey("executors.id"))
    operation_key: Mapped[str] = mapped_column(String(128))
    currency: Mapped[str] = mapped_column(String(3))
    reserved_microunits: Mapped[int] = mapped_column(BigInteger)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(String(16), default="RESERVED")
    version: Mapped[int] = mapped_column(Integer, default=1)
    actual_microunits: Mapped[int | None] = mapped_column(BigInteger)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger)
    receipt_key: Mapped[str | None] = mapped_column(String(128))
    settlement_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class BudgetEvent(PlatformBase):
    __tablename__ = "budget_events"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=identifier)
    work_id: Mapped[str] = mapped_column(ForeignKey("work_budgets.work_id"), index=True)
    reservation_id: Mapped[str | None] = mapped_column(
        ForeignKey("budget_reservations.id")
    )
    kind: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
