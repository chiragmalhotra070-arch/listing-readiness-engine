"""Slice 5: reconciliation -- documents matched to requirements.

Covers ``reconcile_listing_file``: evidence creation, the PENDING ->
RECEIVED transition, idempotent re-runs, and the unmatched/unmet reports.
Runs on SQLite like the rest of the suite (same ``db`` fixture pattern as
``test_listing_domain.py``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Document, Evidence, ListingFile, Requirement
from app.domain.enums import (
    DocumentType,
    EvidenceSource,
    RequirementState,
    RequirementStatus,
    RequirementType,
)
from app.services.reconciliation import ReconciliationResult, reconcile_listing_file
from app.services.requirement_catalog import ensure_ca_catalog
from app.services.requirement_engine import RequirementEngine


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


CA_1968_HOA_SOLAR = {
    "state": "CA",
    "year_built": 1968,
    "property_type": "single_family",
    "hoa": True,
    "solar": True,
    "septic": False,
    "seller_type": "individual",
}

KEY_TO_DOCUMENT_TYPE = {
    "listing_agreement": DocumentType.LISTING_AGREEMENT,
    "seller_advisory": DocumentType.SELLER_ADVISORY,
    "agency_disclosure": DocumentType.AGENCY_DISCLOSURE,
    "ca_tds": DocumentType.CA_TDS,
    "ca_spq": DocumentType.CA_SPQ,
    "agent_visual_inspection": DocumentType.AGENT_VISUAL_INSPECTION,
    "ca_nhd": DocumentType.CA_NHD,
    "wcmd_advisory": DocumentType.WCMD_ADVISORY,
    "lead_disclosure": DocumentType.LEAD_DISCLOSURE,
    "hoa_package": DocumentType.HOA_PACKAGE,
    "solar_agreement": DocumentType.SOLAR_AGREEMENT,
    "prelim_title_report": DocumentType.PRELIM_TITLE_REPORT,
}
ALL_KEYS = set(KEY_TO_DOCUMENT_TYPE)


def _file(db, **facts) -> ListingFile:
    listing_file = ListingFile(
        property_address="123 Main St, Pasadena, CA 91101",
        apn="5842-018-024",
        seller_name="Jane Seller",
        property_attributes=facts,
    )
    db.add(listing_file)
    db.flush()
    return listing_file


def _doc(db, listing_file, document_type, *, confidence: float = 0.97) -> Document:
    document = Document(
        listing_file_id=listing_file.id,
        document_name=f"{document_type.value.lower()}.pdf",
        mime_type="application/pdf",
        document_type=document_type,
        classification_confidence=confidence,
        current_stage="CLASSIFICATION",
    )
    db.add(document)
    db.flush()
    return document


def _generate(db, listing_file) -> list[Requirement]:
    ensure_ca_catalog(db)
    requirements = RequirementEngine().generate(db, listing_file)
    db.flush()
    return requirements


def _requirement(db, listing_file, requirement_key: str) -> Requirement:
    return (
        db.query(Requirement)
        .filter_by(listing_file_id=listing_file.id, requirement_key=requirement_key)
        .one()
    )


def _states(db, listing_file) -> dict[str, str]:
    return {
        requirement.requirement_key: requirement.state
        for requirement in db.query(Requirement).filter_by(listing_file_id=listing_file.id)
    }


def test_single_match_creates_evidence_and_receives(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    document = _doc(db, listing_file, DocumentType.CA_TDS, confidence=0.93)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert result.requirements_moved_to_received == ["ca_tds"]
    assert len(result.unmet_requirement_keys) == 11
    assert "ca_tds" not in result.unmet_requirement_keys
    assert result.unmatched_document_ids == []

    evidence = db.query(Evidence).one()
    assert evidence.source == EvidenceSource.DOCUMENT.value
    assert evidence.confidence == 0.93
    assert evidence.verified is False
    assert evidence.document_id == document.id
    assert evidence.detail == {"document_type": "CA_TDS", "matched_via": "satisfied_by"}

    requirement = _requirement(db, listing_file, "ca_tds")
    assert requirement.state == RequirementState.RECEIVED.value
    assert f"#{document.id}" in requirement.state_reason
    assert "CA_TDS" in requirement.state_reason


def test_second_run_is_idempotent(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _doc(db, listing_file, DocumentType.CA_TDS)
    assert reconcile_listing_file(db, listing_file).evidence_created == 1

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 0
    assert result.requirements_moved_to_received == []
    assert db.query(Evidence).count() == 1
    assert _requirement(db, listing_file, "ca_tds").state == RequirementState.RECEIVED.value
    assert len(result.unmet_requirement_keys) == 11
    assert result.unmatched_document_ids == []


def test_unknown_and_unlisted_types_never_match(db):
    """Neither type creates evidence; both are reported for review."""
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    unknown = _doc(db, listing_file, DocumentType.UNKNOWN, confidence=0.31)
    invoice = _doc(db, listing_file, DocumentType.INVOICE, confidence=0.88)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 0
    assert db.query(Evidence).filter(
        Evidence.document_id.in_([unknown.id, invoice.id])
    ).count() == 0
    assert db.query(Evidence).count() == 0
    assert result.requirements_moved_to_received == []
    assert all(state == RequirementState.PENDING.value for state in _states(db, listing_file).values())
    assert len(result.unmet_requirement_keys) == 12
    assert sorted(result.unmatched_document_ids) == sorted([unknown.id, invoice.id])


def test_two_documents_same_type_two_evidence_one_transition(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    first = _doc(db, listing_file, DocumentType.CA_TDS)
    second = _doc(db, listing_file, DocumentType.CA_TDS, confidence=0.72)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 2
    assert db.query(Evidence).count() == 2
    assert sorted(evidence.confidence for evidence in db.query(Evidence).all()) == [0.72, 0.97]
    assert {evidence.document_id for evidence in db.query(Evidence).all()} == {first.id, second.id}
    assert result.requirements_moved_to_received == ["ca_tds"]
    requirement = _requirement(db, listing_file, "ca_tds")
    assert requirement.state == RequirementState.RECEIVED.value
    assert f"#{first.id}" in requirement.state_reason
    assert result.unmatched_document_ids == []


def test_full_ca_file_all_twelve_received(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    for document_type in KEY_TO_DOCUMENT_TYPE.values():
        _doc(db, listing_file, document_type)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 12
    assert db.query(Evidence).count() == 12
    assert sorted(result.requirements_moved_to_received) == sorted(ALL_KEYS)
    assert result.unmet_requirement_keys == []
    assert result.unmatched_document_ids == []
    assert all(state == RequirementState.RECEIVED.value for state in _states(db, listing_file).values())


def test_partial_documents_leave_nine_unmet(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    for document_type in (DocumentType.LISTING_AGREEMENT, DocumentType.CA_TDS, DocumentType.CA_NHD):
        _doc(db, listing_file, document_type)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 3
    assert sorted(result.requirements_moved_to_received) == ["ca_nhd", "ca_tds", "listing_agreement"]
    assert len(result.unmet_requirement_keys) == 9
    assert set(result.requirements_moved_to_received) | set(result.unmet_requirement_keys) == ALL_KEYS
    assert result.unmatched_document_ids == []


def test_already_received_gains_evidence_without_second_transition(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _doc(db, listing_file, DocumentType.CA_TDS)
    reconcile_listing_file(db, listing_file)

    _doc(db, listing_file, DocumentType.CA_TDS, confidence=0.64)
    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert db.query(Evidence).count() == 2
    assert result.requirements_moved_to_received == []
    assert _requirement(db, listing_file, "ca_tds").state == RequirementState.RECEIVED.value
    assert "ca_tds" not in result.unmet_requirement_keys


def test_file_without_requirements_returns_empty_result(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _doc(db, listing_file, DocumentType.CA_TDS)

    result = reconcile_listing_file(db, listing_file)

    assert result == ReconciliationResult()
    assert db.query(Requirement).count() == 0
    assert db.query(Evidence).count() == 0


def test_exception_state_is_not_auto_cleared(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    requirement = _requirement(db, listing_file, "ca_tds")
    requirement.state = RequirementState.EXCEPTION.value
    requirement.state_reason = "Human flag: seller disputes applicability"
    db.flush()
    _doc(db, listing_file, DocumentType.CA_TDS)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert db.query(Evidence).count() == 1
    requirement = _requirement(db, listing_file, "ca_tds")
    assert requirement.state == RequirementState.EXCEPTION.value
    assert requirement.state_reason == "Human flag: seller disputes applicability"
    assert "ca_tds" not in result.requirements_moved_to_received
    assert "ca_tds" not in result.unmet_requirement_keys


def test_verified_requirement_only_gains_evidence(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    requirement = _requirement(db, listing_file, "ca_tds")
    requirement.state = RequirementState.VERIFIED.value
    db.flush()
    _doc(db, listing_file, DocumentType.CA_TDS)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert _requirement(db, listing_file, "ca_tds").state == RequirementState.VERIFIED.value
    assert "ca_tds" not in result.requirements_moved_to_received


def test_requirement_without_rule_is_skipped(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    orphan = Requirement(
        listing_file_id=listing_file.id,
        requirement_rule_id=None,
        requirement_key="brokerage_file_note",
        requirement_type=RequirementType.BROKERAGE.value,
        status=RequirementStatus.OPTIONAL.value,
        state=RequirementState.PENDING.value,
    )
    db.add(orphan)
    db.flush()
    _doc(db, listing_file, DocumentType.CA_TDS)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert _requirement(db, listing_file, "ca_tds").state == RequirementState.RECEIVED.value
    assert _requirement(db, listing_file, "brokerage_file_note").state == RequirementState.PENDING.value
    assert "brokerage_file_note" in result.unmet_requirement_keys


def test_unmatched_documents_reported_alongside_matches(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _doc(db, listing_file, DocumentType.CA_TDS)
    invoice = _doc(db, listing_file, DocumentType.INVOICE)

    result = reconcile_listing_file(db, listing_file)

    assert result.evidence_created == 1
    assert result.requirements_moved_to_received == ["ca_tds"]
    assert result.unmatched_document_ids == [invoice.id]
    assert len(result.unmet_requirement_keys) == 11
