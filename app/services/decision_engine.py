from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import BusinessOutcome, DocumentType


@dataclass(frozen=True)
class DecisionInput:
    applicable: bool
    document_type: DocumentType
    ocr_score: float
    fallback_used: bool
    duplicate: bool
    duplicate_determinate: bool
    customer_needs_review: bool
    contradiction: bool
    required_fields_valid: bool


@dataclass(frozen=True)
class Decision:
    outcome: BusinessOutcome
    reason: str


def decide(input_data: DecisionInput, *, acceptable_ocr_score: float = 0.90, fallback_min_score: float = 0.85) -> Decision:
    if not input_data.applicable or input_data.document_type == DocumentType.NOT_APPLICABLE:
        return Decision(BusinessOutcome.NOT_APPLICABLE, "document is not applicable")
    if input_data.contradiction:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "explicit contradiction overrides confidence")
    if input_data.customer_needs_review:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "customer identification is ambiguous")
    if input_data.document_type == DocumentType.UNKNOWN:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "document classification is unknown")
    if input_data.ocr_score < fallback_min_score:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "OCR quality is below V1 review threshold")
    if input_data.ocr_score < acceptable_ocr_score and not input_data.fallback_used:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "fallback extraction is required")
    if not input_data.required_fields_valid:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "required business fields are missing")
    if not input_data.duplicate_determinate:
        return Decision(BusinessOutcome.NEEDS_REVIEW, "duplicate determination is ambiguous")
    if input_data.duplicate:
        return Decision(BusinessOutcome.DUPLICATE, "matching V1 duplicate key already exists")
    return Decision(BusinessOutcome.READY_FOR_PROCESSING, "document passed V1 decision checks")
