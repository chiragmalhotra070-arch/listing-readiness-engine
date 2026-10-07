"""listing_files.customer_id + customer_resolution

Revision ID: 0017_customer_aware_intake
Revises: 0016_listing_idempotency_key
Create Date: 2026-10-08

Customer-aware intake:

* ``POST /listing-files`` resolves the seller against the customer master
  at create time.  ``customer_id`` is the nullable FK to the resolved
  customer (never set when resolution is ambiguous or no seller was
  supplied); ``customer_resolution`` records how it resolved
  (EXISTING_CUSTOMER / NEW_CUSTOMER / NEEDS_REVIEW).  Purely additive --
  existing rows stay (NULL, NULL).
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_customer_aware_intake"
down_revision = "0016_listing_idempotency_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("listing_files") as batch:
        batch.add_column(
            sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=True),
        )
        batch.add_column(
            sa.Column("customer_resolution", sa.String(32), nullable=True),
        )
        batch.create_index("ix_listing_files_customer_id", ["customer_id"])


def downgrade() -> None:
    with op.batch_alter_table("listing_files") as batch:
        batch.drop_index("ix_listing_files_customer_id")
        batch.drop_column("customer_resolution")
        batch.drop_column("customer_id")
