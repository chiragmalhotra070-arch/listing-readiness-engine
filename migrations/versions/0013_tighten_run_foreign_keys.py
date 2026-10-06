"""tighten run foreign keys after backfill

Revision ID: 0013_tighten_run_foreign_keys
Revises: 0012_drop_reference_architecture
Create Date: 2026-09-30

Runs AFTER ``scripts/backfill_processing_runs.py`` has populated
``processing_attempts.run_id``.

    processing_attempts.run_id   NULL -> NOT NULL

``extracted_fields.run_id`` is already NOT NULL from 0010.

Safety: the migration refuses to tighten while any attempt is still orphaned,
so ``alembic upgrade head`` without a backfill fails loudly instead of
silently producing a half-migrated schema.

This is why the documented sequence is::

    alembic upgrade 0010_extracted_fields
    python scripts/backfill_processing_runs.py
    alembic upgrade head
"""

from alembic import op
import sqlalchemy as sa


revision = "0013_tighten_run_foreign_keys"
down_revision = "0012_drop_reference_architecture"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    orphaned = bind.execute(
        sa.text("SELECT count(*) FROM processing_attempts WHERE run_id IS NULL")
    ).scalar()
    if orphaned:
        raise RuntimeError(
            f"refusing to tighten processing_attempts.run_id: {orphaned} attempts have no run. "
            "Run `python scripts/backfill_processing_runs.py` first, then re-run alembic upgrade head."
        )
    with op.batch_alter_table("processing_attempts") as batch:
        batch.alter_column("run_id", nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("processing_attempts") as batch:
        batch.alter_column("run_id", nullable=True)
