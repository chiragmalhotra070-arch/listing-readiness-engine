"""add extracted_fields

Revision ID: 0010_extracted_fields
Revises: 0009_processing_attempts_run_id
Create Date: 2026-09-30

Immutable per-field extraction observations attached to the attempt that
produced them (Q3).  ``document_field_values`` is deliberately NOT created:
current accepted values already live on ``documents``.

Immutability is enforced two ways:
  * application convention (rows are only ever inserted),
  * a PostgreSQL trigger that rejects UPDATE and DELETE on the table.

``field_value`` is JSONB on PostgreSQL and JSON elsewhere so that the SQLite
based unit-test harness (``Base.metadata.create_all``) keeps working.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0010_extracted_fields"
down_revision = "0009_processing_attempts_run_id"
branch_labels = None
depends_on = None


IMMUTABILITY_FUNCTION = """
CREATE OR REPLACE FUNCTION forbid_extracted_fields_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'extracted_fields rows are immutable (attempt_id=%)', OLD.id;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        "extracted_fields",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("attempt_id", sa.Integer(), sa.ForeignKey("processing_attempts.id"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("processing_runs.id"), nullable=False),
        sa.Column("field_name", sa.String(128), nullable=False),
        sa.Column("field_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("value_type", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_extracted_fields_attempt_id", "extracted_fields", ["attempt_id"])
    op.create_index("ix_extracted_fields_document_id", "extracted_fields", ["document_id"])
    op.create_index("ix_extracted_fields_run_id", "extracted_fields", ["run_id"])
    op.create_index("ix_extracted_fields_document_field", "extracted_fields", ["document_id", "field_name"])
    op.create_index("ix_extracted_fields_source", "extracted_fields", ["source"])

    op.execute(IMMUTABILITY_FUNCTION)
    op.execute(
        "CREATE TRIGGER trg_extracted_fields_immutable "
        "BEFORE UPDATE OR DELETE ON extracted_fields "
        "FOR EACH ROW EXECUTE FUNCTION forbid_extracted_fields_mutation()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_extracted_fields_immutable ON extracted_fields")
    op.execute("DROP FUNCTION IF EXISTS forbid_extracted_fields_mutation()")
    op.drop_index("ix_extracted_fields_source", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_document_field", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_run_id", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_document_id", table_name="extracted_fields")
    op.drop_index("ix_extracted_fields_attempt_id", table_name="extracted_fields")
    op.drop_table("extracted_fields")
