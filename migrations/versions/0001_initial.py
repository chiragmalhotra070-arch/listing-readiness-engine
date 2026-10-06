"""create V1 intake schema"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table("emails", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("source_email_id", sa.String(255), nullable=False), sa.Column("received_at", sa.DateTime(timezone=True), nullable=False), sa.Column("sender_email", sa.String(320), nullable=False), sa.Column("sender_name", sa.String(255)), sa.Column("sender_company", sa.String(255)), sa.Column("subject", sa.String(1000)), sa.Column("body", sa.Text()), sa.Column("applicability", sa.Boolean()), sa.Column("status", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("source_email_id"))
    op.create_table("customers", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("customer_id", sa.String(255), nullable=False), sa.Column("customer_name", sa.String(255), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.UniqueConstraint("customer_id"))
    op.create_table("customer_email_mappings", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id"), nullable=False), sa.Column("email_address", sa.String(320), nullable=False), sa.Column("verified", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("documents", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("email_id", sa.Integer(), sa.ForeignKey("emails.id"), nullable=False), sa.Column("customer_id", sa.Integer(), sa.ForeignKey("customers.id")), sa.Column("document_name", sa.String(255), nullable=False), sa.Column("mime_type", sa.String(255), nullable=False), sa.Column("storage_reference", sa.String(1000)), sa.Column("content_hash", sa.String(128)), sa.Column("document_type", sa.String(64), nullable=False), sa.Column("document_date", sa.Date()), sa.Column("total_amount", sa.Numeric(18, 4)), sa.Column("currency", sa.String(3)), sa.Column("extracted_text", sa.Text()), sa.Column("extracted_data", postgresql.JSONB()), sa.Column("ocr_score", sa.Float()), sa.Column("classification_confidence", sa.Float()), sa.Column("evidence", postgresql.JSONB()), sa.Column("duplicate", sa.Boolean(), nullable=False), sa.Column("duplicate_of_document_id", sa.Integer(), sa.ForeignKey("documents.id")), sa.Column("decision", sa.String(64)), sa.Column("decision_reason", sa.Text()), sa.Column("processing_status", sa.String(64), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("processing_attempts", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id"), nullable=False), sa.Column("attempt_number", sa.Integer(), nullable=False), sa.Column("stage", sa.String(64), nullable=False), sa.Column("provider", sa.String(255), nullable=False), sa.Column("started_at", sa.DateTime(timezone=True), nullable=False), sa.Column("completed_at", sa.DateTime(timezone=True)), sa.Column("status", sa.String(64), nullable=False), sa.Column("failure_code", sa.String(128)), sa.Column("failure_message", sa.Text()), sa.Column("failure_type", sa.String(32)), sa.Column("retryable", sa.Boolean(), nullable=False), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_table("audit_events", sa.Column("id", sa.Integer(), primary_key=True), sa.Column("email_id", sa.Integer(), sa.ForeignKey("emails.id")), sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id")), sa.Column("event_type", sa.String(128), nullable=False), sa.Column("status", sa.String(64), nullable=False), sa.Column("message", sa.Text(), nullable=False), sa.Column("details", postgresql.JSONB()), sa.Column("correlation_id", sa.String(255)), sa.Column("n8n_execution_id", sa.String(255)), sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    for table, columns in {"emails": ["source_email_id"], "customers": ["customer_id"], "customer_email_mappings": ["customer_id", "email_address"], "documents": ["email_id", "customer_id", "content_hash"], "processing_attempts": ["document_id"], "audit_events": ["email_id", "document_id"]}.items():
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    for table in ["audit_events", "processing_attempts", "documents", "customer_email_mappings", "customers", "emails"]:
        op.drop_table(table)
