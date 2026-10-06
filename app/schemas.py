from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field

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
