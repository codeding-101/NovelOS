"""Per-task spending ceilings; existing rows require explicit configuration before model calls."""

from alembic import op
import sqlalchemy as sa

revision = "0009_task_budgets"
down_revision = "0008_model_api"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("creative_tasks", sa.Column("budget_currency", sa.String(3)))
    op.add_column(
        "creative_tasks", sa.Column("budget_limit_microunits", sa.BigInteger())
    )
    op.add_column("creative_tasks", sa.Column("budget_limit_tokens", sa.BigInteger()))


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
