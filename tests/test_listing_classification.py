"""Slice 3: classifier taxonomy swap -- listing types end to end.

Each listing mock profile classifies through the full classify() path
(LLM -> semantic resolution -> schema validation), parser field hints map
to listing types in the mock fallback, ambiguous shared fields route to
human review instead of misclassifying, and the v2 prompt carries the
listing taxonomy.
"""

from __future__ import annotations

import pytest

from app.adapters.llm import LLMError, LLMRequest, OpenAICompatibleLLMProvider
from app.config import Settings
from app.domain.enums import DocumentType
from app.services.classification import LLMDocumentClassifier


def _settings(**overrides):
    base = {
        "llm_provider": "mock",
        "llm_model": "mock-v1",
        "llm_prompt_version": "v2",
        "llm_schema_version": "v1",
    }
    base.update(overrides)
    return type("Settings", (), base)()


LISTING_PROFILE_CASES = [
    ("listing_agreement", DocumentType.LISTING_AGREEMENT),
    ("seller_advisory", DocumentType.SELLER_ADVISORY),
    ("agency_disclosure", DocumentType.AGENCY_DISCLOSURE),
    ("ca_tds", DocumentType.CA_TDS),
    ("ca_spq", DocumentType.CA_SPQ),
    ("agent_visual_inspection", DocumentType.AGENT_VISUAL_INSPECTION),
    ("ca_nhd", DocumentType.CA_NHD),
    ("wcmd_advisory", DocumentType.WCMD_ADVISORY),
    ("lead_disclosure", DocumentType.LEAD_DISCLOSURE),
    ("hoa_package", DocumentType.HOA_PACKAGE),
    ("prelim_title_report", DocumentType.PRELIM_TITLE_REPORT),
    ("solar_agreement", DocumentType.SOLAR_AGREEMENT),
]


def test_prompt_version_bumped_for_listing_taxonomy():
    assert Settings().llm_prompt_version == "v2"


@pytest.mark.parametrize("profile,doc_type", LISTING_PROFILE_CASES)
def test_listing_mock_profile_classifies_end_to_end(profile, doc_type):
    classifier = LLMDocumentClassifier(_settings())
    result = classifier.classify(
        document_id=1,
        document_name=f"{profile}.pdf",
        mime_type="application/pdf",
        extracted_text="",
        extracted_data={},
        mock_profile=profile,
    )
    assert result.document_type is doc_type
    assert result.prompt_version == "v2"
    assert 0.0 <= result.confidence <= 1.0
    # The shared base vocabulary survives the full pipeline.
    assert result.extracted_data["apn"] == "5842-018-024"
    assert result.extracted_data["property_address"] == "123 Main St, Pasadena, CA 91101"
    assert result.customer_candidates.customer_id_candidates == ["CUST-001"]


def test_listing_profile_type_delta_reaches_extracted_data():
    classifier = LLMDocumentClassifier(_settings())
    result = classifier.classify(
        document_id=1,
        document_name="listing_agreement.pdf",
        mime_type="application/pdf",
        extracted_text="",
        extracted_data={},
        mock_profile="listing_agreement",
    )
    assert result.extracted_data["listing_price"] == 1250000.0
    assert result.extracted_data["signatures_present"] is True


def test_parser_field_hint_fallback_detects_listing_agreement():
    classifier = LLMDocumentClassifier(_settings())
    result = classifier.classify(
        document_id=1,
        document_name="scan.pdf",
        mime_type="application/pdf",
        extracted_text="",
        extracted_data={"listing_price": 999000.0, "apn": "5842-018-024"},
    )
    assert result.document_type is DocumentType.LISTING_AGREEMENT
    assert result.extracted_data["listing_price"] == 999000.0


def test_ambiguous_shared_fields_route_to_review_not_misclassification():
    classifier = LLMDocumentClassifier(_settings())
    with pytest.raises(LLMError) as exc_info:
        classifier.classify(
            document_id=1,
            document_name="scan.pdf",
            mime_type="application/pdf",
            extracted_text="",
            extracted_data={"apn": "5842-018-024", "seller_name": "Jane Seller"},
        )
    assert exc_info.value.needs_review


def test_system_prompt_carries_listing_taxonomy():
    provider = OpenAICompatibleLLMProvider(
        base_url="https://example.com",
        api_key="x",
        model="m",
        timeout_seconds=2,
        max_tokens=200,
        temperature=0,
        prompt_version="v2",
        schema_version="v1",
    )
    request = LLMRequest(document_name="x.pdf", mime_type="application/pdf", extracted_text="", extracted_data={})
    system_prompt, _ = provider._prompts(request)
    assert "listing document extraction and classification" in system_prompt
    assert "financial document extraction" not in system_prompt
    for doc_type in (
        "LISTING_AGREEMENT",
        "SELLER_ADVISORY",
        "AGENCY_DISCLOSURE",
        "CA_TDS",
        "CA_SPQ",
        "AGENT_VISUAL_INSPECTION",
        "CA_NHD",
        "WCMD_ADVISORY",
        "LEAD_DISCLOSURE",
        "HOA_PACKAGE",
        "PRELIM_TITLE_REPORT",
        "SOLAR_AGREEMENT",
    ):
        assert doc_type in system_prompt
