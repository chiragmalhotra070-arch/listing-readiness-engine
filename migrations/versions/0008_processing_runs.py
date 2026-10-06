"""add processing_runs

Revision ID: 0008_processing_runs
Revises: 0007_merge_heads
Create Date: 2026-09-30

One row = one complete processing execution journey for exactly one document.

Trigger taxonomy (see TARGET_SCHEMA_MIGRATION_PLAN.md):

    INTAKE        first execution of a freshly ingested document
    AUTO_RETRY    automatic re-execution (work-item claim after RETRY_SCHEDULED,
                  sync inline retry loop, expired-lease reclaim)
    MANUAL_RETRY  operator POST /documents/{id}/retry
    REPROCESS     operator POST /documents/{id}/reprocess
    CRM_ACTION    POST /documents/{id}/process (CRM-only execution)
    LEGACY        backfill-only: reconstructed from history
    UNKNOWN       backfill-only: evidence did not identify the trigger

``extracted_text`` holds the full parser/OCR text produced by this run (Q4).
Historical text that was never persisted is not fabricated by the backfill.
"""

from alembic import op
import sqlalchemy as sa


revision = "0008_processing_runs"
down_revision = "0007_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "processing_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("trigger", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(64)),
        sa.Column("worker_id", sa.String(255)),
        sa.Column("work_item_id", sa.Integer(), sa.ForeignKey("document_work_items.id")),
        sa.Column("extracted_text", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_processing_runs_document_id", "processing_runs", ["document_id"])
    op.create_index("ix_processing_runs_work_item_id", "processing_runs", ["work_item_id"])
    op.create_index("ix_processing_runs_document_started", "processing_runs", ["document_id", "started_at"])


def downgrade() -> None:
    op.drop_index("ix_processing_runs_document_started", table_name="processing_runs")
    op.drop_index("ix_processing_runs_work_item_id", table_name="processing_runs")
    op.drop_index("ix_processing_runs_document_id", table_name="processing_runs")
    op.drop_table("processing_runs")
