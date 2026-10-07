from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.enums import BusinessOutcome, DocumentType, FailureType, ProcessingStatus

# ---------------------------------------------------------------------------
# API input-boundary constraints (request metadata only)
#
# Every ``max_length`` matches the ``varchar(n)``/``Text`` column the value is
# persisted in, so an oversized value is refused by the request contract
# (HTTP 422) instead of reaching PostgreSQL and failing there with
# SQLSTATE 22001 (``value too long for type character varying(n)``).
#
# A *pattern* is only used where the system itself defines the shape:
# MIME types (``type/subtype``), sha256 hex digests used for content
# verification, and path-ish attachment ids and storage references.  Human-entered
# metadata is deliberately length-only - real subjects, company names, sender
# addresses and document names legitimately contain spaces, parentheses,
# punctuation and non-ASCII characters (live data includes
# ``"Separate Remittance Advice (1) (1).pdf"``).
#
# ``source_email_id``, ``sender_email``, ``correlation_id``,
# ``n8n_execution_id``, ``content_hash`` and ``mock_profile`` are length-only
# on purpose: their NUL-byte rejection contract (``NulByteRejectedError`` for
# the named column) must stay reachable, and a pattern would reject the value
# in Pydantic before that boundary could report it.
# ---------------------------------------------------------------------------

#: RFC 2045 ``token/token`` with optional parameters.  The multipart path
#: stores an upload part's ``Content-Type`` verbatim, so parameters such as
#: ``; name="remittance.pdf"`` must remain acceptable; CR/LF is excluded so a
#: header value can never be smuggled through.
MIME_TYPE_PATTERN = r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}/[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}(?:;\s*[^\r\n]+)?$"

#: The only hash format ``LocalDocumentStorage`` produces and compares
#: (``hashlib.sha256(...).hexdigest()``).
SHA256_HEX_PATTERN = r"^[0-9a-fA-F]{64}$"

#: Attachment ids are opaque, but every format the system produces or
#: observes is path-ish: the derived ``source_email_id:index`` here, and
#: n8n's ``filesystem-v2:workflows/<id>/executions/<n>/binary_data/<uuid>``
#: (36 live rows).
SOURCE_ATTACHMENT_ID_PATTERN = r"^[A-Za-z0-9@._:/-]{1,255}$"

#: ``storage_reference`` is opaque to the API but must stay a path-ish token:
#: the suite's own valid requests carry placeholders such as
#: ``docs/missing.pdf`` alongside real ``local://documents/…`` references, so
#: the contract here is the column length plus "no whitespace, quotes or
#: control characters".  Scheme and containment are enforced at read time by
#: ``LocalDocumentStorage.resolve()`` (``local://documents/`` prefix, storage
#: root containment), which already fails those with a controlled error.
STORAGE_REFERENCE_PATTERN = r"^[A-Za-z0-9._:/@-]{1,1000}$"

#: ``emails.source_email_id`` is ``varchar(255)`` but also feeds the derived
#: ``documents.source_attachment_id`` value ``f"{source_email_id}:{index}"``,
#: so 250 leaves room for ``:`` plus the document index inside the same
#: column.  Exact worst case under current rules: 250 + 1 (":") + 2 digits
#: (``index`` up to ``max_documents_per_intake`` = 25) = 253 <= 255; the
#: derivation sites re-check the column ceiling in
#: ``derived_source_attachment_id``, so raising either ceiling alone cannot
#: silently overflow it.  Length-only: see the module note above.
SOURCE_EMAIL_ID_MAX = 250

#: ``emails.body`` is unbounded ``Text``; the cap is a metadata envelope, not
#: a schema boundary - 1 MiB is 63x the largest body observed in the live
#: data (15,843 characters) and far beyond any business email body.
BODY_MAX_LENGTH = 1_000_000

#: ``documents.mock_profile`` selects an exact-match fixture token in the
#: adapters (longest known: ``conflicting_customer_candidates``, 31 chars).
MOCK_PROFILE_MAX_LENGTH = 64

#: ``file_size_bytes`` is a PostgreSQL ``integer`` (int4); larger values would
#: fail with SQLSTATE 22001 after validation.
INTEGER_MAX = 2_147_483_647


