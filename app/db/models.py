from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base
from app.domain.enums import DocumentType, ReadinessVerdict, RequirementState, WorkItemStatus

# JSON on SQLite (unit tests build the schema with Base.metadata.create_all),
# JSONB on PostgreSQL (matches the alembic DDL).
JSONB = JSON().with_variant(postgresql.JSONB(astext_type=Text()), "postgresql")

# ``column.info`` flag: the column holds free-form prose (human text or
# provider diagnostics), so a NUL byte is noise and is stripped at the
# persistence boundary.  Every other String/Text column is a structured value -
# an identifier, reference, hash, address or vocabulary term - where stripping
# a byte could silently change identity, so a NUL byte is rejected instead.
# Enforced by app/services/text_sanitization.py on ``before_flush``.
FREE_FORM_TEXT: dict[str, bool] = {"free_form_text": True}

# ``column.info`` flag: the column is JSONB on PostgreSQL, so a NUL byte nested
# anywhere inside the value makes ``jsonb`` input fail with SQLSTATE 22P05 and
# abort the whole transaction.  The policy is therefore "reject": the value is
# refused before the driver sees it, never rewritten and never handed to
# PostgreSQL.  Plain ``json`` columns are deliberately unmarked - this phase
# leaves them exactly as they were.
JSONB_REJECT_NUL: dict[str, str] = {"jsonb_policy": "reject"}


