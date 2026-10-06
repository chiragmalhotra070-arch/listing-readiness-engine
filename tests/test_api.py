from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.db.session import Base, get_db
from app.main import app


def text_pdf_bytes() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length 92 >>\nstream\nBT /F1 12 Tf 20 250 Td (Remittance Number: REM-MULTI-001) Tj 0 -20 Td (Customer ID: CUST-001) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode() + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(output)


@pytest.fixture
def client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"})
    app.dependency_overrides.clear()


def intake(client: TestClient, source_email_id: str, *profiles: str):
    return client.post(
        "/v1/emails/intake",
        json={
            "source_email_id": source_email_id,
            "received_at": datetime.now(timezone.utc).isoformat(),
            "sender_email": "sender@example.com",
            "applicability": True,
            "documents": [{"document_name": f"{profile}.pdf", "mime_type": "application/pdf", "mock_profile": profile} for profile in profiles],
        },
    )


def test_case_1_valid_remittance_is_ready_for_processing(client: TestClient) -> None:
    response = intake(client, "case-1", "valid_remittance")
    assert response.status_code == 201
    assert response.json()["documents"][0]["decision"] == "READY_FOR_PROCESSING"


def test_case_2_known_duplicate_remittance(client: TestClient) -> None:
    assert intake(client, "case-2-first", "duplicate_remittance").json()["documents"][0]["decision"] == "READY_FOR_PROCESSING"
    response = intake(client, "case-2-second", "duplicate_remittance")
    assert response.json()["documents"][0]["decision"] == "DUPLICATE"


def test_case_3_unknown_customer_is_new_customer_and_continues(client: TestClient) -> None:
    document = intake(client, "case-3", "unknown_customer").json()["documents"][0]
    assert document["decision"] == "READY_FOR_PROCESSING"
    assert document["customer_type"] == "NEW_CUSTOMER"
    assert document["customer_id"] is None


def test_case_4_conflicting_strong_customer_identifiers(client: TestClient) -> None:
    assert intake(client, "case-4", "conflicting_customer").json()["documents"][0]["decision"] == "NEEDS_REVIEW"


def test_case_5_low_ocr_successful_fallback_continues(client: TestClient) -> None:
    response = intake(client, "case-5", "low_ocr_fallback_success")
    document = response.json()["documents"][0]
    assert document["decision"] == "READY_FOR_PROCESSING"
    assert document["evidence"]["extraction"]["fallback_used"] is True


def test_case_6_low_ocr_after_fallback_needs_review(client: TestClient) -> None:
    assert intake(client, "case-6", "low_ocr_fallback_failure").json()["documents"][0]["decision"] == "NEEDS_REVIEW"


def test_case_7_not_applicable(client: TestClient) -> None:
    assert intake(client, "case-7", "not_applicable").json()["documents"][0]["decision"] == "NOT_APPLICABLE"


@pytest.mark.parametrize(("profile", "retryable"), [("retryable_failure", True), ("permanent_failure", False)])
def test_cases_8_and_9_technical_failures_are_classified(client: TestClient, profile: str, retryable: bool) -> None:
    document = intake(client, f"case-{profile}", profile).json()["documents"][0]
    assert document["decision"] == "FAILED"
    assert document["processing_status"] == "FAILED"
    assert document["retryable"] is False
    assert document["failure_type"] == ("RETRYABLE" if retryable else "PERMANENT")


def test_case_10_two_documents_have_independent_decisions(client: TestClient) -> None:
    documents = intake(client, "case-10", "valid_remittance", "unknown_customer").json()["documents"]
    assert [document["decision"] for document in documents] == ["READY_FOR_PROCESSING", "READY_FOR_PROCESSING"]
    assert [document["customer_type"] for document in documents] == ["EXISTING_CUSTOMER", "NEW_CUSTOMER"]
    assert documents[0]["id"] != documents[1]["id"]


def test_case_11_same_email_is_idempotent(client: TestClient) -> None:
    first = intake(client, "case-11", "valid_remittance")
    second = intake(client, "case-11", "valid_remittance")
    assert first.json()["email_id"] == second.json()["email_id"]
    assert second.json()["idempotent"] is True
    assert len(second.json()["documents"]) == 1


def test_case_12_duplicate_logic_uses_business_identifier(client: TestClient) -> None:
    documents = intake(client, "case-12", "valid_remittance", "valid_remittance_2").json()["documents"]
    assert [document["decision"] for document in documents] == ["READY_FOR_PROCESSING", "READY_FOR_PROCESSING"]


def test_ready_document_can_become_processed_after_mock_crm_action(client: TestClient) -> None:
    document = intake(client, "case-crm", "valid_remittance").json()["documents"][0]
    response = client.post(f"/v1/documents/{document['id']}/process")
    assert response.json()["decision"] == "PROCESSED"


def test_invalid_api_key_is_rejected_before_processing(client: TestClient) -> None:
    response = client.post("/v1/emails/intake", headers={"X-Intake-API-Key": "wrong-key"}, json={"source_email_id": "invalid-key", "received_at": datetime.now(timezone.utc).isoformat(), "sender_email": "sender@example.com", "documents": [{"document_name": "invoice.pdf", "mime_type": "application/pdf", "mock_profile": "valid_remittance"}]})
    assert response.status_code == 401


def test_multipart_intake_accepts_multiple_binary_attachments(client: TestClient) -> None:
    metadata = {
        "source_email_id": "multipart-email",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "sender@example.com",
        "correlation_id": "corr-multipart",
        "documents": [
            {"document_name": "invoice.pdf", "mime_type": "application/pdf", "mock_profile": "valid_remittance"},
            {"document_name": "remittance.pdf", "mime_type": "application/pdf", "mock_profile": "duplicate_remittance"},
        ],
    }
    response = client.post(
        "/v1/emails/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[("files", ("invoice.pdf", text_pdf_bytes(), "application/pdf")), ("files", ("remittance.pdf", text_pdf_bytes(), "application/pdf"))],
    )
    assert response.status_code == 201
    assert response.json()["correlation_id"] == "corr-multipart"
    assert len(response.json()["documents"]) == 2
    assert all(document["evidence"] for document in response.json()["documents"])
