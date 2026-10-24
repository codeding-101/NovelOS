"""Work lifetime budgets and immutable accounting events; external calls stay closed."""

from alembic import op
import sqlalchemy as sa

revision = "0005_work_budgets"
down_revision = "0004_task_runtime"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "work_budgets",
        sa.Column(
            "work_id", sa.String(32), sa.ForeignKey("works.id"), primary_key=True
        ),
        sa.Column("currency", sa.String(3), nullable=False),
        *[
            sa.Column(name, sa.BigInteger(), nullable=False)
            for name in (
                "limit_microunits",
                "limit_tokens",
                "spent_microunits",
                "held_microunits",
                "spent_tokens",
                "held_tokens",
            )
        ],
        sa.Column("version", sa.Integer(), nullable=False),
        sa.CheckConstraint("currency IN ('CNY','USD')", name="budget_currency"),
        sa.CheckConstraint(
            "limit_microunits >= 0 AND limit_tokens >= 0 AND spent_microunits >= 0 AND held_microunits >= 0 AND spent_tokens >= 0 AND held_tokens >= 0",
            name="budget_nonnegative",
        ),
    )
    op.create_table(
        "budget_reservations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "work_id",
            sa.String(32),
            sa.ForeignKey("work_budgets.work_id"),
            nullable=False,
        ),
        sa.Column(
            "task_id", sa.String(32), sa.ForeignKey("creative_tasks.id"), nullable=False
        ),
        sa.Column(
            "attempt_id",
            sa.String(32),
            sa.ForeignKey("task_attempts.id"),
            nullable=False,
        ),
        sa.Column(
            "executor_id", sa.String(32), sa.ForeignKey("executors.id"), nullable=False
        ),
        sa.Column("operation_key", sa.String(128), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("reserved_microunits", sa.BigInteger(), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("actual_microunits", sa.BigInteger()),
        sa.Column("input_tokens", sa.BigInteger()),
        sa.Column("output_tokens", sa.BigInteger()),
        sa.Column("receipt_key", sa.String(128)),
        sa.Column("settlement_hash", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "operation_key", name="budget_operation"),
        sa.UniqueConstraint("executor_id", "receipt_key", name="budget_receipt"),
        sa.CheckConstraint(
            "state IN ('RESERVED','DISPATCHED','UNKNOWN','SETTLED','RELEASED')",
            name="reservation_state",
        ),
        sa.CheckConstraint(
            "reserved_microunits >= 0 AND reserved_tokens >= 0",
            name="reservation_nonnegative",
        ),
    )
    for column in ("work_id", "task_id", "attempt_id"):
        op.create_index(
            f"ix_budget_reservations_{column}", "budget_reservations", [column]
        )
    op.create_table(
        "budget_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "work_id",
            sa.String(32),
            sa.ForeignKey("work_budgets.work_id"),
            nullable=False,
        ),
        sa.Column(
            "reservation_id", sa.String(32), sa.ForeignKey("budget_reservations.id")
        ),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("actor", sa.String(32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_budget_events_work_id", "budget_events", ["work_id"])
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER budget_events_no_{action.lower()} BEFORE {action} ON budget_events BEGIN SELECT RAISE(ABORT, 'Immutable budget event'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER budget_events_immutable BEFORE UPDATE OR DELETE ON budget_events FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
