"""Task-scoped offline Agent grants; no paid or provider API execution."""

from alembic import op
import sqlalchemy as sa

revision = "0007_agent_gateway"
down_revision = "0006_executor_routing"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("executors", sa.Column("agent_protocol", sa.String(32)))
    op.add_column("task_attempts", sa.Column("agent_credential_id", sa.String(32)))
    op.create_table(
        "agent_task_grants",
        sa.Column(
            "credential_id",
            sa.String(32),
            sa.ForeignKey("agent_credentials.id"),
            primary_key=True,
        ),
        sa.Column(
            "task_id", sa.String(32), sa.ForeignKey("creative_tasks.id"), nullable=False
        ),
        sa.Column(
            "executor_id", sa.String(32), sa.ForeignKey("executors.id"), nullable=False
        ),
    )
    op.create_index("ix_agent_task_grants_task_id", "agent_task_grants", ["task_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER agent_grants_no_{action.lower()} BEFORE {action} ON agent_task_grants BEGIN SELECT RAISE(ABORT, 'Immutable Agent scope'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER agent_grants_immutable BEFORE UPDATE OR DELETE ON agent_task_grants FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
