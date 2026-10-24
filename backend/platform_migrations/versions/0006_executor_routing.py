"""Executor declarations and work routing policies. No external adapter is enabled."""

from alembic import op
import sqlalchemy as sa

revision = "0006_executor_routing"
down_revision = "0005_work_budgets"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("enabled", "available"):
        op.add_column(
            "executors",
            sa.Column(name, sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    op.add_column(
        "executors",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "executors",
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column("executors", sa.Column("currency", sa.String(3)))
    for name in ("input_rate", "output_rate", "context_tokens"):
        op.add_column("executors", sa.Column(name, sa.BigInteger()))
    # A deleted credential leaves the declaration readable, but routing rejects it.
    op.add_column("executors", sa.Column("credential_id", sa.String(32)))
    op.add_column("executors", sa.Column("registration_key", sa.String(128)))
    op.add_column("executors", sa.Column("registration_hash", sa.String(64)))
    op.create_index(
        "ix_executor_registration_key", "executors", ["registration_key"], unique=True
    )
    op.create_table(
        "execution_policies",
        sa.Column(
            "work_id", sa.String(32), sa.ForeignKey("works.id"), primary_key=True
        ),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("allowed_ids", sa.JSON(), nullable=False),
        sa.Column("preferred_ids", sa.JSON(), nullable=False),
        sa.Column("auto_local_diagnostics", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "task_routing_decisions",
        sa.Column(
            "task_id",
            sa.String(32),
            sa.ForeignKey("creative_tasks.id"),
            primary_key=True,
        ),
        sa.Column("requested_executor_id", sa.String(32), nullable=False),
        sa.Column(
            "selected_executor_id",
            sa.String(32),
            sa.ForeignKey("executors.id"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
    )
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER routing_no_{action.lower()} BEFORE {action} ON task_routing_decisions BEGIN SELECT RAISE(ABORT, 'Immutable routing decision'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER routing_immutable BEFORE UPDATE OR DELETE ON task_routing_decisions FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
