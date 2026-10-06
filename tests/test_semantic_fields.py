import pytest

from app.adapters.llm import LLMError
from app.domain.enums import DocumentType
from app.services.semantic_fields import SemanticFieldResolver


def resolve(fields, mapper):
    return SemanticFieldResolver(semantic_mapper=mapper).resolve(document_type=DocumentType.CREDIT_NOTE, raw_fields=fields, supplemental_information=[], document_text="Credit note context", canonical_fields={"note_number", "note_date", "adjustment_amount", "total_amount", "currency"})


def test_semantic_llm_unambiguous_mapping():
    c, _, m = resolve({"credit_amt": 450}, lambda **_: {"canonical_field": "adjustment_amount", "mapping_status": "UNAMBIGUOUS", "evidence": "credit amount"})
    assert c["adjustment_amount"] == 450
    assert m[0]["mapping_method"] == "SEMANTIC_LLM"
    assert m[0]["mapping_status"] == "UNAMBIGUOUS"


def test_semantic_llm_ambiguity_is_not_mapped():
    with pytest.raises(LLMError) as error:
        resolve({"amount": 450}, lambda **_: {"canonical_field": None, "mapping_status": "AMBIGUOUS", "evidence": "two possible destinations"})
    mapping = error.value.metadata["semantic_field_mappings"][0]
    assert mapping["mapping_status"] == "AMBIGUOUS" and mapping["canonical_field"] is None


def test_semantic_llm_unknown_is_not_mapped():
    with pytest.raises(LLMError) as error:
        resolve({"unknown_thing": "x"}, lambda **_: {"canonical_field": None, "mapping_status": "UNMAPPED", "evidence": "irrelevant"})
    assert error.value.metadata["semantic_field_mappings"][0]["mapping_status"] == "UNMAPPED"


def test_semantic_llm_cannot_invent_field():
    with pytest.raises(LLMError) as error:
        resolve({"credit_amt": 450}, lambda **_: {"canonical_field": "invented_field", "mapping_status": "UNAMBIGUOUS", "evidence": "invalid"})
    mapping = error.value.metadata["semantic_field_mappings"][0]
    assert mapping["mapping_status"] == "UNMAPPED" and mapping["canonical_field"] is None


def test_semantic_mapper_is_not_called_for_deterministic_fields():
    calls = []
    def mapper(**kwargs):
        calls.append(kwargs["source_field"])
        return {"canonical_field": "subtotal", "mapping_status": "UNAMBIGUOUS", "evidence": "should not run"}
    SemanticFieldResolver(semantic_mapper=mapper).resolve(document_type=DocumentType.INVOICE, raw_fields={"subtotal_amount": 10}, supplemental_information=[], document_text="", canonical_fields={"subtotal"})
    assert calls == []
