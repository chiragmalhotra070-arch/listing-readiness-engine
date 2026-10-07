"""Slice 6: readiness verdict -- requirement states rolled up to a verdict.

Covers ``compute_verdict``: the four verdict branches, the blocking set
(REQUIRED + CONDITIONALLY_REQUIRED), advisory handling, the zero-requirement
guard, and overwrite-on-recompute.  Runs on SQLite like the rest of the
suite (same ``db`` fixture pattern as ``test_listing_domain.py``).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ListingFile, Requirement
from app.domain.enums import (
    ReadinessVerdict,
    RequirementState,
    RequirementStatus,
    RequirementType,
)
from app.services.readiness import VerdictDimensions, compute_verdict
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

ALL_KEYS = {
    "listing_agreement",
    "seller_advisory",
    "agency_disclosure",
    "ca_tds",
    "ca_spq",
    "agent_visual_inspection",
    "ca_nhd",
    "wcmd_advisory",
    "lead_disclosure",
    "hoa_package",
    "solar_agreement",
    "prelim_title_report",
}
BLOCKING_KEYS = ALL_KEYS - {"prelim_title_report"}
ADVISORY_KEYS = {"prelim_title_report"}


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


def _generate(db, listing_file) -> list[Requirement]:
    ensure_ca_catalog(db)
    requirements = RequirementEngine().generate(db, listing_file)
    db.flush()
    return requirements


def _set_state(db, listing_file, requirement_key: str, state: RequirementState) -> Requirement:
    requirement = (
        db.query(Requirement)
        .filter_by(listing_file_id=listing_file.id, requirement_key=requirement_key)
        .one()
    )
    requirement.state = state.value
    db.flush()
    return requirement


def _set_all(db, listing_file, state: RequirementState) -> None:
    db.query(Requirement).filter_by(listing_file_id=listing_file.id).update(
        {"state": state.value}, synchronize_session=False
    )
    db.flush()


def _states(db, listing_file) -> dict[str, str]:
    return {
        requirement.requirement_key: requirement.state
        for requirement in db.query(Requirement).filter_by(listing_file_id=listing_file.id)
    }


def test_all_blocking_verified_is_ready(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.READY
    assert listing_file.readiness_verdict == ReadinessVerdict.READY.value
    assert f"all {len(BLOCKING_KEYS)} required requirements verified" in listing_file.readiness_reason
    assert listing_file.readiness_assessed_at is not None
    assert result.blocking_pending == []
    assert result.blocking_exceptions == []
    assert result.pending_verification == []
    assert result.advisory == []


def test_blocking_pending_is_not_ready(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "ca_tds", RequirementState.PENDING)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.NOT_READY
    assert listing_file.readiness_verdict == ReadinessVerdict.NOT_READY.value
    assert result.blocking_pending == ["ca_tds"]
    assert "missing: ca_tds" in listing_file.readiness_reason
    assert "Not ready" in listing_file.readiness_reason


def test_blocking_exception_is_not_ready(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "lead_disclosure", RequirementState.EXCEPTION)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.NOT_READY
    assert result.blocking_exceptions == ["lead_disclosure"]
    assert result.blocking_pending == []
    assert "flagged for review: lead_disclosure" in listing_file.readiness_reason


def test_received_is_conditionally_ready(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "ca_spq", RequirementState.RECEIVED)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.CONDITIONALLY_READY
    assert listing_file.readiness_verdict == ReadinessVerdict.CONDITIONALLY_READY.value
    assert result.pending_verification == ["ca_spq"]
    assert result.blocking_pending == []
    assert "pending verification: ca_spq" in listing_file.readiness_reason


def test_recommended_pending_does_not_block(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "prelim_title_report", RequirementState.PENDING)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.READY
    assert result.advisory == ["prelim_title_report"]
    assert result.blocking_pending == []
    assert "prelim_title_report" in listing_file.readiness_reason
    assert "Advisory" in listing_file.readiness_reason
    assert "all 11 required requirements verified" in listing_file.readiness_reason


def test_exception_on_recommended_does_not_block(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "prelim_title_report", RequirementState.EXCEPTION)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.READY
    assert result.blocking_exceptions == []
    assert "prelim_title_report" in result.advisory


def test_optional_requirement_never_blocks(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    db.add(
        Requirement(
            listing_file_id=listing_file.id,
            requirement_key="yard_sign",
            requirement_type=RequirementType.OPTIONAL_SUPPORTING.value,
            status=RequirementStatus.OPTIONAL.value,
            state=RequirementState.PENDING.value,
        )
    )
    db.flush()

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.READY
    assert result.advisory == ["yard_sign"]
    assert "yard_sign" in listing_file.readiness_reason


def test_no_requirements_is_not_ready(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.NOT_READY
    assert listing_file.readiness_verdict == ReadinessVerdict.NOT_READY.value
    assert "no requirements generated" in listing_file.readiness_reason
    assert listing_file.readiness_assessed_at is not None
    assert result.blocking_pending == []
    assert result.blocking_exceptions == []
    assert result.pending_verification == []
    assert result.advisory == []
    assert result.dimensions == VerdictDimensions(1.0, 1.0, 1.0)
    assert db.query(Requirement).count() == 0


def test_recompute_overwrites_previous_verdict(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)

    first = compute_verdict(db, listing_file)
    first_assessed_at = listing_file.readiness_assessed_at
    second = compute_verdict(db, listing_file)
    second_assessed_at = listing_file.readiness_assessed_at

    assert first.verdict == second.verdict == ReadinessVerdict.READY
    assert listing_file.readiness_reason == second.reason
    assert second_assessed_at >= first_assessed_at

    _set_state(db, listing_file, "ca_nhd", RequirementState.PENDING)
    third = compute_verdict(db, listing_file)

    assert third.verdict == ReadinessVerdict.NOT_READY
    assert listing_file.readiness_verdict == ReadinessVerdict.NOT_READY.value
    assert "ca_nhd" in listing_file.readiness_reason
    assert listing_file.readiness_assessed_at >= second_assessed_at


def test_combined_missing_and_flagged_reason(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    _set_all(db, listing_file, RequirementState.VERIFIED)
    _set_state(db, listing_file, "ca_tds", RequirementState.PENDING)
    _set_state(db, listing_file, "lead_disclosure", RequirementState.EXCEPTION)

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.NOT_READY
    assert result.blocking_pending == ["ca_tds"]
    assert result.blocking_exceptions == ["lead_disclosure"]
    assert "missing: ca_tds" in listing_file.readiness_reason
    assert "flagged for review: lead_disclosure" in listing_file.readiness_reason


def test_full_demo_shape(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    assert len(_states(db, listing_file)) == 12

    _set_all(db, listing_file, RequirementState.RECEIVED)
    received = compute_verdict(db, listing_file)
    assert received.verdict == ReadinessVerdict.CONDITIONALLY_READY
    assert set(received.pending_verification) == BLOCKING_KEYS
    assert received.advisory == ["prelim_title_report"]

    _set_all(db, listing_file, RequirementState.VERIFIED)
    verified = compute_verdict(db, listing_file)
    assert verified.verdict == ReadinessVerdict.READY
    assert verified.advisory == []

    _set_state(db, listing_file, "ca_tds", RequirementState.PENDING)
    missing = compute_verdict(db, listing_file)
    assert missing.verdict == ReadinessVerdict.NOT_READY
    assert missing.blocking_pending == ["ca_tds"]
    assert "missing: ca_tds" in listing_file.readiness_reason


def test_dimensions_use_the_blocking_set_as_denominator(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    # Twelve all-blocking requirements: the RECOMMENDED one made REQUIRED.
    db.query(Requirement).filter_by(listing_file_id=listing_file.id).update(
        {"status": RequirementStatus.REQUIRED.value}, synchronize_session=False
    )
    db.flush()
    keys = sorted(_states(db, listing_file))
    for key in keys[:6]:
        _set_state(db, listing_file, key, RequirementState.RECEIVED)

    received = compute_verdict(db, listing_file)

    assert received.dimensions == VerdictDimensions(
        completeness=0.5, consistency=1.0, compliance=0.0
    )

    for key in keys[:6]:
        _set_state(db, listing_file, key, RequirementState.VERIFIED)
    verified = compute_verdict(db, listing_file)

    assert verified.dimensions == VerdictDimensions(
        completeness=0.5, consistency=1.0, compliance=0.5
    )


def test_dimensions_are_all_one_when_nothing_blocks(db):
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    _generate(db, listing_file)
    db.query(Requirement).filter_by(listing_file_id=listing_file.id).update(
        {"status": RequirementStatus.RECOMMENDED.value}, synchronize_session=False
    )
    db.flush()

    result = compute_verdict(db, listing_file)

    assert result.verdict == ReadinessVerdict.READY
    assert result.dimensions == VerdictDimensions(1.0, 1.0, 1.0)
