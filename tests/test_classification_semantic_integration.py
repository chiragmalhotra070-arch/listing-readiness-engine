from __future__ import annotations

import pytest

from app.adapters.llm import LLMError, LLMRequest
from app.domain.enums import DocumentType
from app.schemas import LLMAnalysisResult
from app.services.classification import LLMDocumentClassifier
from app.services.semantic_fields import SemanticFieldResolver


class RawCreditNoteProvider:
    name = "mock-raw-provider"
    model = "mock-raw-model"

    def __init__(self, fields: dict[str, object]) -> None:
        self.fields = fields
        self.requests: list[LLMRequest] = []

    def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
        self.requests.append(request)
        return LLMAnalysisResult(
            document_type=DocumentType.CREDIT_NOTE,
            extracted_fields=self.fields,
            confidence=0.96,
            provider=self.name,
            model=self.model,
            prompt_version="test-v1",
            schema_version="test-v1",
            evidence={"source": "controlled raw provider"},
        )


def settings():
    return type("Settings", (), {"llm_schema_version": "v1"})()


def test_production_classifier_wires_raw_provider_to_semantic_llm_fallback_and_strict_schema():
    raw = {
        "credit_amt": 450.0,
        "credit_date": "2026-09-19",
        "credit_ref": "CN-INTEGRATION-001",
        "currency": "USD",
    }
    provider = RawCreditNoteProvider(raw)
    calls: list[dict[str, object]] = []

    def semantic_mapper(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        mappings = {
            "credit_amt": ("adjustment_amount", "credit amount maps to adjustment amount"),
            "credit_date": ("note_date", "credit date maps to note date"),
            "credit_ref": ("note_number", "credit reference maps to note number"),
        }
        target, evidence = mappings[str(kwargs["source_field"])]
        return {"canonical_field": target, "mapping_status": "UNAMBIGUOUS", "evidence": evidence}

    resolver = SemanticFieldResolver(semantic_mapper=semantic_mapper)
    result = LLMDocumentClassifier(settings(), provider=provider, semantic_resolver=resolver).classify(
        document_id=901,
        document_name="integration-credit-note.pdf",
        mime_type="application/pdf",
        extracted_text="Credit adjustment document context",
        extracted_data={},
    )

    assert provider.requests and provider.requests[0].metadata == {}
    assert set(provider.fields) == {"credit_amt", "credit_date", "credit_ref", "currency"}
    # credit_date is an existing document-type contextual mapping, so only the
    # genuinely unresolved raw fields reach the injected semantic mapper.
    assert len(calls) == 2
    assert {str(call["source_field"]) for call in calls} == {"credit_amt", "credit_ref"}
    for call in calls:
        assert call["document_type"] == DocumentType.CREDIT_NOTE
        assert call["document_text"] == "Credit adjustment document context"
        assert call["allowed_fields"] == sorted({"note_number", "note_date", "reference_invoice", "adjustment_amount", "tax_amount", "total_amount", "currency"})
    assert {str(call["source_value"]) for call in calls} == {"450.0", "CN-INTEGRATION-001"}

    assert result.extracted_data == {
        "note_number": "CN-INTEGRATION-001",
        "note_date": "2026-09-19",
        "adjustment_amount": 450.0,
        "currency": "USD",
    }
    assert not {"credit_amt", "credit_date", "credit_ref"} & set(result.extracted_data)
    evidence = result.raw_metadata["semantic_field_mappings"]
    assert len(evidence) == 4
    semantic_items = [item for item in evidence if item["mapping_method"] == "SEMANTIC_LLM"]
    assert {item["source_field"] for item in semantic_items} == {"credit_amt", "credit_ref"}
    for item in semantic_items:
        assert item["mapping_status"] == "UNAMBIGUOUS"
        assert item["canonical_field"] in {"adjustment_amount", "note_number"}
        assert item["evidence"]
    credit_date = next(item for item in evidence if item["source_field"] == "credit_date")
    assert credit_date["mapping_method"] == "DETERMINISTIC_ALIAS"
    assert credit_date["mapping_status"] == "MAPPED"
    currency = next(item for item in evidence if item["source_field"] == "currency")
    assert currency["mapping_method"] == "CANONICAL"


def test_production_classifier_routes_ambiguous_semantic_mapping_to_review_error():
    provider = RawCreditNoteProvider({"amount": 450.0, "currency": "USD"})
    calls: list[dict[str, object]] = []

    def semantic_mapper(**kwargs: object) -> dict[str, object]:
        calls.append(kwargs)
        return {"canonical_field": None, "mapping_status": "AMBIGUOUS", "evidence": "could represent adjustment_amount or total_amount"}

    classifier = LLMDocumentClassifier(settings(), provider=provider, semantic_resolver=SemanticFieldResolver(semantic_mapper=semantic_mapper))
    with pytest.raises(LLMError) as error:
        classifier.classify(document_id=902, document_name="ambiguous-credit-note.pdf", mime_type="application/pdf", extracted_text="Ambiguous credit note context", extracted_data={})

    assert len(calls) == 1
    assert calls[0]["source_field"] == "amount"
    assert error.value.needs_review is True
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    mapping = error.value.metadata["semantic_field_mappings"][0]
    assert mapping["source_field"] == "amount"
    assert mapping["canonical_field"] is None
    assert mapping["mapping_method"] == "SEMANTIC_LLM"
    assert mapping["mapping_status"] == "AMBIGUOUS"
    assert "adjustment_amount" in mapping["evidence"]
    assert "amount" in error.value.metadata.get("document_field_validation", {}).get("received_top_level_fields", [])
