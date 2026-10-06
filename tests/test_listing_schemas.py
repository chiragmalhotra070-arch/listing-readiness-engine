"""Slice 2: listing extraction schemas -- one base, small per-type deltas.

Covers the Tier-1 additions from the listing-document research (2026-10-06):
every listing document type inherits the shared base vocabulary, the
classifier maps each type to its schema, the semantic resolver applies the
shared aliases, and unresolvable fields route to human review.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.adapters.llm import LLMError
from app.domain.enums import DocumentType
from app.schemas import (
    AgentVisualInspectionFields,
    AgencyDisclosureFields,
    HOAPackageFields,
    LeadDisclosureFields,
    ListingAgreementFields,
    ListingDocumentFields,
    NHDFields,
    PrelimTitleFields,
    SellerAdvisoryFields,
    SolarAgreementFields,
    SPQFields,
    TDSFields,
    WCMDFields,
)
from app.services.classification import LLMDocumentClassifier
from app.services.semantic_fields import SemanticFieldResolver

BASE_FIELDS = {
    "property_address",
    "apn",
    "seller_name",
    "document_date",
    "listing_agent_name",
    "brokerage_name",
    "signatures_present",
}

LISTING_SCHEMA_CASES = [
    (DocumentType.LISTING_AGREEMENT, ListingAgreementFields),
    (DocumentType.SELLER_ADVISORY, SellerAdvisoryFields),
    (DocumentType.AGENCY_DISCLOSURE, AgencyDisclosureFields),
    (DocumentType.CA_TDS, TDSFields),
    (DocumentType.CA_SPQ, SPQFields),
    (DocumentType.AGENT_VISUAL_INSPECTION, AgentVisualInspectionFields),
    (DocumentType.CA_NHD, NHDFields),
    (DocumentType.WCMD_ADVISORY, WCMDFields),
    (DocumentType.LEAD_DISCLOSURE, LeadDisclosureFields),
    (DocumentType.HOA_PACKAGE, HOAPackageFields),
    (DocumentType.PRELIM_TITLE_REPORT, PrelimTitleFields),
    (DocumentType.SOLAR_AGREEMENT, SolarAgreementFields),
]


def test_base_schema_has_standardized_fields():
    assert set(ListingDocumentFields.model_fields) == BASE_FIELDS


@pytest.mark.parametrize("doc_type,schema", LISTING_SCHEMA_CASES)
def test_every_listing_type_inherits_base_fields(doc_type, schema):
    assert BASE_FIELDS <= set(schema.model_fields), schema.__name__


@pytest.mark.parametrize("doc_type,schema", LISTING_SCHEMA_CASES)
def test_classifier_maps_every_listing_type_to_its_schema(doc_type, schema):
    assert LLMDocumentClassifier._schema_for(doc_type) is schema


def test_extra_fields_forbidden_on_listing_schemas():
    with pytest.raises(ValidationError):
        TDSFields(property_address="123 Main St", nonsense_field="x")


def test_listing_agreement_delta_fields():
    fields = ListingAgreementFields(listing_price=1_250_000.0, signatures_present=True)
    assert fields.listing_price == 1_250_000.0
    assert fields.signatures_present is True
    assert fields.property_address is None


def test_solar_ownership_type_is_free_text_for_now():
    fields = SolarAgreementFields(ownership_type="leased", provider_name="SunRun")
    assert fields.ownership_type == "leased"
    assert fields.provider_name == "SunRun"


def test_shared_aliases_resolve_to_base_fields():
    resolver = SemanticFieldResolver()
    canonical, _supplemental, mappings = resolver.resolve(
        document_type=DocumentType.CA_TDS,
        raw_fields={
            "parcel_number": "1234-567-890",
            "seller": "Jane Seller",
            "disclosure_signed": True,
        },
        supplemental_information=[],
        document_text="",
        canonical_fields=set(TDSFields.model_fields),
    )
    assert canonical["apn"] == "1234-567-890"
    assert canonical["seller_name"] == "Jane Seller"
    assert canonical["disclosure_signed"] is True
    assert all(m["mapping_status"] == "MAPPED" for m in mappings)


def test_shared_aliases_apply_to_every_listing_type():
    resolver = SemanticFieldResolver()
    for doc_type, schema in LISTING_SCHEMA_CASES:
        canonical, _, _ = resolver.resolve(
            document_type=doc_type,
            raw_fields={"apn_number": "999-000-111"},
            supplemental_information=[],
            document_text="",
            canonical_fields=set(schema.model_fields),
        )
        assert canonical["apn"] == "999-000-111", doc_type


def test_unresolvable_listing_field_routes_to_review():
    resolver = SemanticFieldResolver()
    with pytest.raises(LLMError) as exc_info:
        resolver.resolve(
            document_type=DocumentType.CA_TDS,
            raw_fields={"zzz_not_a_field": "x"},
            supplemental_information=[],
            document_text="",
            canonical_fields=set(TDSFields.model_fields),
        )
    assert exc_info.value.needs_review
    assert (
        exc_info.value.metadata["document_field_validation"]["schema"] == "TDSFields"
    )
