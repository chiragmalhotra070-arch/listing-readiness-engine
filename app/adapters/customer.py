from __future__ import annotations

from dataclasses import dataclass
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Customer
from app.schemas import CustomerCandidates


@dataclass(frozen=True)
class CustomerMatch:
    customer_id: str | None
    customer_name: str | None
    strong_ids: set[str]
    supporting_ids: set[str]
    customer_type: str
    reason: str


class CustomerLookupAdapter:
    @staticmethod
    def _normalize_name(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip()).casefold()

    def lookup(self, *, db: Session, extracted_data: dict[str, object], customer_candidates: CustomerCandidates | None = None) -> CustomerMatch:
        candidates = customer_candidates or CustomerCandidates()
        candidate_ids = candidates.customer_id_candidates or [
            str(extracted_data[name])
            for name in ("customer_id", "sender_customer_id")
            if extracted_data.get(name)
        ]
        strong_ids = {str(value).strip() for value in candidate_ids if str(value).strip()}
        customers = list(db.scalars(select(Customer)).all())
        customers_by_id = {customer.customer_id: customer for customer in customers}
        names = {self._normalize_name(name) for name in candidates.customer_name_candidates if name.strip()}
        name_matches = {
            customer.id: customer
            for customer in customers
            if self._normalize_name(getattr(customer, "legal_name", getattr(customer, "customer_name", ""))) in names
            or any(self._normalize_name(alias) in names for alias in (getattr(customer, "aliases", None) or []))
        }

        if len(strong_ids) > 1:
            return CustomerMatch(None, None, strong_ids, set(), "NEEDS_REVIEW", "multiple customer identities found in one document")

        id_match = customers_by_id.get(next(iter(strong_ids))) if strong_ids else None
        matched_name_ids = set(name_matches)
        if id_match and matched_name_ids and matched_name_ids != {id_match.id}:
            return CustomerMatch(None, None, strong_ids, set(), "NEEDS_REVIEW", "customer ID and customer name identify different customers")
        if id_match:
            return CustomerMatch(id_match.customer_id, getattr(id_match, "legal_name", getattr(id_match, "customer_name", None)), strong_ids, set(), "EXISTING_CUSTOMER", "customer ID matched existing customer")
        if len(matched_name_ids) > 1:
            return CustomerMatch(None, None, strong_ids, set(), "NEEDS_REVIEW", "multiple customer names matched different existing customers")
        if len(matched_name_ids) == 1:
            customer = next(iter(name_matches.values()))
            return CustomerMatch(customer.customer_id, getattr(customer, "legal_name", getattr(customer, "customer_name", None)), strong_ids, set(), "EXISTING_CUSTOMER", "customer name or alias matched an existing customer")
        return CustomerMatch(None, None, strong_ids, set(), "NEW_CUSTOMER", "customer was not found in the customer master")