class DocumentInput(BaseModel):
    document_name: str = Field(max_length=255)
    mime_type: str = Field(max_length=255, pattern=MIME_TYPE_PATTERN)
    storage_reference: Optional[str] = Field(default=None, max_length=1000, pattern=STORAGE_REFERENCE_PATTERN)
    content_hash: Optional[str] = Field(default=None, max_length=128)
    expected_content_hash: Optional[str] = Field(default=None, pattern=SHA256_HEX_PATTERN)
    source_attachment_id: Optional[str] = Field(default=None, max_length=255, pattern=SOURCE_ATTACHMENT_ID_PATTERN)
    file_size_bytes: Optional[int] = Field(default=None, ge=0, le=INTEGER_MAX)
    mock_profile: Optional[str] = Field(default=None, max_length=MOCK_PROFILE_MAX_LENGTH)


class EmailIntakeRequest(BaseModel):
    source_email_id: str = Field(max_length=SOURCE_EMAIL_ID_MAX)
    received_at: datetime
    sender_email: str = Field(max_length=320)
    sender_name: Optional[str] = Field(default=None, max_length=255)
    sender_company: Optional[str] = Field(default=None, max_length=255)
    subject: Optional[str] = Field(default=None, max_length=1000)
    body: Optional[str] = Field(default=None, max_length=BODY_MAX_LENGTH)
    applicability: Optional[bool] = None
    correlation_id: Optional[str] = Field(default=None, max_length=255)
    n8n_execution_id: Optional[str] = Field(default=None, max_length=255)
    documents: list[DocumentInput] = Field(min_length=1)


class N8nIngestDocument(BaseModel):
    document_name: str = Field(max_length=255)
    mime_type: str = Field(max_length=255, pattern=MIME_TYPE_PATTERN)
    source_attachment_id: Optional[str] = Field(default=None, max_length=255, pattern=SOURCE_ATTACHMENT_ID_PATTERN)
    content_base64: str = Field(min_length=1)


class N8nIngestRequest(BaseModel):
    source_email_id: str = Field(max_length=SOURCE_EMAIL_ID_MAX)
    received_at: datetime
    sender_email: str = Field(max_length=320)
    sender_name: Optional[str] = Field(default=None, max_length=255)
    sender_company: Optional[str] = Field(default=None, max_length=255)
    subject: Optional[str] = Field(default=None, max_length=1000)
    body: Optional[str] = Field(default=None, max_length=BODY_MAX_LENGTH)
    documents: list[N8nIngestDocument] = Field(min_length=1)


