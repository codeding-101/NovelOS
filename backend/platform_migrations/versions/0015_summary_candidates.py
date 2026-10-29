"""Immutable source-bound hierarchical summary candidates."""

from alembic import op
import sqlalchemy as sa

revision = "0015_summary_candidates"
down_revision = "0014_task_contexts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "summary_candidates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("document", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "request_key", name="summary_request_key"),
    )
    op.create_index("ix_summary_candidates_work_id", "summary_candidates", ["work_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER summary_candidates_no_{action.lower()} BEFORE {action} ON summary_candidates BEGIN SELECT RAISE(ABORT, 'Immutable summary'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER summary_candidates_immutable BEFORE UPDATE OR DELETE ON summary_candidates FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
