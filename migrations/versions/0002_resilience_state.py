"""add resilience state and side effect operations"""

from alembic import op
import sqlalchemy as sa

revision = "0002_resilience_state"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name, column in [
        ("current_stage", sa.String(64)),
        ("stage_status", sa.String(64)),
        ("overall_status", sa.String(64)),
        ("next_attempt_at", sa.DateTime(timezone=True)),
        ("last_error", sa.Text()),
        ("retryable", sa.Boolean()),
    ]:
        op.add_column("documents", sa.Column(name, column))
    op.add_column("documents", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("processing_attempts", sa.Column("next_attempt_at", sa.DateTime(timezone=True)))
    op.add_column("processing_attempts", sa.Column("quality_score", sa.Float()))
    op.add_column("processing_attempts", sa.Column("result", sa.JSON()))
    op.add_column("processing_attempts", sa.Column("idempotency_key", sa.String(255)))
    op.create_index("ix_processing_attempts_idempotency_key", "processing_attempts", ["idempotency_key"])
    op.create_table(
        "side_effect_operations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("action_type", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("external_reference", sa.String(255)),
        sa.Column("response", sa.JSON()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("idempotency_key"),
    )
    op.create_index("ix_side_effect_operations_document_id", "side_effect_operations", ["document_id"])
    op.create_index("ix_documents_current_stage", "documents", ["current_stage"])


def downgrade() -> None:
    op.drop_index("ix_documents_current_stage", table_name="documents")
    op.drop_index("ix_side_effect_operations_document_id", table_name="side_effect_operations")
    op.drop_table("side_effect_operations")
    op.drop_index("ix_processing_attempts_idempotency_key", table_name="processing_attempts")
    for name in ["idempotency_key", "result", "quality_score", "next_attempt_at"]:
        op.drop_column("processing_attempts", name)
    for name in ["attempt_count", "retryable", "last_error", "next_attempt_at", "overall_status", "stage_status", "current_stage"]:
        op.drop_column("documents", name)
