"""Shared calendar-month token quotas; no changes to historical usage."""

from alembic import op
import sqlalchemy as sa

revision = "0010_monthly_quotas"
down_revision = "0009_task_budgets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "monthly_quotas",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("timezone", sa.String(32), nullable=False),
        sa.Column("limit_tokens", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False, unique=True),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "limit_tokens >= 0 AND limit_tokens <= 1000000000000", name="quota_limit"
        ),
        sa.CheckConstraint(
            "timezone IN ('UTC','Asia/Shanghai')", name="quota_timezone"
        ),
    )
    op.create_table(
        "quota_bindings",
        sa.Column(
            "executor_id",
            sa.String(32),
            sa.ForeignKey("executors.id"),
            primary_key=True,
        ),
        sa.Column(
            "quota_id",
            sa.String(32),
            sa.ForeignKey("monthly_quotas.id"),
            nullable=False,
        ),
    )
    op.create_table(
        "quota_bookings",
        sa.Column(
            "reservation_id",
            sa.String(32),
            sa.ForeignKey("budget_reservations.id"),
            primary_key=True,
        ),
        sa.Column(
            "quota_id",
            sa.String(32),
            sa.ForeignKey("monthly_quotas.id"),
            nullable=False,
        ),
        sa.Column("period", sa.String(7), nullable=False),
    )
    op.create_index("ix_quota_bindings_quota_id", "quota_bindings", ["quota_id"])
    op.create_index("ix_quota_bookings_quota_id", "quota_bookings", ["quota_id"])
    op.create_index("ix_quota_bookings_period", "quota_bookings", ["period"])
    for table in ("quota_bindings", "quota_bookings"):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, 'Immutable quota attribution'); END"
                )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
