from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CustomerResolution:
    customer_id: str | None
    needs_review: bool
    reason: str


def resolve_customer(*, sender_email_customer_id: str | None = None, email_address_customer_id: str | None = None, customer_id: str | None = None, supporting_customer_ids: set[str] | None = None) -> CustomerResolution:
    strong = {value for value in (sender_email_customer_id, email_address_customer_id, customer_id) if value}
    if len(strong) > 1:
        return CustomerResolution(None, True, "conflicting strong customer evidence")
    if len(strong) == 1:
        resolved = next(iter(strong))
        if supporting_customer_ids and resolved not in supporting_customer_ids:
            return CustomerResolution(None, True, "strong and supporting customer evidence conflict")
        return CustomerResolution(resolved, False, "resolved from strong customer evidence")
    if supporting_customer_ids and len(supporting_customer_ids) == 1:
        return CustomerResolution(next(iter(supporting_customer_ids)), False, "resolved from supporting evidence")
    return CustomerResolution(None, True, "customer could not be resolved unambiguously")
