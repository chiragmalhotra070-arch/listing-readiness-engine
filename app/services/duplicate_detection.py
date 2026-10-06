from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.domain.enums import DocumentType


@dataclass(frozen=True)
class DuplicateKey:
    value: tuple[Any, ...] | None
    reason: str


def duplicate_key(document_type: DocumentType, customer_id: str | None, extracted_data: dict[str, Any] | None) -> DuplicateKey:
    if not customer_id:
        return DuplicateKey(None, "customer is required for duplicate detection")
    fields = {
        DocumentType.INVOICE: ("invoice_number",),
        DocumentType.REMITTANCE: ("remittance_number",),
        DocumentType.PAYMENT_ADVICE: ("payment_reference",),
        DocumentType.CREDIT_NOTE: ("note_number",),
        DocumentType.DEBIT_NOTE: ("note_number",),
        DocumentType.BANK_STATEMENT: ("statement_start_date", "statement_end_date"),
    }.get(document_type)
    if not fields:
        return DuplicateKey(None, "document type has no V1 duplicate key")
    data = extracted_data or {}
    values = tuple(data.get(field) for field in fields)
    if any(value in (None, "") for value in values):
        return DuplicateKey(None, "primary business identifier is missing")
    return DuplicateKey((customer_id, *values), "duplicate key derived from V1 rule")
