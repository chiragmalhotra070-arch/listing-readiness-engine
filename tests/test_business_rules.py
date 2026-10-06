from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.models import Customer
from app.domain.enums import BusinessOutcome, DocumentType
from app.adapters.customer import CustomerLookupAdapter
from app.schemas import CustomerCandidates
from app.services.customer_resolution import resolve_customer
from app.services.decision_engine import DecisionInput, decide
from app.services.duplicate_detection import duplicate_key
from app.services.business_identity import normalized_business_identity
from app.services.validation import validate_required_fields


def test_conflicting_strong_customer_evidence_needs_review() -> None:
    result = resolve_customer(sender_email_customer_id="A", email_address_customer_id="B")
    assert result.needs_review is True


def customer_db() -> Session:
    engine = create_engine("sqlite://")
    from app.db.session import Base
    Base.metadata.create_all(engine)
    return Session(engine)


def test_customer_lookup_uses_candidates_separately_from_document_fields() -> None:
    db = customer_db()
    result = CustomerLookupAdapter().lookup(
        db=db,
        extracted_data={"remittance_number": "REM-001", "customer_id": "CUST-999"},
        customer_candidates=CustomerCandidates(customer_id_candidates=["CUST-001"]),
    )
    assert result.customer_id == "CUST-001"
    assert result.customer_type == "EXISTING_CUSTOMER"
    assert result.strong_ids == {"CUST-001"}


def test_customer_lookup_matches_name_and_normalizes_whitespace_and_case() -> None:
    result = CustomerLookupAdapter().lookup(db=customer_db(), extracted_data={}, customer_candidates=CustomerCandidates(customer_name_candidates=["  la   galerie "]))
    assert result.customer_id == "LG001"
    assert result.customer_type == "EXISTING_CUSTOMER"


def test_unknown_customer_is_new_customer() -> None:
    result = CustomerLookupAdapter().lookup(db=customer_db(), extracted_data={}, customer_candidates=CustomerCandidates(customer_id_candidates=["XYZ999"], customer_name_candidates=["New Company"]))
    assert result.customer_id is None
    assert result.customer_type == "NEW_CUSTOMER"


def test_multiple_customer_candidates_are_not_resolved_by_first_candidate() -> None:
    result = CustomerLookupAdapter().lookup(db=customer_db(), extracted_data={"remittance_number": "REM-001"}, customer_candidates=CustomerCandidates(customer_id_candidates=["CUST-001", "CUST-002"]))
    assert result.customer_id is None
    assert result.customer_type == "NEEDS_REVIEW"
    assert result.strong_ids == {"CUST-001", "CUST-002"}


def test_conflicting_id_and_name_needs_review() -> None:
    result = CustomerLookupAdapter().lookup(db=customer_db(), extracted_data={}, customer_candidates=CustomerCandidates(customer_id_candidates=["CUST-001"], customer_name_candidates=["Awthentikz"]))
    assert result.customer_type == "NEEDS_REVIEW"
    assert result.customer_id is None


def test_required_field_validation_ignores_customer_identity() -> None:
    result = validate_required_fields(DocumentType.REMITTANCE, {"remittance_number": "REM-001", "customer_id": "CUST-001"})
    assert result.valid is True
    assert result.missing_fields == ()


def test_duplicate_key_uses_resolved_customer_and_document_identifier() -> None:
    result = duplicate_key(DocumentType.REMITTANCE, "CUST-001", {"remittance_number": "REM-001"})
    assert result.value == ("CUST-001", "REM-001")


def test_duplicate_key_requires_business_identifier() -> None:
    result = duplicate_key(DocumentType.REMITTANCE, "customer-1", {})
    assert result.value is None


def test_business_identity_is_normalized_without_customer_identifier() -> None:
    assert normalized_business_identity(DocumentType.REMITTANCE, {"remittance_number": "REM-001"}) == '["REM-001"]'


def test_business_identity_differs_by_document_type() -> None:
    remittance = normalized_business_identity(DocumentType.REMITTANCE, {"remittance_number": "REF-001"})
    invoice = normalized_business_identity(DocumentType.INVOICE, {"invoice_number": "REF-001"})
    assert remittance == invoice


def test_valid_document_is_ready_not_processed() -> None:
    result = decide(DecisionInput(True, DocumentType.REMITTANCE, 0.96, False, False, True, False, False, True))
    assert result.outcome == BusinessOutcome.READY_FOR_PROCESSING


def test_not_applicable_precedes_other_checks() -> None:
    result = decide(DecisionInput(False, DocumentType.REMITTANCE, 0.20, False, True, False, True, True, False))
    assert result.outcome == BusinessOutcome.NOT_APPLICABLE


def test_contradiction_overrides_confidence() -> None:
    result = decide(DecisionInput(True, DocumentType.REMITTANCE, 0.99, True, False, True, False, True, True))
    assert result.outcome == BusinessOutcome.NEEDS_REVIEW
