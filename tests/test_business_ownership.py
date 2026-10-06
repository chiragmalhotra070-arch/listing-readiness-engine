from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.models import BusinessIdentityOwnership, Customer, Document, Email
from app.db.session import Base
from app.domain.enums import DocumentType
from app.services.business_identity import claim_business_identity


def _session() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    return Session(engine)


def _context(db: Session, *, customer_value: str, customer: Customer | None = None, document_type: DocumentType = DocumentType.REMITTANCE, reference: str = "REM-OWN-001") -> tuple[Document, Customer]:
    email = Email(source_email_id=f"source-{customer_value}-{reference}-{document_type.value}-{db.query(Email).count()}", received_at=datetime.now(timezone.utc), sender_email="synthetic@example.test", status="RECEIVED")
    customer = customer or Customer(customer_id=customer_value, customer_name="Synthetic Customer")
    db.add_all([email, customer] if customer not in db else [email])
    db.flush()
    document = Document(email_id=email.id, document_name="synthetic.pdf", mime_type="application/pdf", document_type=document_type, extracted_data={"remittance_number": reference} if document_type == DocumentType.REMITTANCE else {"invoice_number": reference})
    db.add(document)
    db.flush()
    return document, customer


def test_first_claim_and_second_claim_are_owner_then_duplicate() -> None:
    db = _session()
    first, customer = _context(db, customer_value="CUST-OWN-001")
    first_claim = claim_business_identity(db, document=first, document_type=DocumentType.REMITTANCE, customer=customer, extracted_data=first.extracted_data)
    db.commit()

    second, _ = _context(db, customer_value="CUST-OWN-001", customer=customer)
    second_claim = claim_business_identity(db, document=second, document_type=DocumentType.REMITTANCE, customer=customer, extracted_data=second.extracted_data)

    assert first_claim.outcome == "OWNER"
    assert second_claim.outcome == "DUPLICATE"
    assert second_claim.owner_document_id == first.id
    assert db.scalar(select(BusinessIdentityOwnership).where(BusinessIdentityOwnership.owner_document_id == first.id)) is not None


def test_same_reference_isolated_by_customer_and_document_type() -> None:
    db = _session()
    first, first_customer = _context(db, customer_value="CUST-OWN-001")
    second, second_customer = _context(db, customer_value="CUST-OWN-002")
    invoice, _ = _context(db, customer_value="CUST-OWN-001", customer=first_customer, document_type=DocumentType.INVOICE)

    assert claim_business_identity(db, document=first, document_type=DocumentType.REMITTANCE, customer=first_customer, extracted_data=first.extracted_data).outcome == "OWNER"
    assert claim_business_identity(db, document=second, document_type=DocumentType.REMITTANCE, customer=second_customer, extracted_data=second.extracted_data).outcome == "OWNER"
    assert claim_business_identity(db, document=invoice, document_type=DocumentType.INVOICE, customer=first_customer, extracted_data=invoice.extracted_data).outcome == "OWNER"
    assert db.query(BusinessIdentityOwnership).count() == 3


def test_owner_reclaim_does_not_create_second_ownership_row() -> None:
    db = _session()
    document, customer = _context(db, customer_value="CUST-OWN-001")
    assert claim_business_identity(db, document=document, document_type=DocumentType.REMITTANCE, customer=customer, extracted_data=document.extracted_data).outcome == "OWNER"
    assert claim_business_identity(db, document=document, document_type=DocumentType.REMITTANCE, customer=customer, extracted_data=document.extracted_data).outcome == "OWNER"
    assert db.query(BusinessIdentityOwnership).count() == 1
