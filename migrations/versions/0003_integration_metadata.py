"""add integration correlation metadata"""

from alembic import op
import sqlalchemy as sa

revision = "0003_integration_metadata"
down_revision = "0002_resilience_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("emails", sa.Column("correlation_id", sa.String(255)))
    op.add_column("emails", sa.Column("n8n_execution_id", sa.String(255)))
    op.create_index("ix_emails_correlation_id", "emails", ["correlation_id"])
    op.create_index("ix_emails_n8n_execution_id", "emails", ["n8n_execution_id"])


def downgrade() -> None:
    op.drop_index("ix_emails_n8n_execution_id", table_name="emails")
    op.drop_index("ix_emails_correlation_id", table_name="emails")
    op.drop_column("emails", "n8n_execution_id")
    op.drop_column("emails", "correlation_id")
