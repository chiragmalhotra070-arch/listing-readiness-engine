"""add atomic business identity ownership"""

from alembic import op
import sqlalchemy as sa


revision = "0005_business_identity_ownership"
down_revision = "0004_file_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "business_identity_ownership",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False),
        sa.Column("business_identity", sa.String(1000), nullable=False),
        sa.Column("owner_document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_type", "customer_id", "business_identity", name="uq_business_identity_ownership_identity"),
    )
    op.create_index("ix_business_identity_ownership_customer_id", "business_identity_ownership", ["customer_id"])
    op.create_index("ix_business_identity_ownership_owner_document_id", "business_identity_ownership", ["owner_document_id"])


def downgrade() -> None:
    op.drop_index("ix_business_identity_ownership_owner_document_id", table_name="business_identity_ownership")
    op.drop_index("ix_business_identity_ownership_customer_id", table_name="business_identity_ownership")
    op.drop_table("business_identity_ownership")
