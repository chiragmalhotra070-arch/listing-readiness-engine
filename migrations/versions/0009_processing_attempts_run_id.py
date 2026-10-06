"""add processing_attempts.run_id

Revision ID: 0009_processing_attempts_run_id
Revises: 0008_processing_runs
Create Date: 2026-09-30

Additive only: existing attempts are preserved untouched.  The column is
nullable so that historical rows can be backfilled by
``scripts/backfill_processing_runs.py`` before 0013_tighten_run_foreign_keys
turns it into NOT NULL.

``document_id`` and ``attempt_number`` are deliberately kept: ``document_id``
is the query-convenience FK used everywhere in the API, and ``attempt_number``
is the legacy document-global sequence consumed by the retry budget.
"""

from alembic import op
import sqlalchemy as sa


revision = "0009_processing_attempts_run_id"
down_revision = "0008_processing_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("processing_attempts", sa.Column("run_id", sa.Integer(), sa.ForeignKey("processing_runs.id"), nullable=True))
    op.create_index("ix_processing_attempts_run_id", "processing_attempts", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_processing_attempts_run_id", table_name="processing_attempts")
    op.drop_column("processing_attempts", "run_id")
