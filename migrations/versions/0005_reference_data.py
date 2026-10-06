"""reference data tables - RETIRED (Q1), DDL disabled

Revision ID: 0005_reference_data
Revises: 0004_file_metadata
Create Date: 2026-09-20 (original)

**This revision has been retired.  Its DDL no longer runs.**

Architectural decision Q1 dropped the reference_* architecture:

    * ``customers`` is the authoritative customer master,
    * ``reference_customers`` / ``reference_emails`` / ``reference_documents``
      must not be recreated,
    * the live database never applied this revision (``alembic_version`` is
      ``0006_document_work_items``).

The revision file is kept - rather than deleted - so that the migration graph
stays intact for any database that may already be stamped with this version,
and so that ``0007_merge_heads`` can join the two branches that forked at
``0004_file_metadata`` and give the graph exactly one head.

``upgrade()`` / ``downgrade()`` are intentionally empty.  The authoritative
drop of any reference_* tables that *were* created by an earlier build of this
file lives in ``0012_drop_reference_architecture`` (guarded with an existence
check, so it is a no-op on databases that never had them).

See TARGET_SCHEMA_MIGRATION_PLAN.md, Phase 5.
"""

from alembic import op


revision = "0005_reference_data"
down_revision = "0004_file_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Retired by decision Q1. Deliberately a no-op: reference_* tables must
    # not be recreated. Original DDL is preserved in git history.
    pass


def downgrade() -> None:
    # Retired by decision Q1. Nothing to undo - the tables were never created
    # by this revision in its retired form.
    pass
