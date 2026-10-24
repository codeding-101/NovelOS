"""Persistent local/manual task runtime. No paid executor is enabled."""

from alembic import op
import sqlalchemy as sa

revision = "0004_task_runtime"
down_revision = "0003_asset_delivery"
branch_labels = None
depends_on = None


def upgrade():
    executors = op.create_table(
        "executors",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
    )
    op.bulk_insert(
        executors,
        [
            {
                "id": "human",
                "name": "人工执行",
                "mode": "human",
                "capabilities": ["analyze", "draft", "revise", "review", "research"],
            },
            {
                "id": "local_test",
                "name": "本地流程校验",
                "mode": "local_test",
                "capabilities": ["analyze"],
            },
        ],
    )
    op.create_table(
        "creative_tasks",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column(
            "executor_id", sa.String(32), sa.ForeignKey("executors.id"), nullable=False
        ),
        sa.Column(
            "input_revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            nullable=False,
        ),
        sa.Column("task_type", sa.String(32), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("request_key", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("active_attempt_id", sa.String(32)),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("work_id", "request_key", name="task_request_key"),
        sa.CheckConstraint(
            "state IN ('QUEUED','RUNNING','RETRY_WAIT','PAUSED','SUCCEEDED','FAILED','CANCELLED')",
            name="task_state",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND max_attempts BETWEEN 1 AND 5",
            name="task_attempt_limits",
        ),
    )
    op.create_index("ix_creative_tasks_work_id", "creative_tasks", ["work_id"])
    op.create_index("ix_creative_tasks_state", "creative_tasks", ["state"])
    op.create_table(
        "task_dependencies",
        sa.Column(
            "task_id",
            sa.String(32),
            sa.ForeignKey("creative_tasks.id"),
            primary_key=True,
        ),
        sa.Column(
            "depends_on_id",
            sa.String(32),
            sa.ForeignKey("creative_tasks.id"),
            primary_key=True,
        ),
    )
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "task_id", sa.String(32), sa.ForeignKey("creative_tasks.id"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("task_id", "number", name="task_attempt_number"),
    )
    op.create_index("ix_task_attempts_task_id", "task_attempts", ["task_id"])
    op.create_table(
        "task_checkpoints",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "task_id", sa.String(32), sa.ForeignKey("creative_tasks.id"), nullable=False
        ),
        sa.Column(
            "attempt_id",
            sa.String(32),
            sa.ForeignKey("task_attempts.id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("step", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("attempt_id", "sequence", name="checkpoint_sequence"),
    )
    op.create_index("ix_task_checkpoints_task_id", "task_checkpoints", ["task_id"])
    op.create_table(
        "task_submissions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "task_id", sa.String(32), sa.ForeignKey("creative_tasks.id"), nullable=False
        ),
        sa.Column(
            "attempt_id",
            sa.String(32),
            sa.ForeignKey("task_attempts.id"),
            nullable=False,
        ),
        sa.Column("submission_key", sa.String(128), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("disposition", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("envelope", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "attempt_id", "submission_key", name="result_submission_key"
        ),
    )
    op.create_index("ix_task_submissions_task_id", "task_submissions", ["task_id"])
    for table in ("task_checkpoints", "task_submissions", "task_dependencies"):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Immutable task record'); END"
                )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
