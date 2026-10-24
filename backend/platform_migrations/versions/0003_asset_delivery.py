"""Attachments, durable projections, revision references and read-only import mapping."""

from alembic import op
import sqlalchemy as sa

revision = "0003_asset_delivery"
down_revision = "0002_work_assets"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "attachments",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("asset_id", sa.String(32), sa.ForeignKey("assets.id")),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_attachments_work_id", "attachments", ["work_id"])
    op.create_table(
        "projection_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("token", sa.String(32)),
        sa.Column("error_code", sa.String(64)),
        sa.UniqueConstraint("kind", "target_id", name="projection_target"),
        sa.CheckConstraint(
            "kind IN ('revision','attachment','original')", name="projection_kind"
        ),
        sa.CheckConstraint(
            "state IN ('pending','running','failed','done')", name="projection_state"
        ),
        sa.CheckConstraint("attempts >= 0", name="projection_attempts"),
    )
    op.create_index("ix_projection_jobs_work_id", "projection_jobs", ["work_id"])
    op.create_index("ix_projection_jobs_state", "projection_jobs", ["state"])
    op.create_table(
        "search_documents",
        sa.Column(
            "revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            primary_key=True,
        ),
        sa.Column("text", sa.Text(), nullable=False),
    )
    op.create_table(
        "asset_references",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column(
            "source_revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            nullable=False,
        ),
        sa.Column(
            "target_revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            nullable=False,
        ),
        sa.Column("relation", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_revision_id",
            "target_revision_id",
            "relation",
            name="reference_edge",
        ),
        sa.CheckConstraint(
            "relation IN ('reference','depends_on')", name="reference_relation"
        ),
        sa.CheckConstraint(
            "source_revision_id <> target_revision_id", name="reference_not_self"
        ),
    )
    op.create_index("ix_asset_references_work_id", "asset_references", ["work_id"])
    op.create_table(
        "legacy_imports",
        sa.Column("source_id", sa.String(128), primary_key=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "legacy_records",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(128),
            sa.ForeignKey("legacy_imports.source_id"),
            nullable=False,
        ),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("legacy_id", sa.String(128), nullable=False),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("asset_id", sa.String(32), sa.ForeignKey("assets.id")),
        sa.Column("revision_id", sa.String(32), sa.ForeignKey("asset_revisions.id")),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint(
            "source_id", "table_name", "legacy_id", name="legacy_record_identity"
        ),
    )
    op.create_index("ix_legacy_records_work_id", "legacy_records", ["work_id"])
    # Seed projections for versions and originals created before this migration.
    op.execute("""INSERT INTO projection_jobs (id,work_id,kind,target_id,state,attempts,available_at)
        SELECT r.id,a.work_id,'revision',r.id,'pending',0,CURRENT_TIMESTAMP
        FROM asset_revisions r JOIN assets a ON a.id=r.asset_id""")
    op.execute("""INSERT INTO projection_jobs (id,work_id,kind,target_id,state,attempts,available_at)
        SELECT o.id,a.work_id,'original',o.id,'pending',0,CURRENT_TIMESTAMP
        FROM original_uploads o JOIN asset_revisions r ON r.id=o.revision_id
        JOIN assets a ON a.id=r.asset_id""")
    for table in (
        "attachments",
        "legacy_imports",
        "legacy_records",
        "asset_references",
    ):
        if op.get_bind().dialect.name == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Immutable asset record'); END"
                )
        else:
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