class RemittanceFields(BaseModel):
    remittance_number: Optional[str] = None
    remittance_date: Optional[date] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class InvoiceFields(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    subtotal: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class PaymentAdviceFields(BaseModel):
    payment_reference: Optional[str] = None
    payment_date: Optional[date] = None
    amount: Optional[float] = None
    currency: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class NoteFields(BaseModel):
    note_number: Optional[str] = None
    note_date: Optional[date] = None
    reference_invoice: Optional[str] = None
    adjustment_amount: Optional[float] = None
    tax_amount: Optional[float] = None
    total_amount: Optional[float] = None
    currency: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class BankStatementFields(BaseModel):
    account_number_masked: Optional[str] = None
    statement_start_date: Optional[date] = None
    statement_end_date: Optional[date] = None
    opening_balance: Optional[float] = None
    closing_balance: Optional[float] = None
    currency: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


class CustomerCandidates(BaseModel):
    customer_id_candidates: list[str] = Field(default_factory=list)
    customer_name_candidates: list[str] = Field(default_factory=list)
    other_identifier_candidates: list[str] = Field(default_factory=list)


class SupplementalInformation(BaseModel):
    name: str
    value: Optional[Union[str, float, int, bool]]
    source: Literal["document"]
    model_config = ConfigDict(extra="forbid")


AssessmentCategory = Literal[
    "CLEAR_SINGLE_CUSTOMER",
    "CUSTOMER_ID_REQUIRES_VERIFICATION",
    "MULTIPLE_CUSTOMERS_FOUND",
    "NO_CUSTOMER_IDENTIFIER",
    "CONFLICTING_CUSTOMER_IDENTIFIERS",
    "CUSTOMER_NOT_IDENTIFIED",
    "MULTIPLE_TRANSACTIONS_FOUND",
]


class LLMAssessment(BaseModel):
    category: AssessmentCategory
    summary: str = Field(min_length=1, max_length=500)
    evidence: list[str] = Field(default_factory=list, max_length=5)
    model_config = ConfigDict(extra="forbid")


class LLMAnalysisResult(BaseModel):
    document_type: DocumentType
    assessment: Optional[LLMAssessment] = None
    extracted_fields: dict[str, Any] = Field(default_factory=dict)
    supplemental_information: list[SupplementalInformation] = Field(default_factory=list)
    customer_candidates: CustomerCandidates = Field(default_factory=CustomerCandidates)
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    ambiguities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    provider_request_id: Optional[str] = None
    raw_metadata: dict[str, Any] = Field(default_factory=dict)
    model_config = ConfigDict(extra="forbid")


class DocumentResult(BaseModel):
    id: int
    document_name: Optional[str] = None
    decision: Optional[BusinessOutcome]
    decision_reason: Optional[str]
    processing_status: ProcessingStatus
    retryable: Optional[bool] = None
    failure_type: Optional[FailureType] = None
    evidence: Optional[dict[str, Any]] = None
    current_stage: Optional[str] = None
    stage_status: Optional[str] = None
    overall_status: Optional[str] = None
    next_attempt_at: Optional[datetime] = None
    attempt_count: int = 0
    retry_allowed: bool = False
    document_id: Optional[int] = None
    source_email_id: Optional[str] = None
    correlation_id: Optional[str] = None
    n8n_execution_id: Optional[str] = None
    business_document_type: Optional[DocumentType] = None
    business_reference: Optional[str] = None
    duplicate: Optional[bool] = None
    customer_id: Optional[str] = None
    customer_type: Optional[str] = None
    processing_attempt_id: Optional[int] = None
    storage_reference: Optional[str] = None
    content_hash: Optional[str] = None
    file_size_bytes: Optional[int] = None
    source_attachment_id: Optional[str] = None


class ProcessingAttemptResponse(BaseModel):
    id: int
    document_id: int
    attempt_number: int
    stage: str
    provider: str
    started_at: datetime
    completed_at: Optional[datetime] = None
    status: str
    failure_code: Optional[str] = None
    failure_message: Optional[str] = None
    failure_type: Optional[FailureType] = None
    retryable: bool
    next_attempt_at: Optional[datetime] = None
    quality_score: Optional[float] = None
    result: Optional[dict[str, Any]] = None
    idempotency_key: Optional[str] = None

    model_config = {"from_attributes": True}


class AuditEventResponse(BaseModel):
    id: int
    email_id: Optional[int] = None
    document_id: Optional[int] = None
    event_type: str
    status: str
    message: str
    details: Optional[dict[str, Any]] = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EmailIntakeResponse(BaseModel):
    email_id: int
    source_email_id: Optional[str] = None
    correlation_id: Optional[str] = None
    n8n_execution_id: Optional[str] = None
    documents: list[DocumentResult]
    idempotent: bool = False


# ---------------------------------------------------------------------------
# Listing extraction schemas (California MVP)
#
# One base schema standardized across all listing documents, plus small
# per-type deltas.  The demo tracks every document with the same classified
# strategy: the base fields are the common vocabulary shared by the
# classifier, the semantic field resolver and the requirement engine;
# per-type fields carry only what is specific to that document.
#
# Field set comes from the listing-document research (2026-10-06):
# Tier-1 universal, Tier-2 conditional and Tier-3 realtor-variable lists.
# ---------------------------------------------------------------------------


class ListingDocumentFields(BaseModel):
    """Fields common to every listing document."""

    property_address: Optional[str] = None
    apn: Optional[str] = None
    seller_name: Optional[str] = None
    document_date: Optional[date] = None
    listing_agent_name: Optional[str] = None
    brokerage_name: Optional[str] = None
    signatures_present: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class ListingAgreementFields(ListingDocumentFields):
    """Residential Listing Agreement (CAR RLA)."""

    listing_price: Optional[float] = None
    listing_start_date: Optional[date] = None
    listing_end_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class SellerAdvisoryFields(ListingDocumentFields):
    """Seller's Advisory (CAR SA) -- pre-listing advisory."""

    advisory_acknowledged: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class AgencyDisclosureFields(ListingDocumentFields):
    """Disclosure Regarding Real Estate Agency Relationships (CAR AD)."""

    agency_relationship: Optional[str] = None
    disclosure_signed: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class TDSFields(ListingDocumentFields):
    """Transfer Disclosure Statement (CAR TDS)."""

    disclosure_signed: Optional[bool] = None
    disclosure_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class SPQFields(ListingDocumentFields):
    """Seller Property Questionnaire (CAR SPQ)."""

    questionnaire_complete: Optional[bool] = None
    completion_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class AgentVisualInspectionFields(ListingDocumentFields):
    """Agent Visual Inspection Disclosure (CAR AVID)."""

    inspection_date: Optional[date] = None
    inspection_complete: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class NHDFields(ListingDocumentFields):
    """Natural Hazard Disclosure report + signed statement/receipt."""

    report_date: Optional[date] = None
    hazards_disclosed: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class WCMDFields(ListingDocumentFields):
    """Water-conserving plumbing fixtures & CO detector advisory (WCMD)."""

    fixtures_compliant: Optional[bool] = None
    model_config = ConfigDict(extra="forbid")


class LeadDisclosureFields(ListingDocumentFields):
    """Lead-based paint disclosure (federal; triggered pre-1978)."""

    pamphlet_acknowledged: Optional[bool] = None
    disclosure_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class HOAPackageFields(ListingDocumentFields):
    """HOA resale package (CC&Rs, budget, minutes)."""

    hoa_name: Optional[str] = None
    package_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class PrelimTitleFields(ListingDocumentFields):
    """Preliminary title report."""

    title_company: Optional[str] = None
    report_date: Optional[date] = None
    model_config = ConfigDict(extra="forbid")


class SolarAgreementFields(ListingDocumentFields):
    """Solar ownership / financing agreement."""

    ownership_type: Optional[str] = None  # owned | leased | PPA
    provider_name: Optional[str] = None
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Listing stage endpoints (Slice 7) -- one thin endpoint per workflow stage.
#
# n8n orchestrates these; it passes ids and reads responses, never holds
# workflow state.  Request constraints match the varchar(n) columns, per the
# input-boundary rule above.
# ---------------------------------------------------------------------------


class ListingFileCreate(BaseModel):
    """POST /listing-files -- creates the file; generation is a later stage.

    ``idempotency_key`` is the orchestrator's handle for "this logical file":
    a repeat with the same key returns the existing row (200) instead of
    creating a duplicate.  Optional so direct/manual calls stay keyless.
    """

    property_address: Optional[str] = Field(default=None, max_length=500)
    apn: Optional[str] = Field(default=None, max_length=64)
    seller_name: Optional[str] = Field(default=None, max_length=255)
    property_attributes: dict[str, Any]
    idempotency_key: Optional[str] = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_state_attribute(self) -> "ListingFileCreate":
        state = self.property_attributes.get("state")
        if not isinstance(state, str) or not state.strip():
            raise ValueError("property_attributes.state is required")
        return self


class ListingFileCreated(BaseModel):
    id: int


class RequirementSummary(BaseModel):
    """One generated requirement as the orchestrator reads it back."""

    requirement_key: str
    requirement_type: str
    status: str
    state: str


class RequirementListResponse(BaseModel):
    requirements: list[RequirementSummary]


class DocumentUploaded(BaseModel):
    document_id: int


class DocumentClassified(BaseModel):
    document_id: int
    document_type: str
    classification_confidence: Optional[float]


class DocumentExtracted(BaseModel):
    document_id: int
    document_type: str
    extracted_data: dict[str, Any]
    #: Stage 1 canonical view of the shared fact fields (exact-after-
    #: normalization); the raw ``extracted_data`` above is never rewritten.
    normalized_data: dict[str, Any]


class ReconciliationReport(BaseModel):
    """POST /listing-files/{id}/reconcile -- what one matching pass did."""

    evidence_created: int
    requirements_moved_to_received: list[str]
    unmet_requirement_keys: list[str]
    unmatched_document_ids: list[int]


class VerdictResponse(BaseModel):
    """POST /listing-files/{id}/verdict -- the rollup verdict for one file."""

    verdict: str
    reason: str
    blocking_pending: list[str]
    blocking_exceptions: list[str]
    pending_verification: list[str]
    advisory: list[str]


class ReadoutRequirement(RequirementSummary):
    """One requirement as the readout reports it: ``overdue`` is derived
    (PENDING past ``settings.requirement_overdue_days``), never persisted."""

    overdue: bool = False


class ListingFileReadout(BaseModel):
    """GET /listing-files/{id} -- the whole file state in one read."""

    id: int
    property_address: Optional[str]
    apn: Optional[str]
    seller_name: Optional[str]
    property_attributes: Optional[dict[str, Any]]
    verdict: Optional[str]
    reason: Optional[str]
    readiness_assessed_at: Optional[datetime]
    requirements: list[ReadoutRequirement]
    unmatched_document_ids: list[int]


class ReviewQueueItem(BaseModel):
    """One requirement in the review queue: ``requirement_id`` is the handle
    the verify/flag endpoints act on."""

    requirement_id: int
    requirement_key: str
    requirement_type: str
    status: str
    state: str
    state_reason: Optional[str] = None


class ReviewQueueDocument(BaseModel):
    """One ``UNKNOWN``-classified document awaiting a human decision."""

    document_id: int
    document_name: str


class ReviewQueueUnmatched(BaseModel):
    """One document no requirement has evidence for, and why it is unmatched:

    ``property_mismatch`` -- Stage 2 quarantined it (its APN/address
    disagrees with the file's property identity); ``no_evidence`` -- no
    requirement bound it (unknown classification, unrequested type, or
    uploaded since the last reconcile).
    """

    document_id: int
    document_name: str
    reason: Literal["property_mismatch", "no_evidence"]


class ProcessingFailedRequest(BaseModel):
    """POST /listing-files/{file}/documents/{doc}/processing-failed -- the
    orchestrator reports that node retries were exhausted for one document
    at one stage.  Human remedy only (re-upload / investigate); the engine
    never auto-retries an exhausted document."""

    stage: str = Field(max_length=64)
    reason: str = Field(max_length=2000)

    @model_validator(mode="after")
    def stage_and_reason_required(self) -> "ProcessingFailedRequest":
        if not self.stage.strip():
            raise ValueError("stage is required")
        if not self.reason.strip():
            raise ValueError("reason is required")
        return self


class ProcessingFailedRecorded(BaseModel):
    """Acknowledgement: the report was recorded, with its attempt count."""

    document_id: int
    stage: str
    reason: str
    attempt_count: int


class ReviewQueueProcessingFailed(BaseModel):
    """One document whose orchestrator retries were exhausted at a stage:
    latest report per (document, stage), ``attempt_count`` = how many times
    exhaustion was reported there."""

    document_id: int
    stage: str
    reason: str
    attempt_count: int


class ReviewQueue(BaseModel):
    """GET /listing-files/{id}/review-queue -- everything a human must act on.

    Flat buckets, no ranking and no confidence threshold: every requirement
    in ``EXCEPTION``, every ``RECEIVED`` requirement (pending verification),
    every ``UNKNOWN`` document, every document no requirement has evidence
    for (with its reason), every ``PENDING`` requirement past the
    overdue threshold, and every document whose orchestrator retries were
    exhausted (``processing_failed``).
    """

    listing_file_id: int
    exceptions: list[ReviewQueueItem]
    unknown_documents: list[ReviewQueueDocument]
    unmatched_documents: list[ReviewQueueUnmatched]
    pending_verification: list[ReviewQueueItem]
    overdue: list[ReviewQueueItem]
    processing_failed: list[ReviewQueueProcessingFailed]


class ConflictFinding(BaseModel):
    """R1: one field where evidence documents disagree after normalization."""

    field: str
    values: list[str]
    document_ids: list[int]
    requirement_keys: list[str]
    reason: str


class MissingSignatureFinding(BaseModel):
    """R2: an unsigned signature-bearing evidence document."""

    requirement_key: str
    document_type: str
    document_id: int
    reason: str


class OverdueRequirementFinding(BaseModel):
    """R3: a PENDING requirement past ``settings.requirement_overdue_days``."""

    requirement_key: str
    requirement_id: int
    days_pending: int


class DetectExceptionsReport(BaseModel):
    """POST /listing-files/{id}/detect-exceptions -- R1/R2/R3 findings.

    ``exceptions_raised`` counts requirement state changes this pass made
    (R1/R2 only; R3 never mutates).  The verdict is not recomputed here --
    run the verdict stage afterwards to roll the new states up.
    """

    listing_file_id: int
    conflicts: list[ConflictFinding]
    missing_signatures: list[MissingSignatureFinding]
    overdue: list[OverdueRequirementFinding]
    exceptions_raised: int


class RequirementVerifyRequest(BaseModel):
    """POST /requirements/{id}/verify -- human confirms the requirement."""

    note: Optional[str] = Field(default=None, max_length=1000)


class RequirementFlagRequest(BaseModel):
    """POST /requirements/{id}/flag -- human raises an exception; reason required."""

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def reason_must_not_be_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("reason must not be blank")
        return stripped


class RequirementActionResponse(BaseModel):
    """One human action on one requirement, plus the recomputed file verdict."""

    requirement_id: int
    requirement_key: str
    state: str
    state_reason: Optional[str] = None
    verdict: str
    reason: str


class DocumentReclassifyRequest(BaseModel):
    """POST /documents/{id}/reclassify -- correct an ``UNKNOWN`` classification."""

    document_type: DocumentType


class DocumentReclassified(BaseModel):
    document_id: int
    document_type: str
