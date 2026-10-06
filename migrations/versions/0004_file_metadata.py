"""add durable attachment metadata"""

from alembic import op
import sqlalchemy as sa


revision = "0004_file_metadata"
down_revision = "0003_integration_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("documents", sa.Column("source_attachment_id", sa.String(255)))
    op.add_column("documents", sa.Column("file_size_bytes", sa.Integer()))
    op.create_index("ix_documents_source_attachment_id", "documents", ["source_attachment_id"])


def downgrade() -> None:
    op.drop_index("ix_documents_source_attachment_id", table_name="documents")
    op.drop_column("documents", "file_size_bytes")
    op.drop_column("documents", "source_attachment_id")

