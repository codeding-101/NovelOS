"""Persist immutable owner decisions for summary candidates."""

from alembic import op
import sqlalchemy as sa

revision = "0016_summary_decisions"
down_revision = "0015_summary_candidates"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "summary_decisions",
        sa.Column(
            "summary_id",
            sa.String(32),
            sa.ForeignKey("summary_candidates.id"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('approved','rejected')", name="summary_decision_outcome"
        ),
    )
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER summary_decisions_no_{action.lower()} BEFORE {action} ON summary_decisions BEGIN SELECT RAISE(ABORT, 'Immutable summary decision'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER summary_decisions_immutable BEFORE UPDATE OR DELETE ON summary_decisions FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
