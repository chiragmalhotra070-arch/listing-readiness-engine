"""add durable document work queue"""

from alembic import op
import sqlalchemy as sa


revision = "0006_document_work_items"
down_revision = "0005_business_identity_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "document_work_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("run_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(128)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "run_id", name="uq_document_work_items_document_run"),
        sa.CheckConstraint("status IN ('PENDING', 'CLAIMED', 'RETRY_SCHEDULED', 'COMPLETED', 'FAILED')", name="ck_document_work_items_status"),
    )
    op.create_index("ix_document_work_items_document_id", "document_work_items", ["document_id"])
    op.create_index("ix_document_work_items_available_at", "document_work_items", ["available_at"])
    op.create_index("ix_document_work_items_lease_expires_at", "document_work_items", ["lease_expires_at"])
    op.create_index("ix_document_work_items_pending_available", "document_work_items", ["status", "available_at"])


def downgrade() -> None:
    op.drop_index("ix_document_work_items_pending_available", table_name="document_work_items")
    op.drop_index("ix_document_work_items_lease_expires_at", table_name="document_work_items")
    op.drop_index("ix_document_work_items_available_at", table_name="document_work_items")
    op.drop_index("ix_document_work_items_document_id", table_name="document_work_items")
    op.drop_table("document_work_items")
