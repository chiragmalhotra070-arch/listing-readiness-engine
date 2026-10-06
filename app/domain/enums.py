from enum import Enum


class BusinessOutcome(str, Enum):
    READY_FOR_PROCESSING = "READY_FOR_PROCESSING"
    PROCESSED = "PROCESSED"
    DUPLICATE = "DUPLICATE"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    FAILED = "FAILED"


class ProcessingStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    PENDING_RETRY = "PENDING_RETRY"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    FINAL_FAILURE = "FINAL_FAILURE"


class WorkItemStatus(str, Enum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class StageStatus(str, Enum):
    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    SUCCESS = "SUCCESS"
    RETRYABLE_FAILURE = "RETRYABLE_FAILURE"
    PERMANENT_FAILURE = "PERMANENT_FAILURE"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    FINAL_FAILURE = "FINAL_FAILURE"


class ProcessingStage(str, Enum):
    EMAIL_INGESTION = "EMAIL_INGESTION"
    ATTACHMENT_RETRIEVAL = "ATTACHMENT_RETRIEVAL"
    DOCUMENT_PARSING = "DOCUMENT_PARSING"
    OCR = "OCR"
    CLASSIFICATION = "CLASSIFICATION"
    APPLICABILITY = "APPLICABILITY"
    CUSTOMER_RESOLUTION = "CUSTOMER_RESOLUTION"
    DUPLICATE_DETECTION = "DUPLICATE_DETECTION"
    FIELD_VALIDATION = "FIELD_VALIDATION"
    # Listing-engine stages (V0 pipeline).  NORMALIZE canonicalizes extracted
    # values before anything compares them; READINESS_VERDICT is the
    # file-level verdict, distinct from the document-level BUSINESS_DECISION.
    NORMALIZE = "NORMALIZE"
    PROPERTY_RESOLUTION = "PROPERTY_RESOLUTION"
    RECONCILE = "RECONCILE"
    REQUIREMENT_CHECK = "REQUIREMENT_CHECK"
    READINESS_VERDICT = "READINESS_VERDICT"
    BUSINESS_DECISION = "BUSINESS_DECISION"
    CRM_ACTION = "CRM_ACTION"
    COMPLETE = "COMPLETE"


class FailureType(str, Enum):
    RETRYABLE = "RETRYABLE"
    PERMANENT = "PERMANENT"


class DocumentType(str, Enum):
    INVOICE = "INVOICE"
    REMITTANCE = "REMITTANCE"
    PAYMENT_ADVICE = "PAYMENT_ADVICE"
    CREDIT_NOTE = "CREDIT_NOTE"
    DEBIT_NOTE = "DEBIT_NOTE"
    BANK_STATEMENT = "BANK_STATEMENT"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    # Listing documents (California MVP).  The classification taxonomy for the
    # listing engine; the financial types above are unchanged.
    LISTING_AGREEMENT = "LISTING_AGREEMENT"
    CA_TDS = "CA_TDS"
    CA_SPQ = "CA_SPQ"
    CA_NHD = "CA_NHD"
    LEAD_DISCLOSURE = "LEAD_DISCLOSURE"
    HOA_PACKAGE = "HOA_PACKAGE"
    PRELIM_TITLE_REPORT = "PRELIM_TITLE_REPORT"
    SOLAR_AGREEMENT = "SOLAR_AGREEMENT"
    # Tier-1 additions from the listing-document research (2026-10-06):
    # present in virtually every California listing file.
    SELLER_ADVISORY = "SELLER_ADVISORY"
    AGENCY_DISCLOSURE = "AGENCY_DISCLOSURE"
    AGENT_VISUAL_INSPECTION = "AGENT_VISUAL_INSPECTION"
    WCMD_ADVISORY = "WCMD_ADVISORY"


class ReadinessVerdict(str, Enum):
    """File-level listing verdict.  Lives on ListingFile, not on documents.

    Deliberately distinct from document-level BusinessOutcome: a file can
    process perfectly (no FAILED, no NEEDS_REVIEW) and still be NOT_READY
    to list, and CONDITIONALLY_READY carries the gap plan (owner + due
    date) that makes a launch defensible.
    """

    READY = "READY"
    CONDITIONALLY_READY = "CONDITIONALLY_READY"
    NOT_READY = "NOT_READY"


class RequirementType(str, Enum):
    """Which layer of the requirement model a rule belongs to."""

    CORE = "CORE"
    JURISDICTIONAL = "JURISDICTIONAL"
    FEDERAL = "FEDERAL"
    PROPERTY_CONDITIONAL = "PROPERTY_CONDITIONAL"
    SELLER_CONDITIONAL = "SELLER_CONDITIONAL"
    TRANSACTION_CONDITIONAL = "TRANSACTION_CONDITIONAL"
    BROKERAGE = "BROKERAGE"
    OPTIONAL_SUPPORTING = "OPTIONAL_SUPPORTING"


class RequirementStatus(str, Enum):
    """How strongly a requirement applies.  Independent axis from type."""

    REQUIRED = "REQUIRED"
    CONDITIONALLY_REQUIRED = "CONDITIONALLY_REQUIRED"
    RECOMMENDED = "RECOMMENDED"
    OPTIONAL = "OPTIONAL"


class RequirementState(str, Enum):
    """Request-list lifecycle of one generated requirement."""

    PENDING = "PENDING"
    RECEIVED = "RECEIVED"
    VERIFIED = "VERIFIED"
    EXCEPTION = "EXCEPTION"


class EvidenceSource(str, Enum):
    """What kind of thing an Evidence row points at."""

    DOCUMENT = "DOCUMENT"
    OBSERVATION = "OBSERVATION"
    ATTESTATION = "ATTESTATION"
