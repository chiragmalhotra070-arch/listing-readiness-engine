"""remove the obsolete reference_* architecture (Q1)

Revision ID: 0012_drop_reference_architecture
Revises: 0011_drop_dead_document_columns
Create Date: 2026-09-30

DESTRUCTIVE.  Requires review before it is run against a live database.

Q1 dropped the reference_* architecture; ``customers`` is the authoritative
customer master.  ``0005_reference_data`` has been neutralised so its DDL no
longer runs, therefore this migration is normally a **no-op** (the tables were
never created).  It stays in the chain as a safety net for any database that
did apply the original ``0005_reference_data``.

No runtime dependency remains: see Phase 5 of the plan for the code changes
that removed ``ReferenceCustomer`` from ``adapters/customer.py``, the
``/v1/dev/reference-data/import`` route, and ``services/reference_data.py``.
"""

from alembic import op
from sqlalchemy import inspect


revision = "0012_drop_reference_architecture"
down_revision = "0011_drop_dead_document_columns"
branch_labels = None
depends_on = None

REFERENCE_TABLES = ("reference_documents", "reference_emails", "reference_customers")


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    existing = set(inspector.get_table_names())
    for table in REFERENCE_TABLES:
        if table in existing:
            op.drop_table(table)


def downgrade() -> None:
    raise RuntimeError(
        "reference_* tables were removed by decision Q1 and are not restorable from this migration; "
        "restore them from a database backup if they are ever needed again."
    )
