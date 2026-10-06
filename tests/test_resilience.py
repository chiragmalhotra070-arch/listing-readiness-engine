from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.models import AuditEvent, Document, ProcessingAttempt, SideEffectOperation
from app.db.session import Base, get_db
from app.main import app


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


def intake(client: TestClient, source: str, profile: str):
    return client.post("/v1/emails/intake", json={"source_email_id": source, "received_at": datetime.now(timezone.utc).isoformat(), "sender_email": "resilience@example.com", "applicability": True, "documents": [{"document_name": f"{profile}.pdf", "mime_type": "application/pdf", "mock_profile": profile}]})


def document(client: TestClient, source: str, profile: str) -> dict:
    return intake(client, source, profile).json()["documents"][0]


def test_ocr_provider_failover_records_a_failure_and_b_success(client: TestClient) -> None:
    result = document(client, "ocr-failover", "ocr_failover_success")
    attempts = client.get(f"/v1/documents/{result['id']}/attempts").json()
    ocr_attempts = [attempt for attempt in attempts if attempt["stage"] == "OCR"]
    assert [(attempt["provider"], attempt["status"]) for attempt in ocr_attempts] == [("OCR_PROVIDER_A", "RETRYABLE_FAILURE"), ("OCR_PROVIDER_B", "SUCCESS")]
    assert result["decision"] == "READY_FOR_PROCESSING"


def test_poor_quality_from_all_ocr_providers_is_needs_review(client: TestClient) -> None:
    result = document(client, "ocr-poor", "ocr_poor_both")
    assert result["decision"] == "NEEDS_REVIEW"
    assert result["stage_status"] == "NEEDS_REVIEW"
    assert result["retry_allowed"] is False


def test_both_ocr_providers_timeout_end_in_explicit_final_failure(client: TestClient) -> None:
    result = document(client, "ocr-exhausted", "ocr_both_timeout")
    assert result["decision"] == "FAILED"
    assert result["overall_status"] == "FINAL_FAILURE"
    assert result["stage_status"] == "FINAL_FAILURE"
    assert result["retryable"] is False
    assert result["retry_allowed"] is False


def test_crm_retry_schedules_then_succeeds_without_repeating_ocr(client: TestClient) -> None:
    ready = document(client, "crm-retry", "crm_timeout_then_success")
    before = client.get(f"/v1/documents/{ready['id']}/attempts").json()
    first = client.post(f"/v1/documents/{ready['id']}/process").json()
    assert first["decision"] == "FAILED"
    assert first["stage_status"] == "RETRY_SCHEDULED"
    assert first["overall_status"] == "PENDING_RETRY"
    assert first["next_attempt_at"] is not None
    second = client.post(f"/v1/documents/{ready['id']}/retry").json()
    assert second["decision"] == "PROCESSED"
    assert second["current_stage"] == "COMPLETE"
    after = client.get(f"/v1/documents/{ready['id']}/attempts").json()
    assert len([attempt for attempt in after if attempt["stage"] == "OCR"]) == len([attempt for attempt in before if attempt["stage"] == "OCR"])
    assert len([attempt for attempt in after if attempt["stage"] == "CRM_ACTION"]) == 2


def test_permanent_crm_failure_is_not_retryable(client: TestClient) -> None:
    ready = document(client, "crm-permanent", "crm_invalid_request")
    failed = client.post(f"/v1/documents/{ready['id']}/process").json()
    assert failed["decision"] == "FAILED"
    assert failed["overall_status"] == "FINAL_FAILURE"
    assert failed["retryable"] is False
    assert client.post(f"/v1/documents/{ready['id']}/retry").status_code == 409


def test_same_crm_operation_retry_does_not_duplicate_side_effect(client: TestClient):
    ready = document(client, "crm-idempotent", "crm_timeout_then_success")
    client.post(f"/v1/documents/{ready['id']}/process")
    processed = client.post(f"/v1/documents/{ready['id']}/retry").json()
    repeated = client.post(f"/v1/documents/{ready['id']}/process").json()
    assert processed["decision"] == repeated["decision"] == "PROCESSED"
    assert repeated["current_stage"] == "COMPLETE"
    attempts = client.get(f"/v1/documents/{ready['id']}/attempts").json()
    assert len([attempt for attempt in attempts if attempt["stage"] == "CRM_ACTION"]) == 2


def test_needs_review_does_not_enter_retry_loop(client: TestClient) -> None:
    result = document(client, "review-isolation", "conflicting_customer_candidates")
    assert result["decision"] == "NEEDS_REVIEW"
    assert client.post(f"/v1/documents/{result['id']}/retry").status_code == 409


def test_audit_retains_state_transitions_and_provider_fallback(client: TestClient) -> None:
    result = document(client, "audit-fallback", "ocr_failover_success")
    events = client.get(f"/v1/documents/{result['id']}/audit").json()
    event_types = [event["event_type"] for event in events]
    assert "PROCESSING_STARTED" not in event_types
    assert "STAGE_FAILED" in event_types
    assert "PROVIDER_FALLBACK" in event_types
    assert "DECISION_MADE" in event_types


def test_processing_attempts_include_provider_quality_and_failure_data(client: TestClient) -> None:
    result = document(client, "attempt-details", "ocr_failover_success")
    attempts = client.get(f"/v1/documents/{result['id']}/attempts").json()
    ocr_attempts = [attempt for attempt in attempts if attempt["stage"] == "OCR"]
    assert ocr_attempts[0]["failure_code"] == "NETWORK_TIMEOUT"
    assert ocr_attempts[1]["quality_score"] == 0.92


def test_two_documents_keep_independent_resilience_lifecycles(client: TestClient) -> None:
    response = client.post("/v1/emails/intake", json={"source_email_id": "two-resilience-docs", "received_at": datetime.now(timezone.utc).isoformat(), "sender_email": "resilience@example.com", "applicability": True, "documents": [{"document_name": "ocr_failover_success.pdf", "mime_type": "application/pdf", "mock_profile": "ocr_failover_success"}, {"document_name": "ocr_poor_both.pdf", "mime_type": "application/pdf", "mock_profile": "ocr_poor_both"}]})
    documents = response.json()["documents"]
    assert [item["decision"] for item in documents] == ["READY_FOR_PROCESSING", "NEEDS_REVIEW"]
    assert documents[0]["current_stage"] == "COMPLETE"
    assert documents[1]["current_stage"] == "OCR"
