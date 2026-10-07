"""Exception detection: rules R1-R3 over a file's current state.

``detect_exceptions`` is a pipeline *stage* like reconcile: it evaluates
and (for R1/R2) raises ``EXCEPTION`` states, but it never recomputes the
verdict -- the orchestrator runs the verdict stage after this one, and
``EXCEPTION`` already blocks there.

* **R1 -- cross-document conflict.**  Two or more evidence documents that
  disagree (after Stage 1 normalization) on ``apn``, ``property_address``
  or ``seller_name``.  Every requirement backed by a document carrying a
  conflicting value moves to ``EXCEPTION`` with a machine-generated
  reason naming the conflict; an existing ``EXCEPTION`` reason (a human
  flag) is never overwritten.
* **R2 -- missing signatures.**  Evidence documents of a signature-bearing
  type whose ``signatures_present`` flag is ``False`` (``None``/absent
  means unknown, not unsigned) raise ``EXCEPTION`` on their requirement,
  same preservation rule as R1.  The type list lives in
  ``SIGNATURE_RULE_CONFIG``, not in code branches.
* **R3 -- overdue.**  A ``PENDING`` requirement older than
  ``settings.requirement_overdue_days`` since file creation is *reported*
  (findings, readout, review queue) but never mutated: overdue is a chase
  signal for the orchestrator, not a state change.

Findings are ordered R1 -> R2 -> R3 and every rule is evaluated even when
an earlier one already changed a requirement.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.db.models import Document, Evidence, ListingFile, Requirement
from app.domain.enums import RequirementState
from app.services.audit import REVIEW_ACTOR, record_audit
from app.services.normalization import document_normalized_fields
from app.services.reconciliation import document_type_value

#: R1: facts that must agree across a file's evidence documents.
CONFLICT_FIELDS: tuple[str, ...] = ("apn", "property_address", "seller_name")

#: R2 rule configuration: whose documents must carry signatures, and which
#: extracted flag proves it.  Data, not code -- extend the tuple to widen it.
SIGNATURE_RULE_CONFIG: dict[str, Any] = {
    "rule": "R2",
    "document_types": ("LISTING_AGREEMENT", "CA_TDS", "CA_SPQ"),
    "field": "signatures_present",
}


@dataclass(frozen=True)
class Conflict:
    """One R1 field where evidence documents disagree."""

    field: str
    values: list[str]
    document_ids: list[int]
    requirement_keys: list[str]
    reason: str


@dataclass(frozen=True)
class MissingSignature:
    """One R2 finding: an unsigned signature-bearing evidence document."""

    requirement_key: str
    document_type: str
    document_id: int
    reason: str


@dataclass(frozen=True)
class OverdueEntry:
    """One R3 surfacing: a PENDING requirement past the threshold."""

    requirement_key: str
    requirement_id: int
    days_pending: int


@dataclass(frozen=True)
class DetectionReport:
    """What one detection pass found; ``exceptions_raised`` counts state changes."""

    conflicts: list[Conflict] = field(default_factory=list)
    missing_signatures: list[MissingSignature] = field(default_factory=list)
    overdue: list[OverdueEntry] = field(default_factory=list)
    exceptions_raised: int = 0


def _aligned_moment(listing_file: ListingFile, now: datetime) -> datetime:
    """``now`` in the same naive/aware shape as ``listing_file.created_at``.

    SQLite's ``server_default=func.now()`` stores naive timestamps while
    Postgres keeps them aware; subtracting mixed shapes raises.
    """
    created_at = listing_file.created_at
    if created_at.tzinfo is None and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    if created_at.tzinfo is not None and now.tzinfo is None:
        return now.replace(tzinfo=created_at.tzinfo)
    return now


def is_overdue(
    listing_file: ListingFile,
    requirement: Requirement,
    *,
    threshold_days: int,
    now: Optional[datetime] = None,
) -> bool:
    """A ``PENDING`` requirement past the threshold since file creation."""
    if requirement.state != RequirementState.PENDING.value:
        return False
    moment = _aligned_moment(listing_file, now or datetime.now(timezone.utc))
    return moment - listing_file.created_at > timedelta(days=threshold_days)


def file_age_days(listing_file: ListingFile, *, now: Optional[datetime] = None) -> int:
    """Whole days since file creation (floored, never negative)."""
    moment = _aligned_moment(listing_file, now or datetime.now(timezone.utc))
    return max(0, (moment - listing_file.created_at).days)


def _evidence_documents(
    db: Session, listing_file: ListingFile
) -> tuple[list[Requirement], dict[int, list[Document]], list[Document]]:
    """Requirements, requirement -> evidence documents (in evidence order), and
    the distinct documents referenced."""
    requirements = (
        db.query(Requirement)
        .filter(Requirement.listing_file_id == listing_file.id)
        .order_by(Requirement.requirement_key)
        .all()
    )
    evidence_rows = (
        db.query(Evidence)
        .join(Requirement, Evidence.requirement_id == Requirement.id)
        .filter(Requirement.listing_file_id == listing_file.id)
        .order_by(Evidence.id)
        .all()
    )
    document_ids = sorted({row.document_id for row in evidence_rows if row.document_id is not None})
    documents = (
        db.query(Document).filter(Document.id.in_(document_ids)).order_by(Document.id).all()
        if document_ids
        else []
    )
    documents_by_id = {document.id: document for document in documents}
    by_requirement: dict[int, list[Document]] = {}
    for row in evidence_rows:
        document = documents_by_id.get(row.document_id)
        if document is not None:
            by_requirement.setdefault(row.requirement_id, []).append(document)
    return requirements, by_requirement, documents


def _detect_conflicts(
    requirements: list[Requirement],
    evidence_documents: dict[int, list[Document]],
    documents: list[Document],
) -> list[Conflict]:
    value_to_document_ids: dict[str, dict[str, set[int]]] = {name: {} for name in CONFLICT_FIELDS}
    for document in documents:
        normalized = document_normalized_fields(document)
        for field_name in CONFLICT_FIELDS:
            value = normalized.get(field_name)
            if value is None:
                continue
            value_to_document_ids[field_name].setdefault(value, set()).add(document.id)

    conflicts: list[Conflict] = []
    for field_name in CONFLICT_FIELDS:
        by_value = value_to_document_ids[field_name]
        if len(by_value) < 2:
            continue
        values = sorted(by_value)
        document_ids = sorted({doc_id for ids in by_value.values() for doc_id in ids})
        reason = (
            f"cross-document conflict on {field_name}: "
            + " vs ".join(f'"{value}"' for value in values)
            + " (documents "
            + ", ".join(f"#{doc_id}" for doc_id in document_ids)
            + ")"
        )
        requirement_keys = sorted(
            requirement.requirement_key
            for requirement in requirements
            if any(
                document.id in document_ids
                for document in evidence_documents.get(requirement.id, ())
            )
        )
        conflicts.append(
            Conflict(
                field=field_name,
                values=values,
                document_ids=document_ids,
                requirement_keys=requirement_keys,
                reason=reason,
            )
        )
    return conflicts


def detect_exceptions(
    db: Session,
    listing_file: ListingFile,
    *,
    overdue_days: int,
    now: Optional[datetime] = None,
) -> DetectionReport:
    """Evaluate R1-R3 for one file, raising R1/R2 exceptions as configured.

    Mutates requirements and writes ``EXCEPTION_DETECTED`` audit events for
    every state change; R3 mutates nothing.  The caller commits.
    """
    moment = now or datetime.now(timezone.utc)
    requirements, evidence_documents, documents = _evidence_documents(db, listing_file)

    # R1: evaluate first, then mutate -- R2 must see the state R1 left behind.
    conflicts = _detect_conflicts(requirements, evidence_documents, documents)
    conflict_by_requirement: dict[int, Conflict] = {}
    for conflict in conflicts:
        for requirement in requirements:
            if requirement.requirement_key in conflict.requirement_keys:
                conflict_by_requirement.setdefault(requirement.id, conflict)

    exceptions_raised = 0
    for requirement in requirements:
        conflict = conflict_by_requirement.get(requirement.id)
        if conflict is None or requirement.state == RequirementState.EXCEPTION.value:
            continue
        _raise_exception(
            db,
            requirement,
            reason=conflict.reason,
            document_id=(
                evidence_documents[requirement.id][0].id
                if evidence_documents.get(requirement.id)
                else None
            ),
            details={"rule": "R1", "field": conflict.field, "values": conflict.values},
        )
        exceptions_raised += 1

    # R2: evaluate every requirement; an existing EXCEPTION reason stands.
    signature_document_types = frozenset(SIGNATURE_RULE_CONFIG["document_types"])
    signature_flag = SIGNATURE_RULE_CONFIG["field"]
    missing_signatures: list[MissingSignature] = []
    for requirement in requirements:
        for document in evidence_documents.get(requirement.id, ()):
            document_type = document_type_value(document)
            if document_type not in signature_document_types:
                continue
            if (document.extracted_data or {}).get(signature_flag) is not False:
                continue
            reason = f"missing signatures ({document_type}, document #{document.id})"
            missing_signatures.append(
                MissingSignature(
                    requirement_key=requirement.requirement_key,
                    document_type=document_type,
                    document_id=document.id,
                    reason=reason,
                )
            )
            if requirement.state != RequirementState.EXCEPTION.value:
                _raise_exception(
                    db,
                    requirement,
                    reason=reason,
                    document_id=document.id,
                    details={"rule": SIGNATURE_RULE_CONFIG["rule"], "document_type": document_type},
                )
                exceptions_raised += 1
            break

    # R3: report only -- overdue work is a chase signal, never a state change.
    age_days = file_age_days(listing_file, now=moment)
    overdue = [
        OverdueEntry(
            requirement_key=requirement.requirement_key,
            requirement_id=requirement.id,
            days_pending=age_days,
        )
        for requirement in requirements
        if is_overdue(listing_file, requirement, threshold_days=overdue_days, now=moment)
    ]

    return DetectionReport(
        conflicts=conflicts,
        missing_signatures=missing_signatures,
        overdue=overdue,
        exceptions_raised=exceptions_raised,
    )


def _raise_exception(
    db: Session,
    requirement: Requirement,
    *,
    reason: str,
    document_id: Optional[int],
    details: dict[str, Any],
) -> None:
    """Move one requirement to ``EXCEPTION`` and record the audit event.

    The caller counts the state change; ``document_id`` is the evidence
    document the finding is about (the conflict's first evidence document,
    or the unsigned document for R2) so it is queryable from that
    document's audit trail.
    """
    previous_state = requirement.state
    requirement.state = RequirementState.EXCEPTION.value
    requirement.state_reason = reason
    record_audit(
        db,
        event_type="EXCEPTION_DETECTED",
        status="SUCCESS",
        message=reason,
        document_id=document_id,
        details={
            **details,
            "requirement_id": requirement.id,
            "requirement_key": requirement.requirement_key,
            "previous_state": previous_state,
            "actor": REVIEW_ACTOR,
        },
    )
