"""Explicit story-time intervals attached to immutable reviewed proposals."""

from alembic import op
import sqlalchemy as sa

revision = "0012_story_states"
down_revision = "0011_memory_review"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "memory_story_states",
        sa.Column(
            "proposal_id",
            sa.String(32),
            sa.ForeignKey("memory_proposals.id"),
            primary_key=True,
        ),
        sa.Column("subject_key", sa.String(128), nullable=False),
        sa.Column("predicate", sa.String(128), nullable=False),
        sa.Column("value", sa.String(1024), nullable=False),
        sa.Column("valid_from", sa.BigInteger(), nullable=False),
        sa.Column("valid_until", sa.BigInteger()),
        sa.CheckConstraint(
            "valid_from >= 0 AND valid_from <= 1000000000000 AND "
            "(valid_until IS NULL OR (valid_until > valid_from AND valid_until <= 1000000000000))",
            name="story_state_interval",
        ),
    )
    op.create_index(
        "ix_memory_story_states_subject_key", "memory_story_states", ["subject_key"]
    )
    if op.get_bind().dialect.name == "sqlite":
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER memory_story_states_no_{action.lower()} BEFORE {action} ON memory_story_states BEGIN SELECT RAISE(ABORT, 'Immutable story state'); END"
            )
    else:
        op.execute(
            "CREATE TRIGGER memory_story_states_immutable BEFORE UPDATE OR DELETE ON memory_story_states FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
        )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
