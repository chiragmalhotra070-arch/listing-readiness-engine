from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.models import BusinessIdentityOwnership, Customer, Document
from app.domain.enums import DocumentType
from app.services.duplicate_detection import duplicate_key


@dataclass(frozen=True)
class OwnershipClaim:
    outcome: str
    owner_document_id: int | None = None
    business_identity: str | None = None

    @property
    def is_owner(self) -> bool:
        return self.outcome == "OWNER"

    @property
    def is_duplicate(self) -> bool:
        return self.outcome == "DUPLICATE"


def normalized_business_identity(document_type: DocumentType, extracted_data: dict[str, Any] | None) -> str | None:
    key = duplicate_key(document_type, "identity-customer", extracted_data)
    if key.value is None:
        return None
    return json.dumps(list(key.value[1:]), ensure_ascii=False, separators=(",", ":"), default=str)


def claim_business_identity(
    db: Session,
    *,
    document: Document,
    document_type: DocumentType,
    customer: Customer,
    extracted_data: dict[str, Any] | None,
) -> OwnershipClaim:
    identity = normalized_business_identity(document_type, extracted_data)
    if identity is None:
        return OwnershipClaim("NOT_APPLICABLE")

    existing = db.scalar(
        select(BusinessIdentityOwnership).where(
            BusinessIdentityOwnership.document_type == document_type,
            BusinessIdentityOwnership.customer_id == customer.id,
            BusinessIdentityOwnership.business_identity == identity,
        )
    )
    if existing is not None:
        if existing.owner_document_id == document.id:
            return OwnershipClaim("OWNER", document.id, identity)
        return OwnershipClaim("DUPLICATE", existing.owner_document_id, identity)

    legacy_owner = _find_legacy_owner(db, document, document_type, customer, identity)
    if legacy_owner is not None:
        return OwnershipClaim("DUPLICATE", legacy_owner.id, identity)

    ownership = BusinessIdentityOwnership(
        document_type=document_type,
        customer_id=customer.id,
        business_identity=identity,
        owner_document_id=document.id,
    )
    try:
        with db.begin_nested():
            db.add(ownership)
            db.flush()
    except IntegrityError:
        existing = db.scalar(
            select(BusinessIdentityOwnership).where(
                BusinessIdentityOwnership.document_type == document_type,
                BusinessIdentityOwnership.customer_id == customer.id,
                BusinessIdentityOwnership.business_identity == identity,
            )
        )
        if existing is not None:
            return OwnershipClaim("DUPLICATE", existing.owner_document_id, identity)
        legacy_owner = _find_legacy_owner(db, document, document_type, customer, identity)
        if legacy_owner is not None:
            return OwnershipClaim("DUPLICATE", legacy_owner.id, identity)
        raise

    return OwnershipClaim("OWNER", document.id, identity)


def _find_legacy_owner(
    db: Session,
    document: Document,
    document_type: DocumentType,
    customer: Customer,
    identity: str,
) -> Document | None:
    candidates = db.scalars(
        select(Document).where(
            Document.document_type == document_type,
            Document.customer_id == customer.id,
            Document.id != document.id,
        )
    )
    for candidate in candidates:
        if normalized_business_identity(document_type, candidate.extracted_data) == identity:
            return candidate
    return None
