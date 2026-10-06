"""California requirement catalog, version 1.

Versioned, jurisdiction-scoped rule data for the requirement engine
(``app/services/requirement_engine.py``).  This is the data side of the
product thesis: the required-document list is an OUTPUT of evaluating
these rules against a listing file's ``property_attributes``, not a
hard-coded checklist.

V0 scope: California residential single-family — 12 rules, one per listing
document type.  Expansion (TX, FL, AZ, ...) means new catalog versions or
jurisdictions, not code changes.

Official CAR forms are copyrighted and members-only; ``source`` cites the
statute or form family descriptively, never reproduces form content.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.db.models import RequirementRule
from app.domain.enums import DocumentType, RequirementStatus, RequirementType

CATALOG_VERSION = "1"
CATALOG_JURISDICTION = "CA"
CATALOG_EFFECTIVE_FROM = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _rule(
    requirement_key: str,
    requirement_type: RequirementType,
    status: RequirementStatus,
    document_types: list[DocumentType],
    *,
    trigger: dict[str, Any] | None = None,
    timing: str = "pre_listing",
    source: str,
) -> dict[str, Any]:
    return {
        "requirement_key": requirement_key,
        "requirement_type": requirement_type.value,
        "status": status.value,
        "jurisdiction": CATALOG_JURISDICTION,
        "satisfied_by": {"document_types": [dt.value for dt in document_types]},
        "trigger": trigger,
        "timing": timing,
        "source": source,
        "version": CATALOG_VERSION,
        "effective_from": CATALOG_EFFECTIVE_FROM,
        "effective_to": None,
    }


def _lt(field: str, value: Any) -> dict[str, Any]:
    return {"field": {"field": field, "op": "lt", "value": value}}


def _eq(field: str, value: Any) -> dict[str, Any]:
    return {"field": {"field": field, "op": "eq", "value": value}}


CA_CATALOG_V1: list[dict[str, Any]] = [
    _rule(
        "listing_agreement", RequirementType.CORE, RequirementStatus.REQUIRED,
        [DocumentType.LISTING_AGREEMENT],
        source="CAR RLA — brokerage listing contract",
    ),
    _rule(
        "seller_advisory", RequirementType.CORE, RequirementStatus.REQUIRED,
        [DocumentType.SELLER_ADVISORY],
        source="CAR SA — pre-listing seller advisory",
    ),
    _rule(
        "agency_disclosure", RequirementType.CORE, RequirementStatus.REQUIRED,
        [DocumentType.AGENCY_DISCLOSURE],
        source="CAR AD — agency disclosure, precedes the listing agreement",
    ),
    _rule(
        "ca_tds", RequirementType.JURISDICTIONAL, RequirementStatus.REQUIRED,
        [DocumentType.CA_TDS],
        source="Cal. Civil Code \u00a71102 et seq. — transfer disclosure statement",
    ),
    _rule(
        "ca_spq", RequirementType.JURISDICTIONAL, RequirementStatus.REQUIRED,
        [DocumentType.CA_SPQ],
        source="CAR SPQ — seller property questionnaire, supplements TDS",
    ),
    _rule(
        "agent_visual_inspection", RequirementType.JURISDICTIONAL, RequirementStatus.REQUIRED,
        [DocumentType.AGENT_VISUAL_INSPECTION],
        source="Cal. Civil Code \u00a71102.6 — agent visual inspection disclosure",
    ),
    _rule(
        "ca_nhd", RequirementType.JURISDICTIONAL, RequirementStatus.REQUIRED,
        [DocumentType.CA_NHD],
        source="Cal. Civil Code \u00a71103 et seq. — natural hazard disclosure",
    ),
    _rule(
        "wcmd_advisory", RequirementType.JURISDICTIONAL, RequirementStatus.REQUIRED,
        [DocumentType.WCMD_ADVISORY],
        source="Cal. Civil Code \u00a7\u00a71101.1\u20131101.8 (SB 407) — water-conserving fixtures",
    ),
    _rule(
        "lead_disclosure", RequirementType.FEDERAL, RequirementStatus.CONDITIONALLY_REQUIRED,
        [DocumentType.LEAD_DISCLOSURE],
        trigger=_lt("year_built", 1978),
        source="42 USC \u00a74852d — federal lead-based paint disclosure",
    ),
    _rule(
        "hoa_package", RequirementType.PROPERTY_CONDITIONAL, RequirementStatus.CONDITIONALLY_REQUIRED,
        [DocumentType.HOA_PACKAGE],
        trigger=_eq("hoa", True),
        source="HOA resale package — CC&Rs, budget, minutes",
    ),
    _rule(
        "solar_agreement", RequirementType.PROPERTY_CONDITIONAL, RequirementStatus.CONDITIONALLY_REQUIRED,
        [DocumentType.SOLAR_AGREEMENT],
        trigger=_eq("solar", True),
        source="Solar ownership / financing agreement",
    ),
    _rule(
        "prelim_title_report", RequirementType.BROKERAGE, RequirementStatus.RECOMMENDED,
        [DocumentType.PRELIM_TITLE_REPORT],
        source="Brokerage file-completeness — preliminary title report",
    ),
]


def validate_catalog(entries: list[dict[str, Any]]) -> None:
    """Fail loud on catalog bugs: bad enums, duplicate keys, bad triggers."""
    from app.services.requirement_engine import validate_trigger_shape

    seen: set[str] = set()
    for entry in entries:
        key = entry["requirement_key"]
        if key in seen:
            raise ValueError(f"duplicate requirement_key: {key!r}")
        seen.add(key)
        RequirementType(entry["requirement_type"])
        RequirementStatus(entry["status"])
        for document_type in entry["satisfied_by"]["document_types"]:
            DocumentType(document_type)
        validate_trigger_shape(entry["trigger"])


def ensure_ca_catalog(session: Session) -> int:
    """Insert missing CA v1 rules.  Idempotent; returns rows created."""
    validate_catalog(CA_CATALOG_V1)
    created = 0
    for entry in CA_CATALOG_V1:
        exists = (
            session.query(RequirementRule)
            .filter_by(requirement_key=entry["requirement_key"], version=entry["version"])
            .first()
        )
        if exists is None:
            session.add(RequirementRule(**entry))
            created += 1
    session.flush()
    return created
