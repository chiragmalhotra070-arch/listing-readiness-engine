"""requirement_rules.owner

Revision ID: 0018_requirement_owner
Revises: 0017_customer_aware_intake
Create Date: 2026-10-08

Catalog ownership:

* ``owner`` (seller / agent / third_party) is a catalog attribute; the
  requirement engine copies it onto generated ``Requirement`` rows, which
  the generate/readout responses expose.  Purely additive -- existing rule
  rows stay NULL and get backfilled by ``ensure_ca_catalog`` on first use.
"""

from alembic import op
import sqlalchemy as sa

revision = "0018_requirement_owner"
down_revision = "0017_customer_aware_intake"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("requirement_rules") as batch:
        batch.add_column(sa.Column("owner", sa.String(32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("requirement_rules") as batch:
        batch.drop_column("owner")
