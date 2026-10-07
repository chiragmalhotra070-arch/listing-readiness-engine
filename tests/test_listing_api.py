"""Slice 7a: listing stage endpoints -- the n8n-visible API surface.

Thin wrappers over the tested services (requirement engine, classifier,
reconciliation, readiness), auth via the existing intake API key.  Each
test drives one endpoint through the FastAPI TestClient; the provider is
the mock one (``conftest.client``), so the demo documents classify from
their fixture profiles.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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


def _create_listing_file(client, *, property_attributes: dict | None = None) -> dict:
    response = client.post(
        "/v1/listing-files",
        json={
            "property_address": "123 Main St, Pasadena, CA 91101",
            "apn": "5842-018-024",
            "seller_name": "Jane Seller",
            "property_attributes": CA_FACTS if property_attributes is None else property_attributes,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_create_listing_file_returns_id(client):
    body = _create_listing_file(client)

    assert set(body) == {"id", "customer_id", "customer_resolution"}
    assert isinstance(body["id"], int)


def test_create_listing_file_requires_state_in_attributes(client):
    response = client.post(
        "/v1/listing-files",
        json={"property_address": "123 Main St", "property_attributes": {"year_built": 1968}},
    )

    assert response.status_code == 422
    assert "state" in response.text


def test_listing_endpoints_require_api_key(client):
    unauthenticated = client.post(
        "/v1/listing-files",
        json={"property_attributes": CA_FACTS},
        headers={"X-Intake-API-Key": "wrong-key"},
    )

    assert unauthenticated.status_code == 401


def test_generate_requirements_creates_twelve_for_full_ca_file(client):
    file_id = _create_listing_file(client)["id"]

    response = client.post(f"/v1/listing-files/{file_id}/generate-requirements")

    assert response.status_code == 200, response.text
    requirements = response.json()["requirements"]
    assert len(requirements) == 12
    assert {requirement["requirement_key"] for requirement in requirements} == {
        "listing_agreement", "seller_advisory", "agency_disclosure", "ca_tds", "ca_spq",
        "agent_visual_inspection", "ca_nhd", "wcmd_advisory", "lead_disclosure",
        "hoa_package", "solar_agreement", "prelim_title_report",
    }
    assert all(set(requirement) == {"requirement_key", "requirement_type", "status", "state"} for requirement in requirements)
    assert all(requirement["state"] == "PENDING" for requirement in requirements)


def test_generate_requirements_is_idempotent(client):
    file_id = _create_listing_file(client)["id"]
    first = client.post(f"/v1/listing-files/{file_id}/generate-requirements").json()["requirements"]
    second = client.post(f"/v1/listing-files/{file_id}/generate-requirements").json()["requirements"]

    assert len(second) == len(first) == 12
    assert {requirement["requirement_key"] for requirement in second} == {requirement["requirement_key"] for requirement in first}


def test_generate_requirements_unknown_file_is_404(client):
    response = client.post("/v1/listing-files/9999/generate-requirements")

    assert response.status_code == 404


def _upload(client, file_id: int, *, filename: str = "demo-ca-tds.pdf", content: bytes | None = None, mime_type: str = "application/pdf"):
    payload = (DEMO_ROOT / "completed" / filename).read_bytes() if content is None else content
    return client.post(
        f"/v1/listing-files/{file_id}/documents",
        files={"file": (filename, payload, mime_type)},
    )


def test_upload_document_returns_document_id(client):
    file_id = _create_listing_file(client)["id"]

    response = _upload(client, file_id)

    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"document_id"}
    assert isinstance(body["document_id"], int)


def test_upload_document_unknown_listing_file_is_404(client):
    response = _upload(client, 9999)

    assert response.status_code == 404


def test_upload_document_rejects_content_that_is_not_a_pdf(client):
    file_id = _create_listing_file(client)["id"]

    response = _upload(client, file_id, content=b"not a pdf at all")

    assert response.status_code == 422


def _upload_as(client, file_id: int, filename: str) -> int:
    # Real per-name bytes when the demo fixture exists.  Non-demo names fall
    # back to the TDS bytes tagged with the filename (a trailing PDF comment
    # pypdf ignores) so each logical document has its own content hash --
    # content-hash dedup would otherwise collapse same-bytes uploads.
    path = DEMO_ROOT / "completed" / filename
    if path.exists():
        content = path.read_bytes()
    else:
        content = (DEMO_ROOT / "completed" / "demo-ca-tds.pdf").read_bytes() + f"\n% {filename}\n".encode()
    response = _upload(client, file_id, filename=filename, content=content)
    assert response.status_code == 201, response.text
    return response.json()["document_id"]


def test_classify_demo_document_returns_type_and_confidence(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")

    response = client.post(f"/v1/documents/{document_id}/classify")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document_id
    assert body["document_type"] == "CA_TDS"
    assert 0.0 < body["classification_confidence"] <= 1.0


def test_classify_reports_unknown_as_an_honest_outcome(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "unknown_document.pdf")

    response = client.post(f"/v1/documents/{document_id}/classify")

    assert response.status_code == 200, response.text
    assert response.json()["document_type"] == "UNKNOWN"


def test_classify_unknown_document_is_404(client):
    response = client.post("/v1/documents/9999/classify")

    assert response.status_code == 404


def test_classify_maps_transient_provider_failure_to_503(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "provider_timeout.pdf")

    response = client.post(f"/v1/documents/{document_id}/classify")

    # retryable LLM failure: 503 tells the orchestrator to retry the node
    # (the old 502 conflated this with permanent failures).
    assert response.status_code == 503


def test_extract_returns_per_type_fields_after_classify(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{document_id}/classify")

    response = client.post(f"/v1/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_id"] == document_id
    assert body["document_type"] == "CA_TDS"
    assert body["extracted_data"]["apn"] == "5842-018-024"
    assert body["extracted_data"]["seller_name"] == "Jane Seller"


def test_extract_runs_the_provider_when_classify_was_skipped(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")

    response = client.post(f"/v1/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["document_type"] == "UNKNOWN"
    assert body["extracted_data"]["apn"] == "5842-018-024"


def test_extract_unknown_document_is_404(client):
    response = client.post("/v1/documents/9999/extract")

    assert response.status_code == 404


def test_reconcile_moves_matched_requirement_to_received(client):
    file_id = _create_listing_file(client)["id"]
    client.post(f"/v1/listing-files/{file_id}/generate-requirements")
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{document_id}/classify")

    response = client.post(f"/v1/listing-files/{file_id}/reconcile")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "evidence_created",
        "requirements_moved_to_received",
        "unmet_requirement_keys",
        "unmatched_document_ids",
    }
    assert body["evidence_created"] == 1
    assert body["requirements_moved_to_received"] == ["ca_tds"]
    assert "ca_tds" not in body["unmet_requirement_keys"]
    assert len(body["unmet_requirement_keys"]) == 11
    assert body["unmatched_document_ids"] == []


def test_reconcile_without_requirements_returns_empty_result(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{document_id}/classify")

    response = client.post(f"/v1/listing-files/{file_id}/reconcile")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["evidence_created"] == 0
    assert body["requirements_moved_to_received"] == []
    assert body["unmet_requirement_keys"] == []
    assert body["unmatched_document_ids"] == []


def test_reconcile_unknown_listing_file_is_404(client):
    response = client.post("/v1/listing-files/9999/reconcile")

    assert response.status_code == 404


DEMO_COMPLETED = [
    "demo-listing-agreement.pdf",
    "demo-seller-advisory.pdf",
    "demo-agency-disclosure.pdf",
    "demo-ca-tds.pdf",
    "demo-ca-spq.pdf",
    "demo-agent-visual-inspection.pdf",
    "demo-ca-nhd.pdf",
    "demo-wcmd-advisory.pdf",
    "demo-lead-disclosure.pdf",
    "demo-hoa-package.pdf",
    "demo-prelim-title-report.pdf",
    "demo-solar-agreement.pdf",
]


def _ingest_demo_documents(client, file_id: int, filenames: list[str]) -> list[int]:
    document_ids = []
    for filename in filenames:
        document_id = _upload_as(client, file_id, filename)
        assert client.post(f"/v1/documents/{document_id}/classify").status_code == 200
        assert client.post(f"/v1/documents/{document_id}/extract").status_code == 200
        document_ids.append(document_id)
    return document_ids


def test_verdict_is_not_ready_while_required_documents_are_missing(client):
    file_id = _create_listing_file(client)["id"]
    client.post(f"/v1/listing-files/{file_id}/generate-requirements")
    _ingest_demo_documents(client, file_id, ["demo-ca-tds.pdf"])
    client.post(f"/v1/listing-files/{file_id}/reconcile")

    response = client.post(f"/v1/listing-files/{file_id}/verdict")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "NOT_READY"
    assert len(body["blocking_pending"]) == 10
    assert body["advisory"] == ["prelim_title_report"]
    assert "missing:" in body["reason"]
    assert "seller_advisory" in body["reason"]


def test_verdict_is_conditionally_ready_once_everything_is_received(client):
    file_id = _create_listing_file(client)["id"]
    client.post(f"/v1/listing-files/{file_id}/generate-requirements")
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    reconcile = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert reconcile["evidence_created"] == 12
    assert reconcile["unmet_requirement_keys"] == []

    response = client.post(f"/v1/listing-files/{file_id}/verdict")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["verdict"] == "CONDITIONALLY_READY"
    assert "pending verification" in body["reason"]
    assert set(body["pending_verification"]) | set(body["advisory"]) == {
        "listing_agreement", "seller_advisory", "agency_disclosure", "ca_tds", "ca_spq",
        "agent_visual_inspection", "ca_nhd", "wcmd_advisory", "lead_disclosure",
        "hoa_package", "solar_agreement", "prelim_title_report",
    }
    assert body["blocking_pending"] == []
    assert body["blocking_exceptions"] == []


def test_verdict_unknown_listing_file_is_404(client):
    response = client.post("/v1/listing-files/9999/verdict")

    assert response.status_code == 404


def test_readout_returns_file_requirements_and_verdict(client):
    file_id = _create_listing_file(client)["id"]
    client.post(f"/v1/listing-files/{file_id}/generate-requirements")
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    client.post(f"/v1/listing-files/{file_id}/verdict")

    response = client.get(f"/v1/listing-files/{file_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == file_id
    assert body["property_address"] == "123 Main St, Pasadena, CA 91101"
    assert body["apn"] == "5842-018-024"
    assert body["seller_name"] == "Jane Seller"
    assert body["property_attributes"] == CA_FACTS
    assert body["verdict"] == "CONDITIONALLY_READY"
    assert "pending verification" in body["reason"]
    assert len(body["requirements"]) == 12
    keys = {requirement["requirement_key"] for requirement in body["requirements"]}
    assert "ca_tds" in keys
    ca_tds = next(r for r in body["requirements"] if r["requirement_key"] == "ca_tds")
    assert ca_tds["status"] == "REQUIRED"
    assert ca_tds["state"] == "RECEIVED"
    assert body["unmatched_document_ids"] == []


def test_readout_before_anything_reports_an_empty_file(client):
    file_id = _create_listing_file(client)["id"]

    response = client.get(f"/v1/listing-files/{file_id}")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requirements"] == []
    assert body["verdict"] is None
    assert body["reason"] is None
    assert body["unmatched_document_ids"] == []


def test_readout_reports_unmatched_unknown_documents_for_review(client):
    file_id = _create_listing_file(client)["id"]
    client.post(f"/v1/listing-files/{file_id}/generate-requirements")
    unknown_id = _upload_as(client, file_id, "unknown_document.pdf")
    client.post(f"/v1/documents/{unknown_id}/classify")
    client.post(f"/v1/listing-files/{file_id}/reconcile")

    response = client.get(f"/v1/listing-files/{file_id}")

    assert response.status_code == 200, response.text
    assert response.json()["unmatched_document_ids"] == [unknown_id]


def test_readout_unknown_listing_file_is_404(client):
    response = client.get("/v1/listing-files/9999")

    assert response.status_code == 404


def test_demo_file_end_to_end_reaches_conditionally_ready(client):
    """The n8n demo path: one file, twelve documents, one honest verdict."""
    file_id = _create_listing_file(client)["id"]

    generated = client.post(f"/v1/listing-files/{file_id}/generate-requirements").json()["requirements"]
    assert len(generated) == 12
    assert all(r["state"] == "PENDING" for r in generated)

    for filename in DEMO_COMPLETED:
        document_id = _upload_as(client, file_id, filename)
        classified = client.post(f"/v1/documents/{document_id}/classify").json()
        assert classified["document_type"] != "UNKNOWN"
        extracted = client.post(f"/v1/documents/{document_id}/extract").json()
        assert extracted["extracted_data"], filename

    reconcile = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert reconcile["evidence_created"] == 12
    assert reconcile["requirements_moved_to_received"] == sorted(r["requirement_key"] for r in generated)
    assert reconcile["unmet_requirement_keys"] == []
    assert reconcile["unmatched_document_ids"] == []

    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["verdict"] == "CONDITIONALLY_READY"

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert readout["verdict"] == "CONDITIONALLY_READY"
    assert all(r["state"] == "RECEIVED" for r in readout["requirements"])
    assert readout["unmatched_document_ids"] == []
