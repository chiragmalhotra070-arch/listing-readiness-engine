"""documents.normalized_data: the Stage 1 canonical view

Revision ID: 0015_listing_consistency
Revises: 0014_listing_readiness_domain
Create Date: 2026-10-06

Slice 9 of the listing engine (V0): consistency normalization.

* ``documents.normalized_data`` -- canonical forms of the shared fact
  fields (property_address, apn, seller_name, listing_agent_name,
  document_date), written alongside ``extracted_data`` at extraction.
  Property-identity resolution (Stage 2) and the cross-document conflict
  rule (R1) read this column; readers fall back to normalizing
  ``extracted_data`` for rows created before this migration.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0015_listing_consistency"
down_revision = "0014_listing_readiness_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("normalized_data", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("documents", "normalized_data")
