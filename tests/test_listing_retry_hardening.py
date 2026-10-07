"""Retry & idempotency hardening (audit close-out slice).

Seam: the n8n-visible HTTP API, same TestClient style as
``test_listing_api``.  Covers the brief's seven proofs:

1. same idempotency key twice -> one file, second returns existing (200)
2. same bytes uploaded twice -> one document row (200)
3. different bytes -> two documents (201)
4. transient classify failure -> 503, retried, then succeeds
5. exhausted document -> ``processing_failed`` bucket populated
6. 503/422/500 mapping per LLMError variant
7. full suite green (run outside this file)
"""

from __future__ import annotations

from pathlib import Path

from app.adapters.llm import LLMError
from app.db.models import Document, ListingFile
from app.services.classification import LLMDocumentClassifier

DEMO_ROOT = Path(__file__).resolve().parent.parent / "demo_documents"
CA_FACTS = {
    "state": "CA",
    "year_built": 1968,
    "property_type": "single_family",
    "hoa": True,
    "solar": True,
    "septic": False,
    "seller_type": "individual",
}


def _create(client, *, idempotency_key: str | None = None, apn: str = "5842-018-024"):
    body: dict = {
        "property_address": "123 Main St, Pasadena, CA 91101",
        "apn": apn,
        "seller_name": "Jane Seller",
        "property_attributes": CA_FACTS,
    }
    if idempotency_key is not None:
        body["idempotency_key"] = idempotency_key
    return client.post("/v1/listing-files", json=body)


def test_same_idempotency_key_returns_existing_file(client, db):
    first = _create(client, idempotency_key="webhook-run-1")
    assert first.status_code == 201, first.text

    second = _create(client, idempotency_key="webhook-run-1")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first.json()["id"]

    assert db.query(ListingFile).count() == 1


def _upload(client, file_id: int, *, filename: str = "demo-ca-tds.pdf", content: bytes | None = None):
    if content is None:
        content = (DEMO_ROOT / "completed" / "demo-ca-tds.pdf").read_bytes()
    return client.post(
        f"/v1/listing-files/{file_id}/documents",
        files={"file": (filename, content, "application/pdf")},
    )


def test_same_bytes_uploaded_twice_returns_existing_document(client, db):
    file_id = _create(client, idempotency_key="upload-dup").json()["id"]

    first = _upload(client, file_id)
    assert first.status_code == 201, first.text

    second = _upload(client, file_id)
    assert second.status_code == 200, second.text
    assert second.json()["document_id"] == first.json()["document_id"]

    rows = db.query(Document).filter(Document.listing_file_id == file_id).count()
    assert rows == 1


def test_different_bytes_create_two_documents(client, db):
    file_id = _create(client, idempotency_key="upload-distinct").json()["id"]

    first = _upload(client, file_id)
    assert first.status_code == 201, first.text

    second = _upload(
        client,
        file_id,
        filename="demo-ca-spq.pdf",
        content=(DEMO_ROOT / "completed" / "demo-ca-spq.pdf").read_bytes(),
    )
    assert second.status_code == 201, second.text
    assert second.json()["document_id"] != first.json()["document_id"]

    rows = db.query(Document).filter(Document.listing_file_id == file_id).count()
    assert rows == 2


def _classified_document(client, *, idempotency_key: str) -> int:
    file_id = _create(client, idempotency_key=idempotency_key).json()["id"]
    return _upload(client, file_id).json()["document_id"]


def _raise(error: LLMError):
    def _classify(self, **kwargs):
        raise error

    return _classify


def test_classify_status_mapping_per_llm_error_variant(client, monkeypatch):
    doc_id = _classified_document(client, idempotency_key="map-retryable")

    # transient LLM failure -> 503: n8n can retry the node.
    monkeypatch.setattr(LLMDocumentClassifier, "classify", _raise(LLMError("provider timed out", code="LLM_TIMEOUT", retryable=True)))
    response = client.post(f"/v1/documents/{doc_id}/classify")
    assert response.status_code == 503, response.text

    # needs_review stays 422: the document goes to a human, not a retry.
    monkeypatch.setattr(LLMDocumentClassifier, "classify", _raise(LLMError("response failed structured validation", code="LLM_SCHEMA_VALIDATION_ERROR", retryable=False, needs_review=True)))
    response = client.post(f"/v1/documents/{doc_id}/classify")
    assert response.status_code == 422, response.text

    # permanent, non-review failure -> 500: no retry will save it.
    monkeypatch.setattr(LLMDocumentClassifier, "classify", _raise(LLMError("provider configuration is incomplete", code="LLM_AUTHENTICATION_ERROR", retryable=False)))
    response = client.post(f"/v1/documents/{doc_id}/classify")
    assert response.status_code == 500, response.text

    # chassis-retryable code set (classify_error) still maps to 503 even if
    # the adapter forgot the retryable flag -- shared taxonomy, no new codes.
    monkeypatch.setattr(LLMDocumentClassifier, "classify", _raise(LLMError("upstream 503", code="HTTP_503", retryable=False)))
    response = client.post(f"/v1/documents/{doc_id}/classify")
    assert response.status_code == 503, response.text


