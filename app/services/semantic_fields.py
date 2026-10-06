from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.domain.enums import DocumentType
from app.adapters.llm import LLMError


@dataclass(frozen=True)
class FieldMapping:
    source_field: str
    source_value: Any
    canonical_field: str | None
    mapping_method: str
    mapping_status: str
    evidence: str


ALIASES: dict[DocumentType, dict[str, str]] = {
    DocumentType.INVOICE: {"subtotal_amount": "subtotal", "tax": "tax_amount", "sales_tax": "tax_amount"},
    DocumentType.CREDIT_NOTE: {
        "credit_note_number": "note_number",
        "credit_note_date": "note_date",
        "credit_date": "note_date",
    },
    DocumentType.DEBIT_NOTE: {
        "debit_note_number": "note_number",
        "debit_note_date": "note_date",
        "debit_date": "note_date",
    },
}

SUPPLEMENTAL_FIELDS: dict[DocumentType, set[str]] = {
    DocumentType.BANK_STATEMENT: {"statement_number"},
}


#: Listing document types share one base extraction vocabulary, so their
#: deterministic aliases are shared too.  Per-type aliases can extend these
#: later.
LISTING_ALIASES: dict[str, str] = {
    "property_address_line": "property_address",
    "assessor_parcel_number": "apn",
    "parcel_number": "apn",
    "apn_number": "apn",
    "seller": "seller_name",
    "owner_name": "seller_name",
    "doc_date": "document_date",
    "date_of_document": "document_date",
    "agent_name": "listing_agent_name",
    "brokerage": "brokerage_name",
    "signed": "signatures_present",
    "is_signed": "signatures_present",
}

LISTING_DOCUMENT_TYPES: tuple[DocumentType, ...] = (
    DocumentType.LISTING_AGREEMENT,
    DocumentType.SELLER_ADVISORY,
    DocumentType.AGENCY_DISCLOSURE,
    DocumentType.CA_TDS,
    DocumentType.CA_SPQ,
    DocumentType.AGENT_VISUAL_INSPECTION,
    DocumentType.CA_NHD,
    DocumentType.WCMD_ADVISORY,
    DocumentType.LEAD_DISCLOSURE,
    DocumentType.HOA_PACKAGE,
    DocumentType.PRELIM_TITLE_REPORT,
    DocumentType.SOLAR_AGREEMENT,
)

for _listing_document_type in LISTING_DOCUMENT_TYPES:
    ALIASES.setdefault(_listing_document_type, {}).update(LISTING_ALIASES)

#: Diagnostic schema names used in semantic-resolution metadata.
_SCHEMA_NAMES: dict[DocumentType, str] = {
    DocumentType.REMITTANCE: "RemittanceFields",
    DocumentType.INVOICE: "InvoiceFields",
    DocumentType.PAYMENT_ADVICE: "PaymentAdviceFields",
    DocumentType.CREDIT_NOTE: "NoteFields",
    DocumentType.DEBIT_NOTE: "NoteFields",
    DocumentType.BANK_STATEMENT: "BankStatementFields",
    DocumentType.LISTING_AGREEMENT: "ListingAgreementFields",
    DocumentType.SELLER_ADVISORY: "SellerAdvisoryFields",
    DocumentType.AGENCY_DISCLOSURE: "AgencyDisclosureFields",
    DocumentType.CA_TDS: "TDSFields",
    DocumentType.CA_SPQ: "SPQFields",
    DocumentType.AGENT_VISUAL_INSPECTION: "AgentVisualInspectionFields",
    DocumentType.CA_NHD: "NHDFields",
    DocumentType.WCMD_ADVISORY: "WCMDFields",
    DocumentType.LEAD_DISCLOSURE: "LeadDisclosureFields",
    DocumentType.HOA_PACKAGE: "HOAPackageFields",
    DocumentType.PRELIM_TITLE_REPORT: "PrelimTitleFields",
    DocumentType.SOLAR_AGREEMENT: "SolarAgreementFields",
}


