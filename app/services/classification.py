from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.adapters.llm import LLMError, LLMRequest, LLMProvider, build_llm_provider
from app.config import Settings
from app.domain.enums import DocumentType
from app.schemas import (
    AgentVisualInspectionFields,
    AgencyDisclosureFields,
    BankStatementFields,
    CustomerCandidates,
    HOAPackageFields,
    InvoiceFields,
    LeadDisclosureFields,
    ListingAgreementFields,
    NHDFields,
    NoteFields,
    PaymentAdviceFields,
    PrelimTitleFields,
    RemittanceFields,
    SellerAdvisoryFields,
    SolarAgreementFields,
    SPQFields,
    TDSFields,
    WCMDFields,
)
from app.services.semantic_fields import SemanticFieldResolver


@dataclass(frozen=True)
class ClassificationResult:
    document_type: DocumentType
    confidence: float
    normalized_confidence: float
    applicable: bool
    evidence: dict[str, Any]
    assessment: dict[str, Any] | None
    extracted_data: dict[str, Any]
    supplemental_information: list[dict[str, Any]]
    customer_candidates: CustomerCandidates
    provider: str
    model: str
    prompt_version: str
    schema_version: str
    provider_request_id: str | None
    warnings: list[str]
    ambiguities: list[str]
    raw_metadata: dict[str, Any]


class DocumentClassifier:
    def classify(self, **kwargs: Any) -> ClassificationResult:
        raise NotImplementedError


class LLMDocumentClassifier(DocumentClassifier):
    def __init__(self, settings: Settings, *, provider: LLMProvider | None = None, semantic_resolver: SemanticFieldResolver | None = None) -> None:
        self.provider = provider or build_llm_provider(settings)
        self.schema_version = settings.llm_schema_version
        self.semantic_resolver = semantic_resolver or SemanticFieldResolver(semantic_mapper=getattr(self.provider, "map_semantic_field", None))

    def classify(self, *, document_id: int | None, document_name: str, mime_type: str, extracted_text: str, extracted_data: dict[str, Any], parser_metadata: dict[str, Any] | None = None, mock_profile: str | None = None) -> ClassificationResult:
        request = LLMRequest(document_name=document_name, mime_type=mime_type, extracted_text=extracted_text, extracted_data=extracted_data, metadata={**(parser_metadata or {}), "mock_profile": mock_profile} if mock_profile else (parser_metadata or {}), requested_extraction_schema=self.schema_version)
        result = self.provider.analyze(request)
        fields = dict(result.extracted_fields)
        schema = self._schema_for(result.document_type)
        canonical_fields = set(schema.model_fields) if schema else set()
        try:
            fields, supplemental_information, mapping_evidence = self.semantic_resolver.resolve(document_type=result.document_type, raw_fields=fields, supplemental_information=[item.model_dump() for item in result.supplemental_information], document_text=extracted_text, canonical_fields=canonical_fields)
        except LLMError as error:
            error.metadata = {"provider": result.provider, "model": result.model, "provider_request_id": result.provider_request_id, **result.raw_metadata, **error.metadata}
            raise
        self._validate_document_fields(result.document_type, fields, provider_metadata={"provider": result.provider, "model": result.model, "provider_request_id": result.provider_request_id, **result.raw_metadata, "semantic_field_mappings": mapping_evidence})
        normalized_confidence = max(0.0, min(1.0, result.confidence))
        supplemental_information = [item for item in supplemental_information if item["name"] not in canonical_fields]
        return ClassificationResult(document_type=result.document_type, confidence=result.confidence, normalized_confidence=normalized_confidence, applicable=result.document_type != DocumentType.NOT_APPLICABLE, evidence=result.evidence, assessment=result.assessment.model_dump() if result.assessment is not None else None, extracted_data=fields, supplemental_information=supplemental_information, customer_candidates=result.customer_candidates, provider=result.provider, model=result.model, prompt_version=result.prompt_version, schema_version=result.schema_version, provider_request_id=result.provider_request_id, warnings=result.warnings, ambiguities=result.ambiguities, raw_metadata={**result.raw_metadata, "semantic_field_mappings": mapping_evidence})

    @staticmethod
    def _known_supplemental_names(document_type: DocumentType) -> tuple[str, ...]:
        if document_type == DocumentType.INVOICE:
            return ("payment_terms", "order_number", "order_date")
        return ()

    @staticmethod
    def _schema_for(document_type: DocumentType):
        return {
            DocumentType.REMITTANCE: RemittanceFields,
            DocumentType.INVOICE: InvoiceFields,
            DocumentType.PAYMENT_ADVICE: PaymentAdviceFields,
            DocumentType.CREDIT_NOTE: NoteFields,
            DocumentType.DEBIT_NOTE: NoteFields,
            DocumentType.BANK_STATEMENT: BankStatementFields,
            DocumentType.LISTING_AGREEMENT: ListingAgreementFields,
            DocumentType.SELLER_ADVISORY: SellerAdvisoryFields,
            DocumentType.AGENCY_DISCLOSURE: AgencyDisclosureFields,
            DocumentType.CA_TDS: TDSFields,
            DocumentType.CA_SPQ: SPQFields,
            DocumentType.AGENT_VISUAL_INSPECTION: AgentVisualInspectionFields,
            DocumentType.CA_NHD: NHDFields,
            DocumentType.WCMD_ADVISORY: WCMDFields,
            DocumentType.LEAD_DISCLOSURE: LeadDisclosureFields,
            DocumentType.HOA_PACKAGE: HOAPackageFields,
            DocumentType.PRELIM_TITLE_REPORT: PrelimTitleFields,
            DocumentType.SOLAR_AGREEMENT: SolarAgreementFields,
        }.get(document_type)

    @classmethod
    def _validate_document_fields(cls, document_type: DocumentType, fields: dict[str, Any], *, provider_metadata: dict[str, Any] | None = None) -> None:
        schema = cls._schema_for(document_type)
        if schema is None:
            return
        try:
            schema.model_validate(fields)
        except ValidationError as error:
            diagnostics = {
                "schema": schema.__name__,
                "expected_fields": {name: str(field.annotation) for name, field in schema.model_fields.items()},
                "received_top_level_fields": sorted(str(name) for name in fields),
                "received_field_types": {str(name): type(value).__name__ for name, value in fields.items()},
                "validation_errors": [
                    {
                        "field_path": [str(part) for part in item.get("loc", ())],
                        "error_type": item.get("type"),
                        "expected": (item.get("ctx") or {}).get("expected") if isinstance((item.get("ctx") or {}).get("expected"), str) else None,
                        "received_type": type(item.get("input")).__name__ if "input" in item else None,
                    }
                    for item in error.errors()
                ],
            }
            raise LLMError("LLM extracted fields failed document schema validation", code="LLM_SCHEMA_VALIDATION_ERROR", retryable=False, needs_review=True, metadata={**(provider_metadata or {}), "document_field_validation": diagnostics}) from error
