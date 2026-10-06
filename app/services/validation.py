from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import DocumentType


REQUIRED_FIELDS: dict[DocumentType, tuple[str, ...]] = {
    DocumentType.REMITTANCE: ("remittance_number",),
    DocumentType.INVOICE: ("invoice_number",),
    DocumentType.PAYMENT_ADVICE: ("payment_reference",),
    DocumentType.CREDIT_NOTE: ("note_number",),
    DocumentType.DEBIT_NOTE: ("note_number",),
    DocumentType.BANK_STATEMENT: ("statement_start_date", "statement_end_date"),
}


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    missing_fields: tuple[str, ...]


def validate_required_fields(document_type: DocumentType, extracted_data: dict[str, Any]) -> ValidationResult:
    required = REQUIRED_FIELDS.get(document_type, ())
    missing = tuple(field for field in required if extracted_data.get(field) in (None, ""))
    return ValidationResult(not missing, missing)
