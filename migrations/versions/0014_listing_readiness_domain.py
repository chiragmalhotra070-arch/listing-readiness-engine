"""listing readiness domain: files, requirement catalog, evidence

Revision ID: 0014_listing_readiness_domain
Revises: 0013_tighten_run_foreign_keys
Create Date: 2026-10-06

Slice 1 of the listing engine (V0): the file-level domain.

* ``listing_files`` -- one row per property+seller: canonical identity,
  the property-attributes fact sheet that drives requirement generation,
  and the file-level readiness verdict.  The verdict uses the new
  ``ReadinessVerdict`` enum; document-level ``BusinessOutcome`` is
  deliberately untouched.
* ``requirement_rules`` -- the versioned requirement catalog (the
  "Initial California requirement model").  Data, not code:
  ``(requirement_key, version)`` is unique so catalog revisions coexist.
* ``requirements`` -- generated requirement instances per listing file.
  The PENDING -> RECEIVED -> VERIFIED / EXCEPTION request lifecycle is
  enforced by a CHECK constraint.
* ``evidence`` -- document / observation / attestation rows satisfying a
  requirement; confidence-gated.
* ``documents.listing_file_id`` -- binds upload-intake documents to a file.
* ``documents.email_id`` -- relaxed to nullable: upload intake has no email.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0014_listing_readiness_domain"
down_revision = "0013_tighten_run_foreign_keys"
branch_labels = None
depends_on = None


def _jsonb():
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "listing_files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("property_address", sa.String(500)),
        sa.Column("apn", sa.String(64)),
        sa.Column("seller_name", sa.String(255)),
        sa.Column("property_attributes", _jsonb()),
        sa.Column("readiness_verdict", sa.String(32)),
        sa.Column("readiness_reason", sa.Text()),
        sa.Column("readiness_assessed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
    )
    op.create_index("ix_listing_files_apn", "listing_files", ["apn"])

    op.create_table(
        "requirement_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requirement_key", sa.String(64), nullable=False),
        sa.Column("requirement_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("jurisdiction", sa.String(16), nullable=False),
        sa.Column("satisfied_by", _jsonb()),
        sa.Column("trigger", _jsonb()),
        sa.Column("timing", sa.String(64)),
        sa.Column("source", sa.String(255)),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("effective_from", sa.DateTime(timezone=True)),
        sa.Column("effective_to", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("requirement_key", "version", name="uq_requirement_rules_key_version"),
    )
    op.create_index("ix_requirement_rules_requirement_key", "requirement_rules", ["requirement_key"])
    op.create_index("ix_requirement_rules_jurisdiction", "requirement_rules", ["jurisdiction"])

    op.create_table(
        "requirements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("listing_file_id", sa.Integer(), sa.ForeignKey("listing_files.id"), nullable=False),
        sa.Column("requirement_rule_id", sa.Integer(), sa.ForeignKey("requirement_rules.id")),
        sa.Column("requirement_key", sa.String(64), nullable=False),
        sa.Column("requirement_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("state", sa.String(32), nullable=False, server_default="PENDING"),
        sa.Column("state_reason", sa.Text()),
        sa.Column("owner", sa.String(255)),
        sa.Column("due_date", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.UniqueConstraint("listing_file_id", "requirement_key", name="uq_requirements_file_key"),
        sa.CheckConstraint("state IN ('PENDING', 'RECEIVED', 'VERIFIED', 'EXCEPTION')", name="ck_requirements_state"),
    )
    op.create_index("ix_requirements_listing_file_id", "requirements", ["listing_file_id"])
    op.create_index("ix_requirements_file_state", "requirements", ["listing_file_id", "state"])

    op.create_table(
        "evidence",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("requirement_id", sa.Integer(), sa.ForeignKey("requirements.id"), nullable=False),
        sa.Column("document_id", sa.Integer(), sa.ForeignKey("documents.id")),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float()),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("detail", _jsonb()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_evidence_requirement_id", "evidence", ["requirement_id"])
    op.create_index("ix_evidence_document_id", "evidence", ["document_id"])

    with op.batch_alter_table("documents") as batch:
        batch.add_column(sa.Column("listing_file_id", sa.Integer(), sa.ForeignKey("listing_files.id")))
        batch.create_index("ix_documents_listing_file_id", ["listing_file_id"])
        batch.alter_column("email_id", nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("documents") as batch:
        batch.drop_index("ix_documents_listing_file_id")
        batch.drop_column("listing_file_id")
        batch.alter_column("email_id", nullable=False)
    op.drop_index("ix_evidence_document_id", table_name="evidence")
    op.drop_index("ix_evidence_requirement_id", table_name="evidence")
    op.drop_table("evidence")
    op.drop_index("ix_requirements_file_state", table_name="requirements")
    op.drop_index("ix_requirements_listing_file_id", table_name="requirements")
    op.drop_table("requirements")
    op.drop_index("ix_requirement_rules_jurisdiction", table_name="requirement_rules")
    op.drop_index("ix_requirement_rules_requirement_key", table_name="requirement_rules")
    op.drop_table("requirement_rules")
    op.drop_index("ix_listing_files_apn", table_name="listing_files")
    op.drop_table("listing_files")