class Email(Base):
    __tablename__ = "emails"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_email_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sender_email: Mapped[str] = mapped_column(String(320))
    sender_name: Mapped[Optional[str]] = mapped_column(String(255), info=FREE_FORM_TEXT)
    sender_company: Mapped[Optional[str]] = mapped_column(String(255), info=FREE_FORM_TEXT)
    subject: Mapped[Optional[str]] = mapped_column(String(1000), info=FREE_FORM_TEXT)
    body: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    applicability: Mapped[Optional[bool]] = mapped_column(Boolean)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    n8n_execution_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(64), default="RECEIVED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    documents: Mapped[list["Document"]] = relationship(back_populates="email")


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    customer_name: Mapped[str] = mapped_column(String(255), info=FREE_FORM_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class CustomerEmailMapping(Base):
    __tablename__ = "customer_email_mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), index=True)
    email_address: Mapped[str] = mapped_column(String(320), index=True)
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_current_stage", "current_stage"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("emails.id"), index=True)
    listing_file_id: Mapped[Optional[int]] = mapped_column(ForeignKey("listing_files.id"), index=True)
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), index=True)
    document_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(255))
    storage_reference: Mapped[Optional[str]] = mapped_column(String(1000))
    content_hash: Mapped[Optional[str]] = mapped_column(String(128), index=True)
    source_attachment_id: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    file_size_bytes: Mapped[Optional[int]] = mapped_column(Integer)
    document_type: Mapped[DocumentType] = mapped_column(String(64), default=DocumentType.UNKNOWN)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    extracted_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    #: Stage 1 canonical view of the shared fact fields (property_address,
    #: apn, seller_name, ...); written with ``extracted_data`` at every
    #: persist site (see ``normalization.ensure_extracted_fields``).  Null on
    #: rows created before Slice 9 -- readers fall back to normalizing
    #: ``extracted_data`` on the fly.
    normalized_data: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    ocr_score: Mapped[Optional[float]]
    classification_confidence: Mapped[Optional[float]]
    evidence: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    #: Orchestrator-reported retry exhaustion, append-only: entries of
    #: {stage, reason, attempt, recorded_at} written by POST
    #: /listing-files/{file}/documents/{doc}/processing-failed.  The review
    #: queue's processing_failed bucket reads the latest entry per stage.
    processing_failures: Mapped[Optional[list[Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    duplicate_of_document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id"))
    decision: Mapped[Optional[str]] = mapped_column(String(64))
    decision_reason: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    processing_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    current_stage: Mapped[str] = mapped_column(String(64), default="EMAIL_INGESTION")
    stage_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    overall_status: Mapped[str] = mapped_column(String(64), default="PENDING")
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    retryable: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    email: Mapped[Optional[Email]] = relationship(back_populates="documents")
    listing_file: Mapped[Optional["ListingFile"]] = relationship(back_populates="documents")


class BusinessIdentityOwnership(Base):
    __tablename__ = "business_identity_ownership"
    __table_args__ = (
        UniqueConstraint("document_type", "customer_id", "business_identity", name="uq_business_identity_ownership_identity"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    customer_id: Mapped[int] = mapped_column(ForeignKey("customers.id"), nullable=False, index=True)
    business_identity: Mapped[str] = mapped_column(String(1000), nullable=False)
    owner_document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class DocumentWorkItem(Base):
    __tablename__ = "document_work_items"
    __table_args__ = (
        UniqueConstraint("document_id", "run_id", name="uq_document_work_items_document_run"),
        CheckConstraint("status IN ('PENDING', 'CLAIMED', 'RETRY_SCHEDULED', 'COMPLETED', 'FAILED')", name="ck_document_work_items_status"),
        Index("ix_document_work_items_pending_available", "status", "available_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=WorkItemStatus.PENDING)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)
    claimed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), index=True)
    worker_id: Mapped[Optional[str]] = mapped_column(String(255))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_code: Mapped[Optional[str]] = mapped_column(String(128))
    last_error_message: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProcessingRun(Base):
    """One complete processing execution journey for exactly one document.

    ``trigger`` records why the run started (INTAKE, AUTO_RETRY, MANUAL_RETRY,
    REPROCESS, CRM_ACTION, LEGACY, UNKNOWN).  A new run deliberately does NOT
    reset the document's automatic retry budget: that budget is derived from
    persisted ``processing_attempts`` rows, which are never deleted.
    """

    __tablename__ = "processing_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[Optional[str]] = mapped_column(String(64))
    worker_id: Mapped[Optional[str]] = mapped_column(String(255))
    work_item_id: Mapped[Optional[int]] = mapped_column(ForeignKey("document_work_items.id"), index=True)
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ProcessingAttempt(Base):
    __tablename__ = "processing_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    run_id: Mapped[Optional[int]] = mapped_column(ForeignKey("processing_runs.id"), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    stage: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(255))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(64))
    failure_code: Mapped[Optional[str]] = mapped_column(String(128))
    failure_message: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    failure_type: Mapped[Optional[str]] = mapped_column(String(32))
    retryable: Mapped[bool] = mapped_column(Boolean, default=False)
    next_attempt_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    quality_score: Mapped[Optional[float]]
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ExtractedField(Base):
    """Immutable per-field extraction observation produced by one attempt.

    Rows are insert-only.  On PostgreSQL a BEFORE UPDATE OR DELETE trigger
    (created by migration 0010) rejects any mutation.  ``document_field_values``
    is intentionally NOT modelled: current accepted values live on ``documents``.
    """

    __tablename__ = "extracted_fields"
    __table_args__ = (
        Index("ix_extracted_fields_document_field", "document_id", "field_name"),
        Index("ix_extracted_fields_source", "source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[int] = mapped_column(ForeignKey("processing_attempts.id"), nullable=False, index=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), nullable=False, index=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("processing_runs.id"), nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(128), nullable=False)
    field_value: Mapped[Any] = mapped_column(JSONB, nullable=False, info=JSONB_REJECT_NUL)
    value_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    confidence: Mapped[Optional[float]]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class SideEffectOperation(Base):
    __tablename__ = "side_effect_operations"

    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey("documents.id"), index=True)
    action_type: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(64))
    external_reference: Mapped[Optional[str]] = mapped_column(String(255))
    response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    email_id: Mapped[Optional[int]] = mapped_column(ForeignKey("emails.id"), index=True)
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text, info=FREE_FORM_TEXT)
    details: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    correlation_id: Mapped[Optional[str]] = mapped_column(String(255))
    n8n_execution_id: Mapped[Optional[str]] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ListingFile(Base):
    """A listing-readiness file: one property, one seller, their documents.

    The file-level aggregate for the listing engine.  Documents bind to it
    via ``documents.listing_file_id`` (email intake keeps using ``email_id``;
    upload intake sets ``listing_file_id`` instead, which is why ``email_id``
    is nullable).

    ``property_attributes`` is the JSONB fact sheet that drives requirement
    generation (``year_built``, ``property_type``, ``hoa``, ``solar``,
    ``septic``, ``seller_type``, ...).  The readiness verdict lives here,
    never on an individual document.
    """

    __tablename__ = "listing_files"

    __table_args__ = (
        # Idempotent creation: at most one file per non-null key (the
        # check-then-insert in create_listing_file still races without this).
        Index(
            "uq_listing_files_idempotency_key",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
            sqlite_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    #: Orchestrator-supplied "same logical file" key; null = keyless create.
    idempotency_key: Mapped[Optional[str]] = mapped_column(String(255))
    # Resolved property identity (canonical values; null until resolved).
    property_address: Mapped[Optional[str]] = mapped_column(String(500))
    apn: Mapped[Optional[str]] = mapped_column(String(64), index=True)
    # Resolved seller identity (display name; null until resolved).
    seller_name: Mapped[Optional[str]] = mapped_column(String(255), info=FREE_FORM_TEXT)
    #: Customer-aware intake: which customer the seller resolved to at
    #: create time (null when no seller was supplied or resolution is
    #: ambiguous), and how (EXISTING_CUSTOMER / NEW_CUSTOMER / NEEDS_REVIEW).
    customer_id: Mapped[Optional[int]] = mapped_column(ForeignKey("customers.id"), index=True)
    customer_resolution: Mapped[Optional[str]] = mapped_column(String(32))
    # Fact sheet for the requirement engine.
    property_attributes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    # Readiness verdict (ReadinessVerdict); null until assessed.
    readiness_verdict: Mapped[Optional[ReadinessVerdict]] = mapped_column(String(32))
    readiness_reason: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    readiness_assessed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    documents: Mapped[list["Document"]] = relationship(back_populates="listing_file")
    requirements: Mapped[list["Requirement"]] = relationship(back_populates="listing_file")


class RequirementRule(Base):
    """One versioned entry in the requirement catalog.

    The "Initial California requirement model": configurable, versioned
    data, not code.  The requirement engine evaluates each rule's
    ``trigger`` against a listing file's ``property_attributes``; matching
    rules generate ``Requirement`` rows.  ``(requirement_key, version)`` is
    unique so catalog revisions coexist.
    """

    __tablename__ = "requirement_rules"
    __table_args__ = (
        UniqueConstraint("requirement_key", "version", name="uq_requirement_rules_key_version"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    requirement_key: Mapped[str] = mapped_column(String(64), index=True)
    requirement_type: Mapped[str] = mapped_column(String(32))  # RequirementType
    status: Mapped[str] = mapped_column(String(32))  # RequirementStatus
    #: Owner vocabulary (seller / agent / third_party); see requirement_catalog.
    owner: Mapped[Optional[str]] = mapped_column(String(32))
    jurisdiction: Mapped[str] = mapped_column(String(16), index=True)
    satisfied_by: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    trigger: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    timing: Mapped[Optional[str]] = mapped_column(String(64))
    source: Mapped[Optional[str]] = mapped_column(String(255))
    version: Mapped[str] = mapped_column(String(32))
    effective_from: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    effective_to: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Requirement(Base):
    """A generated requirement instance for one listing file.

    Produced by the requirement engine from ``RequirementRule`` rows plus
    the file's ``property_attributes``.  Lifecycle: PENDING → RECEIVED →
    VERIFIED, or EXCEPTION.  A conditionally-ready gap plan is expressed
    with ``owner`` + ``due_date``.
    """

    __tablename__ = "requirements"
    __table_args__ = (
        UniqueConstraint("listing_file_id", "requirement_key", name="uq_requirements_file_key"),
        CheckConstraint(
            "state IN ('PENDING', 'RECEIVED', 'VERIFIED', 'EXCEPTION')",
            name="ck_requirements_state",
        ),
        Index("ix_requirements_file_state", "listing_file_id", "state"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    listing_file_id: Mapped[int] = mapped_column(ForeignKey("listing_files.id"), index=True)
    requirement_rule_id: Mapped[Optional[int]] = mapped_column(ForeignKey("requirement_rules.id"))
    requirement_key: Mapped[str] = mapped_column(String(64))
    requirement_type: Mapped[str] = mapped_column(String(32))  # RequirementType
    status: Mapped[str] = mapped_column(String(32))  # RequirementStatus
    state: Mapped[str] = mapped_column(String(32), nullable=False, default=RequirementState.PENDING)
    state_reason: Mapped[Optional[str]] = mapped_column(Text, info=FREE_FORM_TEXT)
    owner: Mapped[Optional[str]] = mapped_column(String(255))
    due_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    listing_file: Mapped["ListingFile"] = relationship(back_populates="requirements")
    evidence: Mapped[list["Evidence"]] = relationship(back_populates="requirement")


class Evidence(Base):
    """Something claimed to satisfy a requirement.

    Usually a document; occasionally an observation or an attestation
    (e.g. agent-attested showing access).  Confidence-gated: low-confidence
    evidence routes to human confirmation, never auto-trust.
    """

    __tablename__ = "evidence"

    id: Mapped[int] = mapped_column(primary_key=True)
    requirement_id: Mapped[int] = mapped_column(ForeignKey("requirements.id"), index=True)
    document_id: Mapped[Optional[int]] = mapped_column(ForeignKey("documents.id"), index=True)
    source: Mapped[str] = mapped_column(String(16))  # EvidenceSource
    confidence: Mapped[Optional[float]]
    verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    verified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    detail: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, info=JSONB_REJECT_NUL)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    requirement: Mapped["Requirement"] = relationship(back_populates="evidence")
