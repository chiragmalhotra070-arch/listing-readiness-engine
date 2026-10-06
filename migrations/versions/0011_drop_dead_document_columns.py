"""remove dead document columns (Q2)

Revision ID: 0011_drop_dead_document_columns
Revises: 0010_extracted_fields
Create Date: 2026-09-30

DESTRUCTIVE.  Requires review before it is run against a live database.

    documents.total_amount   numeric(18,4)   - 0 writers, 0 non-null rows live
    documents.currency       varchar(3)      - 0 writers, 0 non-null rows live
    documents.document_date  date            - 0 writers, 0 non-null rows live

The values these columns were meant to hold already live inside
``documents.extracted_data`` (JSON) and in ``processing_attempts.result``.
Nothing is lost.

Guarded: refuses to drop a column that still carries data, so an unexpected
population cannot be destroyed silently.
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_drop_dead_document_columns"
down_revision = "0010_extracted_fields"
branch_labels = None
depends_on = None

DEAD_COLUMNS = ("total_amount", "currency", "document_date")


def upgrade() -> None:
    bind = op.get_bind()
    for column in DEAD_COLUMNS:
        populated = bind.execute(
            sa.text(f"SELECT count(*) FROM documents WHERE {column} IS NOT NULL")
        ).scalar()
        if populated:
            raise RuntimeError(
                f"refusing to drop documents.{column}: {populated} non-null rows exist. "
                "Migrate those values first (see TARGET_SCHEMA_MIGRATION_PLAN.md, Migration E)."
            )
        op.drop_column("documents", column)


def downgrade() -> None:
    op.add_column("documents", sa.Column("total_amount", sa.Numeric(18, 4)))
    op.add_column("documents", sa.Column("currency", sa.String(3)))
    op.add_column("documents", sa.Column("document_date", sa.Date()))
