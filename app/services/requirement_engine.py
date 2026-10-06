"""Requirement engine: from property facts to the required-document list.

Evaluates versioned ``RequirementRule`` rows against a listing file's
``property_attributes`` and generates ``Requirement`` rows.  Triggers are
data (JSON predicates stored on the rule), not code — see
``app/services/requirement_catalog.py``.

V0 boundaries (deliberate):
- A missing fact never fires a rule (except ``is_null``): requirements are
  not invented from unknown data.  Unknown facts are a readiness-layer
  concern, not a rule-firing concern.
- Re-generation creates missing requirements but never modifies or retires
  existing ones: agent progress (RECEIVED, owners, due dates) is never
  clobbered by a re-run.
- Type mismatches between a fact and a trigger value raise; triggers and
  fact types are both controlled by us, so a mismatch is a bug that must
  surface, not a rule to silently skip.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.db.models import ListingFile, Requirement, RequirementRule
from app.domain.enums import RequirementState

#: Fact fields a rule trigger may reference.  Anything else fails catalog
#: validation — a trigger on an unknown fact is a catalog bug, not data.
KNOWN_FACT_FIELDS = frozenset({
    "state",
    "year_built",
    "property_type",
    "hoa",
    "solar",
    "septic",
    "well",
    "seller_type",
    "tenant_occupied",
    "pool_spa",
})

_OPERATORS = (
    "eq", "ne", "lt", "lte", "gt", "gte",
    "in", "not_in", "is_true", "is_false", "is_null", "not_null",
)

_MISSING = object()


def validate_trigger_shape(trigger: Any) -> None:
    """Raise ValueError when the trigger is not a valid predicate."""
    if trigger is None:
        return
    if not isinstance(trigger, dict) or len(trigger) != 1:
        raise ValueError(f"trigger must be a single-key predicate object: {trigger!r}")
    (form, payload), = trigger.items()
    if form in ("all", "any"):
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"{form!r} requires a non-empty list: {trigger!r}")
        for sub in payload:
            validate_trigger_shape(sub)
    elif form == "not":
        validate_trigger_shape(payload)
    elif form == "field":
        if not isinstance(payload, dict):
            raise ValueError(f"field predicate needs an object payload: {trigger!r}")
        field, op = payload.get("field"), payload.get("op")
        if field not in KNOWN_FACT_FIELDS:
            raise ValueError(f"trigger references unknown fact field: {field!r}")
        if op not in _OPERATORS:
            raise ValueError(f"unknown trigger operator: {op!r}")
        if op not in ("is_true", "is_false", "is_null", "not_null") and "value" not in payload:
            raise ValueError(f"operator {op!r} requires a value: {trigger!r}")
    else:
        raise ValueError(f"unknown predicate form: {form!r}")


def evaluate_trigger(trigger: dict[str, Any] | None, facts: dict[str, Any]) -> bool:
    """True when the rule applies to the facts."""
    if trigger is None:
        return True
    (form, payload), = trigger.items()
    if form == "all":
        return all(evaluate_trigger(sub, facts) for sub in payload)
    if form == "any":
        return any(evaluate_trigger(sub, facts) for sub in payload)
    if form == "not":
        return not evaluate_trigger(payload, facts)
    # form == "field"
    field, op = payload["field"], payload["op"]
    value = facts.get(field, _MISSING)
    if op == "is_null":
        return value is _MISSING or value is None
    if op == "not_null":
        return value is not _MISSING and value is not None
    if value is _MISSING or value is None:
        return False
    expected = payload.get("value")
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op == "lt":
        return value < expected
    if op == "lte":
        return value <= expected
    if op == "gt":
        return value > expected
    if op == "gte":
        return value >= expected
    if op == "in":
        return value in expected
    if op == "not_in":
        return value not in expected
    if op == "is_true":
        return value is True
    if op == "is_false":
        return value is False
    raise ValueError(f"unknown operator: {op!r}")  # unreachable if validated


def _version_key(version: str) -> tuple[int, Any]:
    try:
        return (0, int(version))
    except (TypeError, ValueError):
        return (1, str(version))


def _as_naive_utc(moment: datetime) -> datetime:
    # SQLite returns naive datetimes for TZ-aware columns; Postgres returns
    # aware ones.  Normalize both sides so the effective window compares.
    return moment.replace(tzinfo=None) if moment.tzinfo is not None else moment


def select_rules(
    rules: Iterable[RequirementRule],
    facts: dict[str, Any],
    *,
    now: datetime | None = None,
) -> list[RequirementRule]:
    """Rules that apply to the facts: jurisdiction, effective window, trigger.

    Pure over rule objects (no session needed); per requirement_key the
highest version wins.
    """
    now_naive = _as_naive_utc(now or datetime.now(timezone.utc))
    state = facts.get("state")
    best: dict[str, RequirementRule] = {}
    for rule in rules:
        if rule.jurisdiction not in ("ALL", state):
            continue
        if rule.effective_from is not None and _as_naive_utc(rule.effective_from) > now_naive:
            continue
        if rule.effective_to is not None and _as_naive_utc(rule.effective_to) <= now_naive:
            continue
        current = best.get(rule.requirement_key)
        if current is None or _version_key(rule.version) > _version_key(current.version):
            best[rule.requirement_key] = rule
    return [rule for rule in best.values() if evaluate_trigger(rule.trigger, facts)]


class RequirementEngine:
    """Generates ``Requirement`` rows for a listing file.  Stateless."""

    def generate(self, session: Session, listing_file: ListingFile) -> list[Requirement]:
        facts = dict(listing_file.property_attributes or {})
        rules = session.query(RequirementRule).all()
        selected = select_rules(rules, facts)
        existing = {
            requirement.requirement_key: requirement
            for requirement in session.query(Requirement).filter(
                Requirement.listing_file_id == listing_file.id
            )
        }
        result: list[Requirement] = []
        for rule in selected:
            requirement = existing.get(rule.requirement_key)
            if requirement is None:
                requirement = Requirement(
                    listing_file_id=listing_file.id,
                    requirement_rule_id=rule.id,
                    requirement_key=rule.requirement_key,
                    requirement_type=rule.requirement_type,
                    status=rule.status,
                    state=RequirementState.PENDING.value,
                )
                session.add(requirement)
            result.append(requirement)
        session.flush()
        return result
