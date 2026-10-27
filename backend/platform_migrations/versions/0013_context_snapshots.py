"""Frozen offline context assembly and explicit capacity decisions."""

from alembic import op
import sqlalchemy as sa

revision = "0013_context_snapshots"
down_revision = "0012_story_states"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "context_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column(
            "input_revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            nullable=False,
        ),
        sa.Column("story_at", sa.BigInteger()),
        sa.Column("capacity_bytes", sa.Integer(), nullable=False),
        sa.Column("required_bytes", sa.Integer(), nullable=False),
        sa.Column("used_bytes", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("blockers", sa.JSON(), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "request_key", name="context_request_key"),
        sa.CheckConstraint("status IN ('READY','BLOCKED')", name="context_status"),
        sa.CheckConstraint(
            "status != 'READY' OR used_bytes <= capacity_bytes",
            name="context_ready_capacity",
        ),
        sa.CheckConstraint(
            "capacity_bytes > 0 AND capacity_bytes <= 8388608 AND required_bytes > 0 AND used_bytes >= required_bytes",
            name="context_capacity",
        ),
    )
    op.create_index("ix_context_snapshots_work_id", "context_snapshots", ["work_id"])
    op.create_table(
        "context_memories",
        sa.Column(
            "snapshot_id",
            sa.String(32),
            sa.ForeignKey("context_snapshots.id"),
            primary_key=True,
        ),
        sa.Column(
            "proposal_id",
            sa.String(32),
            sa.ForeignKey("memory_proposals.id"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column("included", sa.Boolean(), nullable=False),
        sa.Column("omission_reason", sa.String(32)),
        sa.UniqueConstraint("snapshot_id", "position", name="context_memory_position"),
        sa.CheckConstraint(
            "NOT required OR included", name="context_required_included"
        ),
    )
    for table in ("context_snapshots", "context_memories"):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable context record'); END"
                )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