class SemanticFieldResolver:
    def __init__(self, semantic_mapper: Callable[..., dict[str, Any] | None] | None = None) -> None:
        self.semantic_mapper = semantic_mapper

    @staticmethod
    def _contextual_mapping(document_type: DocumentType, source_field: str, source_value: Any, canonical_fields: set[str]) -> dict[str, Any] | None:
        normalized = source_field.casefold().replace(" ", "_")
        candidates = {
            DocumentType.CREDIT_NOTE: {"credit_amount": "adjustment_amount", "credit_date": "note_date"},
            DocumentType.DEBIT_NOTE: {"debit_amount": "adjustment_amount", "debit_date": "note_date"},
            DocumentType.INVOICE: {},
            DocumentType.REMITTANCE: {},
            DocumentType.PAYMENT_ADVICE: {},
            DocumentType.BANK_STATEMENT: {},
        }.get(document_type, {})
        if normalized in candidates and candidates[normalized] in canonical_fields:
            return {"canonical_field": candidates[normalized], "mapping_status": "UNAMBIGUOUS", "evidence": "document-type-specific semantic field mapping"}
        if normalized == "amount":
            return {"canonical_field": None, "mapping_status": "AMBIGUOUS", "evidence": "amount has multiple possible canonical meanings"}
        return None

    def resolve(
        self,
        *,
        document_type: DocumentType,
        raw_fields: dict[str, Any],
        supplemental_information: list[dict[str, Any]],
        document_text: str,
        canonical_fields: set[str],
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
        canonical: dict[str, Any] = {}
        supplemental = list(supplemental_information)
        mappings: list[FieldMapping] = []
        for source_field, source_value in raw_fields.items():
            if source_field in canonical_fields:
                target, method, status, evidence = source_field, "CANONICAL", "MAPPED", "field name is canonical"
            elif source_field in ALIASES.get(document_type, {}):
                target, method, status, evidence = ALIASES[document_type][source_field], "DETERMINISTIC_ALIAS", "MAPPED", "explicit document-type alias"
            elif source_field in SUPPLEMENTAL_FIELDS.get(document_type, set()):
                target, method, status, evidence = None, "SUPPLEMENTAL", "SUPPLEMENTAL", "useful noncanonical document information"
            else:
                proposal = self.semantic_mapper(document_type=document_type, source_field=source_field, source_value=source_value, document_text=document_text, allowed_fields=sorted(canonical_fields)) if self.semantic_mapper else self._contextual_mapping(document_type, source_field, source_value, canonical_fields)
                proposal = proposal if isinstance(proposal, dict) else {}
                target = proposal.get("canonical_field")
                status = proposal.get("mapping_status", "UNMAPPED")
                evidence = str(proposal.get("evidence", "no unambiguous canonical mapping"))[:500]
                method = "SEMANTIC_LLM" if self.semantic_mapper else "SEMANTIC_LLM"
                if status not in {"UNAMBIGUOUS", "AMBIGUOUS", "SUPPLEMENTAL", "UNMAPPED"}:
                    status = "UNMAPPED"
                if status == "UNAMBIGUOUS" and target not in canonical_fields:
                    target, status = None, "UNMAPPED"
                    evidence = "semantic mapper returned a field outside the allowed canonical vocabulary"
                elif status != "UNAMBIGUOUS":
                    target = None
            mapping = FieldMapping(source_field, source_value, target, method, status, evidence)
            mappings.append(mapping)
            if target is None:
                if status == "SUPPLEMENTAL":
                    supplemental.append({"name": source_field, "value": source_value, "source": "document"})
                continue
            if target in canonical:
                if canonical[target] != source_value:
                    mappings.append(FieldMapping(source_field, source_value, None, method, "AMBIGUOUS", f"conflicts with another source value for {target}"))
                    canonical.pop(target, None)
                continue
            canonical[target] = source_value
        unresolved = [m for m in mappings if m.mapping_status in {"UNMAPPED", "AMBIGUOUS"}]
        if unresolved:
            metadata = {"semantic_field_mappings": [{"source_field": m.source_field, "canonical_field": m.canonical_field, "mapping_method": m.mapping_method, "mapping_status": m.mapping_status, "evidence": m.evidence} for m in mappings], "document_field_validation": {"schema": _SCHEMA_NAMES.get(document_type, "UnknownFields"), "received_top_level_fields": sorted(raw_fields), "received_field_types": {str(k): type(v).__name__ for k, v in raw_fields.items()}, "validation_errors": [{"field_path": [m.source_field], "error_type": "ambiguous" if m.mapping_status == "AMBIGUOUS" else "extra_forbidden", "expected": None, "received_type": type(m.source_value).__name__} for m in unresolved] + [{"field_path": [name], "error_type": "extra_forbidden", "expected": None, "received_type": type(value).__name__} for name, value in raw_fields.items() if name not in {m.source_field for m in unresolved}]}}
            for item in unresolved:
                if item.source_field in {"tax", "sales_tax"} and item.canonical_field is None:
                    metadata["canonical_alias_conflict"] = {"alias": item.source_field, "canonical": "tax_amount"}
                    break
            raise LLMError("LLM extracted fields could not be semantically resolved", code="LLM_SCHEMA_VALIDATION_ERROR", retryable=False, needs_review=True, metadata=metadata)
        return canonical, supplemental, [mapping.__dict__ for mapping in mappings]
