"""merge alembic heads into a single authoritative head

Revision ID: 0007_merge_heads
Revises: 0005_reference_data, 0006_document_work_items
Create Date: 2026-09-30

The migration graph previously forked at 0004_file_metadata:

    0004_file_metadata -> 0005_reference_data            (head)
    0004_file_metadata -> 0005_business_identity_ownership
                       -> 0006_document_work_items       (head)

Two heads meant ``alembic upgrade head`` could not resolve a single target.
This revision joins both branches so the graph has exactly one head again.

``0005_reference_data`` is retained purely as graph history (Q1 dropped the
reference_* architecture).  Whether its DDL is ever executed depends on the
starting version; the reference_* drop lives in 0012 and is guarded with
``checkfirst`` so a database that never created the tables is a no-op.
"""

from alembic import op


revision = "0007_merge_heads"
down_revision = ("0005_reference_data", "0006_document_work_items")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