def test_transient_classify_failure_retried_then_succeeds(client, monkeypatch):
    doc_id = _classified_document(client, idempotency_key="transient-retry")

    original = LLMDocumentClassifier.classify
    state = {"calls": 0}

    def flaky(self, **kwargs):
        state["calls"] += 1
        if state["calls"] == 1:
            raise LLMError("LLM provider timed out", code="LLM_TIMEOUT", retryable=True)
        return original(self, **kwargs)

    monkeypatch.setattr(LLMDocumentClassifier, "classify", flaky)

    first = client.post(f"/v1/documents/{doc_id}/classify")
    assert first.status_code == 503, first.text

    second = client.post(f"/v1/documents/{doc_id}/classify")
    assert second.status_code == 200, second.text
    assert second.json()["document_type"] == "CA_TDS"
    assert state["calls"] == 2


def test_exhausted_document_lands_in_processing_failed_bucket(client):
    file_id = _create(client, idempotency_key="pf-1").json()["id"]
    doc_id = _upload(client, file_id).json()["document_id"]

    queue = client.get(f"/v1/listing-files/{file_id}/review-queue").json()
    assert queue["processing_failed"] == []

    payload = {"stage": "CLASSIFICATION", "reason": "node retries exhausted: LLM provider timed out"}
    first = client.post(f"/v1/listing-files/{file_id}/documents/{doc_id}/processing-failed", json=payload)
    assert first.status_code == 200, first.text
    assert first.json() == {
        "document_id": doc_id,
        "stage": "CLASSIFICATION",
        "reason": payload["reason"],
        "attempt_count": 1,
    }

    queue = client.get(f"/v1/listing-files/{file_id}/review-queue").json()
    assert queue["processing_failed"] == [
        {"document_id": doc_id, "stage": "CLASSIFICATION", "reason": payload["reason"], "attempt_count": 1}
    ]

    # a later exhaustion at the same stage increments the attempt count,
    # it does not add a second bucket row
    second = client.post(f"/v1/listing-files/{file_id}/documents/{doc_id}/processing-failed", json=payload)
    assert second.status_code == 200, second.text
    assert second.json()["attempt_count"] == 2

    queue = client.get(f"/v1/listing-files/{file_id}/review-queue").json()
    assert len(queue["processing_failed"]) == 1
    assert queue["processing_failed"][0]["attempt_count"] == 2

    # a different stage is its own bucket row
    other = client.post(
        f"/v1/listing-files/{file_id}/documents/{doc_id}/processing-failed",
        json={"stage": "EXTRACT", "reason": "node retries exhausted: upstream 500"},
    )
    assert other.status_code == 200, other.text
    queue = client.get(f"/v1/listing-files/{file_id}/review-queue").json()
    assert {(row["stage"], row["attempt_count"]) for row in queue["processing_failed"]} == {
        ("CLASSIFICATION", 2),
        ("EXTRACT", 1),
    }

    # every report is audit-backed
    events = client.get(f"/v1/documents/{doc_id}/audit").json()
    failed = [event for event in events if event["event_type"] == "PROCESSING_FAILED"]
    assert [event["status"] for event in failed] == ["FAILURE", "FAILURE", "FAILURE"]
    assert failed[0]["details"]["attempt_count"] == 1

    # stage and reason are required and non-empty -- the taxonomy is the
    # documented pipeline stage name, not a free-text field to abuse
    for bad in ({"stage": "", "reason": "x"}, {"stage": "CLASSIFICATION", "reason": " "}):
        rejected = client.post(
            f"/v1/listing-files/{file_id}/documents/{doc_id}/processing-failed",
            json=bad,
        )
        assert rejected.status_code == 422, rejected.text


def test_processing_failed_for_unknown_document_404s(client):
    file_id = _create(client, idempotency_key="pf-404").json()["id"]

    missing_doc = client.post(
        f"/v1/listing-files/{file_id}/documents/99999/processing-failed",
        json={"stage": "CLASSIFICATION", "reason": "exhausted"},
    )
    assert missing_doc.status_code == 404, missing_doc.text

    other_file = _create(client, idempotency_key="pf-other").json()["id"]
    doc_id = _upload(client, other_file).json()["document_id"]
    wrong_file = client.post(
        f"/v1/listing-files/{file_id}/documents/{doc_id}/processing-failed",
        json={"stage": "CLASSIFICATION", "reason": "exhausted"},
    )
    assert wrong_file.status_code == 404, wrong_file.text
