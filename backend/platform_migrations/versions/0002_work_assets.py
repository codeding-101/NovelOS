"""Work assets, immutable draft revisions and exact original uploads."""

from alembic import op
import sqlalchemy as sa

revision = "0002_work_assets"
down_revision = "0001_control_foundation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "works",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "assets",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("work_id", sa.String(32), sa.ForeignKey("works.id"), nullable=False),
        sa.Column("parent_id", sa.String(32), sa.ForeignKey("assets.id")),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("head_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('volume','chapter','scene','material')", name="asset_kind"
        ),
        sa.CheckConstraint("head_version >= 1", name="asset_head_positive"),
    )
    op.create_index("ix_assets_work_id", "assets", ["work_id"])
    op.create_table(
        "asset_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "asset_id", sa.String(32), sa.ForeignKey("assets.id"), nullable=False
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("restored_from", sa.String(32), sa.ForeignKey("asset_revisions.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("asset_id", "number", name="asset_revision_number"),
        sa.CheckConstraint("number >= 1", name="revision_number_positive"),
        sa.CheckConstraint("status = 'DRAFT'", name="revision_draft_only"),
    )
    op.create_index("ix_asset_revisions_asset_id", "asset_revisions", ["asset_id"])
    op.create_table(
        "original_uploads",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(32),
            sa.ForeignKey("asset_revisions.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("filename", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
    )
    # Application mistakes cannot silently overwrite historical text or original files.
    if op.get_bind().dialect.name == "sqlite":
        for table in ("asset_revisions", "original_uploads"):
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER {table}_no_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'Immutable asset record'); END"
                )
    else:
        op.execute("""CREATE FUNCTION reject_asset_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'Immutable asset record'; END;
            $$ LANGUAGE plpgsql""")
        for table in ("asset_revisions", "original_uploads"):
            op.execute(
                f"CREATE TRIGGER {table}_immutable BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION reject_asset_mutation()"
            )


def downgrade():
    raise RuntimeError("Destructive downgrade is disabled; restore a verified backup")
