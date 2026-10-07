"""listing_files.idempotency_key + documents.processing_failures

Revision ID: 0016_listing_idempotency_key
Revises: 0015_listing_consistency
Create Date: 2026-10-07

Retry & idempotency hardening:

* Item 1: ``POST /listing-files`` accepts an ``idempotency_key`` so a
  replayed logical file returns the existing row instead of inserting a
  duplicate.  The partial unique index (non-null keys only) closes the
  check-then-insert race; keyless creates are unaffected.
* Item 4: ``documents.processing_failures`` holds the orchestrator's
  per-stage retry-exhaustion reports that feed the review queue's
  ``processing_failed`` bucket.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0016_listing_idempotency_key"
down_revision = "0015_listing_consistency"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "listing_files",
        sa.Column("idempotency_key", sa.String(255), nullable=True),
    )
    op.create_index(
        "uq_listing_files_idempotency_key",
        "listing_files",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )
    op.add_column(
        "documents",
        sa.Column("processing_failures", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "processing_failures")
    op.drop_index("uq_listing_files_idempotency_key", table_name="listing_files")
    op.drop_column("listing_files", "idempotency_key")
