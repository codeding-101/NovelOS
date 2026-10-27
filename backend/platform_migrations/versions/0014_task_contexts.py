"""Immutable task to context binding; legacy tasks remain unbound."""

from alembic import op
import sqlalchemy as sa

revision = "0014_task_contexts"
down_revision = "0013_context_snapshots"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_contexts",
        sa.Column(
            "task_id",
            sa.String(32),
            sa.ForeignKey("creative_tasks.id"),
            primary_key=True,
        ),
        sa.Column(
            "snapshot_id",
            sa.String(32),
            sa.ForeignKey("context_snapshots.id"),
            nullable=False,
        ),
    )
    op.create_index("ix_task_contexts_snapshot_id", "task_contexts", ["snapshot_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER task_contexts_no_{action.lower()} BEFORE {action} ON task_contexts BEGIN SELECT RAISE(ABORT, 'Immutable task context'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER task_contexts_immutable BEFORE UPDATE OR DELETE ON task_contexts FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
