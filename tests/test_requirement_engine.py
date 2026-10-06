"""Slice 4: requirement engine -- from property facts to the required list.

Covers the JSON predicate evaluator, the CA v1 catalog seed, and
idempotent requirement generation (re-runs never clobber agent progress).
Runs on SQLite like the rest of the suite.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import Base, ListingFile, Requirement, RequirementRule
from app.domain.enums import RequirementState
from app.services.requirement_catalog import CA_CATALOG_V1, ensure_ca_catalog, validate_catalog
from app.services.requirement_engine import (
    RequirementEngine,
    evaluate_trigger,
    select_rules,
    validate_trigger_shape,
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


def _file(db, **facts):
    listing_file = ListingFile(
        property_address="123 Main St, Pasadena, CA 91101",
        apn="5842-018-024",
        seller_name="Jane Seller",
        property_attributes=facts,
    )
    db.add(listing_file)
    db.flush()
    return listing_file


CA_1968_HOA_SOLAR = {
    "state": "CA",
    "year_built": 1968,
    "property_type": "single_family",
    "hoa": True,
    "solar": True,
    "septic": False,
    "seller_type": "individual",
}
CA_2005_NO_HOA_SOLAR = {
    "state": "CA",
    "year_built": 2005,
    "property_type": "single_family",
    "hoa": False,
    "solar": False,
    "septic": False,
    "seller_type": "individual",
}
ALWAYS_KEYS = {
    "listing_agreement",
    "seller_advisory",
    "agency_disclosure",
    "ca_tds",
    "ca_spq",
    "agent_visual_inspection",
    "ca_nhd",
    "wcmd_advisory",
    "prelim_title_report",
}


@pytest.mark.parametrize(
    "trigger,facts,expected",
    [
        (None, {}, True),
        (None, {"year_built": 1968}, True),
        ({"field": {"field": "year_built", "op": "lt", "value": 1978}}, {"year_built": 1968}, True),
        ({"field": {"field": "year_built", "op": "lt", "value": 1978}}, {"year_built": 2005}, False),
        ({"field": {"field": "year_built", "op": "lt", "value": 1978}}, {}, False),
        ({"field": {"field": "hoa", "op": "eq", "value": True}}, {"hoa": True}, True),
        ({"field": {"field": "hoa", "op": "eq", "value": True}}, {"hoa": False}, False),
        ({"field": {"field": "year_built", "op": "gte", "value": 1978}}, {"year_built": 1978}, True),
        ({"field": {"field": "seller_type", "op": "in", "value": ["trust", "llc"]}}, {"seller_type": "trust"}, True),
        ({"field": {"field": "seller_type", "op": "in", "value": ["trust", "llc"]}}, {"seller_type": "individual"}, False),
        ({"field": {"field": "seller_type", "op": "not_in", "value": ["trust"]}}, {"seller_type": "individual"}, True),
        ({"field": {"field": "year_built", "op": "is_null"}}, {}, True),
        ({"field": {"field": "year_built", "op": "is_null"}}, {"year_built": None}, True),
        ({"field": {"field": "year_built", "op": "is_null"}}, {"year_built": 1968}, False),
        ({"field": {"field": "year_built", "op": "not_null"}}, {"year_built": 1968}, True),
        ({"field": {"field": "year_built", "op": "not_null"}}, {}, False),
        ({"field": {"field": "hoa", "op": "is_true"}}, {"hoa": True}, True),
        ({"field": {"field": "hoa", "op": "is_true"}}, {"hoa": 1}, False),
        ({"field": {"field": "hoa", "op": "is_false"}}, {"hoa": False}, True),
        (
            {"all": [{"field": {"field": "hoa", "op": "eq", "value": True}}, {"field": {"field": "solar", "op": "eq", "value": True}}]},
            {"hoa": True, "solar": True},
            True,
        ),
        (
            {"all": [{"field": {"field": "hoa", "op": "eq", "value": True}}, {"field": {"field": "solar", "op": "eq", "value": True}}]},
            {"hoa": True, "solar": False},
            False,
        ),
        (
            {"any": [{"field": {"field": "hoa", "op": "eq", "value": True}}, {"field": {"field": "solar", "op": "eq", "value": True}}]},
            {"hoa": False, "solar": True},
            True,
        ),
        (
            {"not": {"field": {"field": "hoa", "op": "eq", "value": True}}},
            {"hoa": False},
            True,
        ),
        (
            {"not": {"field": {"field": "hoa", "op": "eq", "value": True}}},
            {"hoa": True},
            False,
        ),
    ],
)
def test_evaluate_trigger(trigger, facts, expected):
    assert evaluate_trigger(trigger, facts) is expected


@pytest.mark.parametrize(
    "trigger",
    [
        {"unknown_form": []},
        {"all": []},
        {"all": "notalist"},
        {"not": {"bogus": 1}},
        {"field": {"field": "nope", "op": "eq", "value": 1}},
        {"field": {"field": "hoa", "op": "bogus", "value": True}},
        {"field": {"field": "hoa", "op": "eq"}},
        {"a": 1, "b": 2},
        "notadict",
        42,
    ],
)
def test_validate_trigger_shape_rejects(trigger):
    with pytest.raises(ValueError):
        validate_trigger_shape(trigger)


def test_catalog_has_twelve_unique_keys():
    assert len(CA_CATALOG_V1) == 12
    assert len({entry["requirement_key"] for entry in CA_CATALOG_V1}) == 12


def test_catalog_validates_clean():
    validate_catalog(CA_CATALOG_V1)


def test_catalog_rejects_unknown_fact_field():
    bad = [dict(CA_CATALOG_V1[0], trigger={"field": {"field": "bogus", "op": "eq", "value": 1}})]
    with pytest.raises(ValueError, match="unknown fact field"):
        validate_catalog(bad)


def test_catalog_rejects_duplicate_keys():
    with pytest.raises(ValueError, match="duplicate requirement_key"):
        validate_catalog([CA_CATALOG_V1[0], CA_CATALOG_V1[0]])


def test_ensure_ca_catalog_is_idempotent(db):
    assert ensure_ca_catalog(db) == 12
    assert ensure_ca_catalog(db) == 0
    assert db.query(RequirementRule).count() == 12


def test_generate_full_ca_file(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    requirements = RequirementEngine().generate(db, listing_file)
    assert {r.requirement_key for r in requirements} == ALWAYS_KEYS | {
        "lead_disclosure",
        "hoa_package",
        "solar_agreement",
    }
    assert all(r.state == RequirementState.PENDING.value for r in requirements)
    assert all(r.requirement_rule_id is not None for r in requirements)


def test_generate_skips_untriggered_conditionals(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, **CA_2005_NO_HOA_SOLAR)
    requirements = RequirementEngine().generate(db, listing_file)
    assert {r.requirement_key for r in requirements} == ALWAYS_KEYS


def test_generate_is_idempotent_and_preserves_progress(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    engine = RequirementEngine()
    first = engine.generate(db, listing_file)
    assert len(first) == 12
    progressed_key = first[0].requirement_key
    first[0].state = RequirementState.RECEIVED.value
    db.flush()
    second = engine.generate(db, listing_file)
    assert len(second) == 12
    assert db.query(Requirement).filter_by(listing_file_id=listing_file.id).count() == 12
    by_key = {r.requirement_key: r for r in second}
    assert by_key[progressed_key].state == RequirementState.RECEIVED.value


def test_generate_out_of_jurisdiction_yields_nothing(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, state="TX", year_built=1968, hoa=True, solar=True)
    assert RequirementEngine().generate(db, listing_file) == []


def test_generate_without_state_yields_nothing(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, year_built=1968, hoa=True)
    assert RequirementEngine().generate(db, listing_file) == []


def test_requirement_rows_carry_rule_linkage(db):
    ensure_ca_catalog(db)
    listing_file = _file(db, **CA_1968_HOA_SOLAR)
    by_key = {r.requirement_key: r for r in RequirementEngine().generate(db, listing_file)}
    assert (by_key["ca_tds"].requirement_type, by_key["ca_tds"].status) == ("JURISDICTIONAL", "REQUIRED")
    assert (by_key["lead_disclosure"].requirement_type, by_key["lead_disclosure"].status) == (
        "FEDERAL",
        "CONDITIONALLY_REQUIRED",
    )
    assert (by_key["prelim_title_report"].requirement_type, by_key["prelim_title_report"].status) == (
        "BROKERAGE",
        "RECOMMENDED",
    )


def test_select_rules_prefers_highest_version():
    old = RequirementRule(
        requirement_key="x", requirement_type="CORE", status="REQUIRED",
        jurisdiction="CA", version="1", trigger=None,
    )
    new = RequirementRule(
        requirement_key="x", requirement_type="CORE", status="REQUIRED",
        jurisdiction="CA", version="2", trigger=None,
    )
    assert select_rules([old, new], {"state": "CA"}) == [new]


def test_select_rules_respects_effective_window():
    future = RequirementRule(
        requirement_key="x", requirement_type="CORE", status="REQUIRED",
        jurisdiction="CA", version="1", trigger=None,
        effective_from=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert select_rules([future], {"state": "CA"}) == []
