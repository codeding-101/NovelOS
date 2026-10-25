"""Opt-in model API profile. Existing executors remain disconnected."""

from alembic import op
import sqlalchemy as sa

revision = "0008_model_api"
down_revision = "0007_agent_gateway"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("executors", sa.Column("api_config", sa.JSON(), nullable=True))


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
