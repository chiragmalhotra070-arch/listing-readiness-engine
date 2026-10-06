"""Readiness verdict: roll requirement states up to a file-level verdict.

Pure rule rollup over a listing file's current ``Requirement`` rows — no
scoring, no weighting, no Evidence inspection.  Verification state is the
signal; ``Evidence.confidence`` gates verification elsewhere (rule-check
/ human review), not the verdict.

V0 boundaries (deliberate):
- The blocking set is ``REQUIRED`` + ``CONDITIONALLY_REQUIRED``.  Every
  requirement on the file is applicable by construction (generation
  already applied triggers), so a human-flagged EXCEPTION on a blocking
  requirement is a hard block: false blocking is acceptable, false
  clearance is not.
- ``RECOMMENDED`` / ``OPTIONAL`` never block; they surface as advisory.
  An EXCEPTION on a non-blocking requirement does not block either.
- Zero requirements means zero defenses — the verdict is ``NOT_READY``
  with an actionable "no requirements generated" reason.
- The verdict is a function of current requirement state only: re-running
  overwrites the previous verdict (no manual override in v1), and owner /
  due_date gap plans are ignored.
- Nothing in this *pipeline* sets ``VERIFIED`` (reconciliation reaches
  ``RECEIVED``): ``VERIFIED`` comes from human review via
  ``POST /requirements/{id}/verify`` (Slice 8).  Until a person verifies,
  ``CONDITIONALLY_READY`` is the highest verdict a file can reach.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.db.models import ListingFile, Requirement
from app.domain.enums import ReadinessVerdict, RequirementState, RequirementStatus

#: The blocking set: a file cannot be ready while any of these is open.
BLOCKING_STATUSES = frozenset({
    RequirementStatus.REQUIRED.value,
    RequirementStatus.CONDITIONALLY_REQUIRED.value,
})


@dataclass(frozen=True)
class VerdictResult:
    """The verdict plus the requirement keys behind each branch."""

    verdict: ReadinessVerdict
    reason: str
    blocking_pending: list[str] = field(default_factory=list)
    blocking_exceptions: list[str] = field(default_factory=list)
    pending_verification: list[str] = field(default_factory=list)
    advisory: list[str] = field(default_factory=list)


def _not_ready_reason(missing: list[str], flagged: list[str]) -> str:
    parts = []
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    if flagged:
        parts.append(f"flagged for review: {', '.join(flagged)}")
    return f"Not ready — {'; '.join(parts)}."


def _with_advisory(reason: str, advisory: list[str]) -> str:
    if not advisory:
        return reason
    return f"{reason} Advisory — recommended pending: {', '.join(advisory)}."


def _classify(requirements: list[Requirement]) -> VerdictResult:
    blocking_pending: list[str] = []
    blocking_exceptions: list[str] = []
    pending_verification: list[str] = []
    advisory: list[str] = []
    blocking_total = 0

    for requirement in requirements:
        key = requirement.requirement_key
        if requirement.status not in BLOCKING_STATUSES:
            if requirement.state != RequirementState.VERIFIED.value:
                advisory.append(key)
            continue
        blocking_total += 1
        state = requirement.state
        if state == RequirementState.EXCEPTION.value:
            blocking_exceptions.append(key)
        elif state == RequirementState.PENDING.value:
            blocking_pending.append(key)
        elif state == RequirementState.RECEIVED.value:
            pending_verification.append(key)

    if blocking_exceptions or blocking_pending:
        verdict = ReadinessVerdict.NOT_READY
        reason = _not_ready_reason(sorted(blocking_pending), sorted(blocking_exceptions))
    elif pending_verification:
        verdict = ReadinessVerdict.CONDITIONALLY_READY
        reason = f"Conditionally ready — pending verification: {', '.join(sorted(pending_verification))}."
    else:
        verdict = ReadinessVerdict.READY
        if blocking_total:
            reason = f"Ready — all {blocking_total} required requirements verified."
        else:
            reason = "Ready — no blocking requirements."

    return VerdictResult(
        verdict=verdict,
        reason=_with_advisory(reason, sorted(advisory)),
        blocking_pending=sorted(blocking_pending),
        blocking_exceptions=sorted(blocking_exceptions),
        pending_verification=sorted(pending_verification),
        advisory=sorted(advisory),
    )


def compute_verdict(session: Session, listing_file: ListingFile) -> VerdictResult:
    """Compute and persist the file-level readiness verdict.

    Overwrites any previous verdict: ``readiness_verdict``,
    ``readiness_reason``, and ``readiness_assessed_at`` are a pure
    function of the requirement states right now.
    """
    requirements = (
        session.query(Requirement)
        .filter(Requirement.listing_file_id == listing_file.id)
        .order_by(Requirement.requirement_key)
        .all()
    )

    if requirements:
        result = _classify(requirements)
    else:
        result = VerdictResult(
            verdict=ReadinessVerdict.NOT_READY,
            reason="Not ready — no requirements generated.",
        )

    listing_file.readiness_verdict = result.verdict.value
    listing_file.readiness_reason = result.reason
    listing_file.readiness_assessed_at = datetime.now(timezone.utc)
    session.flush()
    return result
