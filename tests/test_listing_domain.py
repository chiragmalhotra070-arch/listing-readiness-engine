"""Slice 1: listing domain -- enums, models, constraints.

Runs on SQLite via ``Base.metadata.create_all`` (same as the rest of the
suite); PostgreSQL specifics (JSONB, CHECK) are covered by the migration
itself, exercised on the dev database.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, Document, Evidence, ListingFile, Requirement, RequirementRule
from app.domain.enums import (
    BusinessOutcome,
    DocumentType,
    EvidenceSource,
    ProcessingStage,
    ReadinessVerdict,
    RequirementState,
    RequirementStatus,
    RequirementType,
)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


def _listing_file(db, **kwargs):
    listing_file = ListingFile(
        property_address="123 Main Street, Los Angeles, CA 90001",
        apn="1234-567-890",
        seller_name="Jane Seller",
        property_attributes={
            "year_built": 1968,
            "property_type": "single_family",
            "hoa": True,
            "solar": True,
            "septic": False,
            "seller_type": "individual",
        },
        **kwargs,
    )
    db.add(listing_file)
    db.flush()
    return listing_file


def test_listing_document_types():
    assert DocumentType.CA_TDS.value == "CA_TDS"
    assert DocumentType.CA_SPQ.value == "CA_SPQ"
    assert DocumentType.CA_NHD.value == "CA_NHD"
    assert DocumentType.LEAD_DISCLOSURE.value == "LEAD_DISCLOSURE"
    assert DocumentType.HOA_PACKAGE.value == "HOA_PACKAGE"
    assert DocumentType.LISTING_AGREEMENT.value == "LISTING_AGREEMENT"
    assert DocumentType.PRELIM_TITLE_REPORT.value == "PRELIM_TITLE_REPORT"
    assert DocumentType.SOLAR_AGREEMENT.value == "SOLAR_AGREEMENT"
    # financial taxonomy untouched
    assert DocumentType.INVOICE.value == "INVOICE"
    assert DocumentType.UNKNOWN.value == "UNKNOWN"


def test_listing_pipeline_stages():
    for stage in ("NORMALIZE", "PROPERTY_RESOLUTION", "RECONCILE", "REQUIREMENT_CHECK", "READINESS_VERDICT"):
        assert ProcessingStage(stage).value == stage


def test_readiness_verdict_is_file_level():
    assert {v.value for v in ReadinessVerdict} == {"READY", "CONDITIONALLY_READY", "NOT_READY"}
    # document-level outcomes untouched
    assert BusinessOutcome.NEEDS_REVIEW.value == "NEEDS_REVIEW"
    assert BusinessOutcome.FAILED.value == "FAILED"


def test_requirement_axes_are_independent():
    assert RequirementType.FEDERAL.value == "FEDERAL"
    assert RequirementType.PROPERTY_CONDITIONAL.value == "PROPERTY_CONDITIONAL"
    assert RequirementStatus.CONDITIONALLY_REQUIRED.value == "CONDITIONALLY_REQUIRED"
    assert RequirementState.EXCEPTION.value == "EXCEPTION"
    assert EvidenceSource.ATTESTATION.value == "ATTESTATION"


def test_listing_file_verdict_defaults_null(db):
    listing_file = _listing_file(db)
    assert listing_file.readiness_verdict is None
    listing_file.readiness_verdict = ReadinessVerdict.CONDITIONALLY_READY
    listing_file.readiness_reason = "HOA package pending"
    db.flush()
    assert db.get(ListingFile, listing_file.id).readiness_verdict == ReadinessVerdict.CONDITIONALLY_READY


def test_document_binds_to_listing_file_without_email(db):
    listing_file = _listing_file(db)
    doc = Document(document_name="tds.pdf", mime_type="application/pdf", listing_file_id=listing_file.id)
    db.add(doc)
    db.flush()
    assert doc.email_id is None
    assert doc.listing_file.id == listing_file.id
    assert listing_file.documents[0].id == doc.id


def test_requirement_lifecycle_and_uniqueness(db):
    listing_file = _listing_file(db)
    req = Requirement(
        listing_file_id=listing_file.id,
        requirement_key="CA_TDS",
        requirement_type=RequirementType.JURISDICTIONAL.value,
        status=RequirementStatus.REQUIRED.value,
    )
    db.add(req)
    db.flush()
    assert req.state == RequirementState.PENDING
    req.state = RequirementState.RECEIVED.value
    db.flush()
    # the same requirement key twice for one file is rejected
    db.add(
        Requirement(
            listing_file_id=listing_file.id,
            requirement_key="CA_TDS",
            requirement_type="JURISDICTIONAL",
            status="REQUIRED",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_requirement_state_check_constraint(db):
    listing_file = _listing_file(db)
    db.add(
        Requirement(
            listing_file_id=listing_file.id,
            requirement_key="CA_TDS",
            requirement_type="JURISDICTIONAL",
            status="REQUIRED",
            state="BOGUS",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_requirement_rule_versioning(db):
    for version in ("2026-10-01", "2026-11-01"):
        db.add(
            RequirementRule(
                requirement_key="CA_TDS",
                requirement_type=RequirementType.JURISDICTIONAL.value,
                status=RequirementStatus.REQUIRED.value,
                jurisdiction="CA",
                version=version,
                trigger={"property_type": "single_family"},
                satisfied_by={"document_types": ["CA_TDS"]},
            )
        )
    db.flush()
    # same key + same version is rejected
    db.add(
        RequirementRule(
            requirement_key="CA_TDS",
            requirement_type="JURISDICTIONAL",
            status="REQUIRED",
            jurisdiction="CA",
            version="2026-10-01",
        )
    )
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_evidence_links_document_or_attestation(db):
    listing_file = _listing_file(db)
    doc = Document(document_name="hoa.pdf", mime_type="application/pdf", listing_file_id=listing_file.id)
    db.add(doc)
    db.flush()
    req = Requirement(
        listing_file_id=listing_file.id,
        requirement_key="HOA_PACKAGE",
        requirement_type="PROPERTY_CONDITIONAL",
        status="REQUIRED",
    )
    db.add(req)
    db.flush()
    doc_evidence = Evidence(
        requirement_id=req.id,
        document_id=doc.id,
        source=EvidenceSource.DOCUMENT.value,
        confidence=0.97,
    )
    attestation = Evidence(
        requirement_id=req.id,
        document_id=None,
        source=EvidenceSource.ATTESTATION.value,
        detail={"attested_by": "listing agent"},
    )
    db.add_all([doc_evidence, attestation])
    db.flush()
    assert req.evidence[0].source == EvidenceSource.DOCUMENT.value
    assert attestation.verified is False
