"""Immutable memory proposals and owner-reviewed canonical heads."""

from alembic import op
import sqlalchemy as sa

revision = "0011_memory_review"
down_revision = "0010_monthly_quotas"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_proposals",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("entry_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "base_proposal_id", sa.String(32), sa.ForeignKey("memory_proposals.id")
        ),
        sa.Column("submission_id", sa.String(32), sa.ForeignKey("task_submissions.id")),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "request_key", name="memory_request_key"),
        sa.CheckConstraint(
            "kind IN ('character','relationship','world_rule','event','foreshadow','promise')",
            name="memory_kind",
        ),
    )
    op.create_index("ix_memory_proposals_work_id", "memory_proposals", ["work_id"])
    op.create_table(
        "memory_sources",
        sa.Column(
            "proposal_id",
            sa.String(32),
            sa.ForeignKey("memory_proposals.id"),
            primary_key=True,
        ),
        sa.Column(
            "revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            primary_key=True,
        ),
    )
    op.create_table(
        "memory_decisions",
        sa.Column(
            "proposal_id",
            sa.String(32),
            sa.ForeignKey("memory_proposals.id"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('approved','rejected')", name="memory_decision_outcome"
        ),
    )
    op.create_table(
        "memory_entries",
        sa.Column(
            "work_id", sa.String(32), sa.ForeignKey("works.id"), primary_key=True
        ),
        sa.Column("entry_key", sa.String(128), primary_key=True),
        sa.Column(
            "proposal_id",
            sa.String(32),
            sa.ForeignKey("memory_proposals.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("version", sa.Integer(), nullable=False),
    )
    for table in ("memory_proposals", "memory_sources", "memory_decisions"):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable memory record'); END"
                )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
