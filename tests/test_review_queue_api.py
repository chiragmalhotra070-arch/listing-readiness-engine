"""Slice 8: the human review queue -- read the work, act on it, leave audit.

Thin endpoints over the tested services.  The queue is a projection of
current requirement/document state (no ranking, no confidence threshold),
verify/flag mutate one requirement and re-run the file verdict, and
reclassify corrects an ``UNKNOWN`` document so the *next* reconcile pass
can match it.  Generation, reconciliation, and verdict semantics are not
changed here.
"""

from __future__ import annotations

from tests.test_listing_api import (
    DEMO_COMPLETED,
    _create_listing_file,
    _ingest_demo_documents,
    _upload_as,
)


def _generate(client, file_id: int) -> None:
    assert client.post(f"/v1/listing-files/{file_id}/generate-requirements").status_code == 200


def _queue(client, file_id: int) -> dict:
    response = client.get(f"/v1/listing-files/{file_id}/review-queue")
    assert response.status_code == 200, response.text
    return response.json()


def _requirement_id_for(client, file_id: int, requirement_key: str) -> int:
    """The queue exposes ids; every actionable requirement appears in it."""
    for bucket in ("pending_verification", "exceptions"):
        for item in _queue(client, file_id)[bucket]:
            if item["requirement_key"] == requirement_key:
                return item["requirement_id"]
    raise AssertionError(f"{requirement_key} not in review queue")


def test_review_queue_is_empty_on_fresh_file(client):
    file_id = _create_listing_file(client)["id"]

    response = client.get(f"/v1/listing-files/{file_id}/review-queue")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "listing_file_id",
        "exceptions",
        "unknown_documents",
        "unmatched_documents",
        "pending_verification",
        "overdue",
        "processing_failed",
        "customer_needs_review",
    }
    assert body["listing_file_id"] == file_id
    assert body["exceptions"] == []
    assert body["unknown_documents"] == []
    assert body["unmatched_documents"] == []
    assert body["pending_verification"] == []
    assert body["overdue"] == []
    assert body["customer_needs_review"] == []


def test_review_queue_unknown_listing_file_is_404(client):
    response = client.get("/v1/listing-files/9999/review-queue")

    assert response.status_code == 404


def test_review_queue_buckets_unknown_document_and_flagged_requirement(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    tds_doc = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{tds_doc}/classify")
    unknown_doc = _upload_as(client, file_id, "unknown_document.pdf")
    assert client.post(f"/v1/documents/{unknown_doc}/classify").json()["document_type"] == "UNKNOWN"
    client.post(f"/v1/listing-files/{file_id}/reconcile")

    queue = _queue(client, file_id)
    assert queue["unmatched_documents"] == [
        {
            "document_id": unknown_doc,
            "document_name": "unknown_document.pdf",
            "reason": "no_evidence",
        }
    ]
    assert queue["unknown_documents"] == [
        {"document_id": unknown_doc, "document_name": "unknown_document.pdf"}
    ]
    assert [item["requirement_key"] for item in queue["pending_verification"]] == ["ca_tds"]
    assert queue["exceptions"] == []
    pending_item = queue["pending_verification"][0]
    assert set(pending_item) == {
        "requirement_id",
        "requirement_key",
        "requirement_type",
        "status",
        "state",
        "state_reason",
    }
    assert pending_item["state"] == "RECEIVED"

    flagged = client.post(
        f"/v1/requirements/{pending_item['requirement_id']}/flag",
        json={"reason": "seller signature missing"},
    )
    assert flagged.status_code == 200, flagged.text

    queue = _queue(client, file_id)
    assert [item["requirement_key"] for item in queue["exceptions"]] == ["ca_tds"]
    assert queue["exceptions"][0]["state"] == "EXCEPTION"
    assert queue["exceptions"][0]["state_reason"] == "seller signature missing"
    assert queue["pending_verification"] == []
    assert queue["unmatched_documents"] == [
        {
            "document_id": unknown_doc,
            "document_name": "unknown_document.pdf",
            "reason": "no_evidence",
        }
    ]
    assert queue["unknown_documents"] == [
        {"document_id": unknown_doc, "document_name": "unknown_document.pdf"}
    ]


def test_verify_moves_received_to_verified_and_recomputes_verdict(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["verdict"] == "CONDITIONALLY_READY"
    assert len(_queue(client, file_id)["pending_verification"]) == 12

    response = client.post(
        f"/v1/requirements/{_requirement_id_for(client, file_id, 'ca_tds')}/verify",
        json={"note": "checked against source document"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {
        "requirement_id",
        "requirement_key",
        "state",
        "state_reason",
        "verdict",
        "reason",
    }
    assert body["requirement_key"] == "ca_tds"
    assert body["state"] == "VERIFIED"
    assert body["state_reason"] == "checked against source document"
    assert body["verdict"] == "CONDITIONALLY_READY"
    assert "ca_tds" not in body["reason"]

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    ca_tds = next(r for r in readout["requirements"] if r["requirement_key"] == "ca_tds")
    assert ca_tds["state"] == "VERIFIED"
    queue = _queue(client, file_id)
    assert len(queue["pending_verification"]) == 11
    assert "ca_tds" not in [item["requirement_key"] for item in queue["pending_verification"]]


def test_verify_from_exception_clears_the_block(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    req_id = _requirement_id_for(client, file_id, "ca_tds")
    client.post(f"/v1/requirements/{req_id}/flag", json={"reason": "illegible scan"})
    flagged = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert flagged["verdict"] == "NOT_READY"
    assert flagged["blocking_exceptions"] == ["ca_tds"]

    response = client.post(f"/v1/requirements/{req_id}/verify", json={})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "VERIFIED"
    assert body["state_reason"] == "verified by human review"
    assert body["verdict"] == "CONDITIONALLY_READY"
    assert body["reason"].startswith("Conditionally ready — pending verification: ")
    assert "ca_tds" not in body["reason"]


def test_flag_with_reason_blocks_the_verdict(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    client.post(f"/v1/listing-files/{file_id}/verdict")

    response = client.post(
        f"/v1/requirements/{_requirement_id_for(client, file_id, 'ca_tds')}/flag",
        json={"reason": "seller signature missing"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["requirement_key"] == "ca_tds"
    assert body["state"] == "EXCEPTION"
    assert body["state_reason"] == "seller signature missing"
    assert body["verdict"] == "NOT_READY"
    assert "flagged for review: ca_tds" in body["reason"]

    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["blocking_exceptions"] == ["ca_tds"]

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert readout["verdict"] == "NOT_READY"


def test_flag_requires_a_reason(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, ["demo-ca-tds.pdf"])
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    req_id = _requirement_id_for(client, file_id, "ca_tds")

    missing = client.post(f"/v1/requirements/{req_id}/flag", json={})
    empty = client.post(f"/v1/requirements/{req_id}/flag", json={"reason": "   "})

    assert missing.status_code == 422
    assert empty.status_code == 422


def test_reclassify_unknown_document_then_reconcile_matches_it(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    doc_id = _upload_as(client, file_id, "unknown_document.pdf")
    client.post(f"/v1/documents/{doc_id}/classify")
    before = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert before["evidence_created"] == 0
    assert before["unmatched_document_ids"] == [doc_id]
    assert _queue(client, file_id)["unknown_documents"] == [
        {"document_id": doc_id, "document_name": "unknown_document.pdf"}
    ]

    response = client.post(
        f"/v1/documents/{doc_id}/reclassify", json={"document_type": "CA_TDS"}
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"document_id": doc_id, "document_type": "CA_TDS"}

    after = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert after["evidence_created"] == 1
    assert after["requirements_moved_to_received"] == ["ca_tds"]
    assert after["unmatched_document_ids"] == []
    queue = _queue(client, file_id)
    assert queue["unknown_documents"] == []
    assert [item["requirement_key"] for item in queue["pending_verification"]] == ["ca_tds"]


def test_reclassify_already_classified_document_is_409(client):
    file_id = _create_listing_file(client)["id"]
    doc_id = _upload_as(client, file_id, "demo-ca-tds.pdf")
    assert client.post(f"/v1/documents/{doc_id}/classify").json()["document_type"] == "CA_TDS"

    response = client.post(f"/v1/documents/{doc_id}/reclassify", json={"document_type": "CA_NHD"})

    assert response.status_code == 409


def test_reclassify_rejects_invalid_document_type(client):
    file_id = _create_listing_file(client)["id"]
    doc_id = _upload_as(client, file_id, "unknown_document.pdf")
    client.post(f"/v1/documents/{doc_id}/classify")

    response = client.post(f"/v1/documents/{doc_id}/reclassify", json={"document_type": "NOT_A_TYPE"})

    assert response.status_code == 422


def test_reclassify_unknown_document_is_404(client):
    response = client.post("/v1/documents/9999/reclassify", json={"document_type": "CA_TDS"})

    assert response.status_code == 404


def test_requirement_actions_on_unknown_id_are_404(client):
    verify = client.post("/v1/requirements/9999/verify", json={})
    flag = client.post("/v1/requirements/9999/flag", json={"reason": "because"})

    assert verify.status_code == 404
    assert flag.status_code == 404


def test_review_endpoints_require_api_key(client):
    file_id = _create_listing_file(client)["id"]
    wrong_key = {"X-Intake-API-Key": "wrong-key"}

    assert client.get(f"/v1/listing-files/{file_id}/review-queue", headers=wrong_key).status_code == 401
    assert client.post("/v1/requirements/1/verify", json={}, headers=wrong_key).status_code == 401
    assert client.post("/v1/requirements/1/flag", json={"reason": "x"}, headers=wrong_key).status_code == 401
    assert client.post("/v1/documents/1/reclassify", json={"document_type": "CA_TDS"}, headers=wrong_key).status_code == 401


def test_verify_records_an_audit_event(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    tds_doc = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{tds_doc}/classify")
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    req_id = _requirement_id_for(client, file_id, "ca_tds")

    verify = client.post(f"/v1/requirements/{req_id}/verify", json={"note": "signed copy on file"})
    assert verify.status_code == 200, verify.text

    events = client.get(f"/v1/documents/{tds_doc}/audit").json()
    event = next(e for e in events if e["event_type"] == "REQUIREMENT_VERIFIED")
    assert event["status"] == "SUCCESS"
    assert event["message"] == "requirement verified by human review"
    assert event["details"]["requirement_id"] == req_id
    assert event["details"]["requirement_key"] == "ca_tds"
    assert event["details"]["previous_state"] == "RECEIVED"
    assert event["details"]["note"] == "signed copy on file"
    assert event["details"]["actor"] == "intake-api-key"


def test_flag_records_an_audit_event(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    tds_doc = _upload_as(client, file_id, "demo-ca-tds.pdf")
    client.post(f"/v1/documents/{tds_doc}/classify")
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    req_id = _requirement_id_for(client, file_id, "ca_tds")

    flag = client.post(f"/v1/requirements/{req_id}/flag", json={"reason": "seller signature missing"})
    assert flag.status_code == 200, flag.text

    events = client.get(f"/v1/documents/{tds_doc}/audit").json()
    event = next(e for e in events if e["event_type"] == "REQUIREMENT_FLAGGED")
    assert event["status"] == "SUCCESS"
    assert event["message"] == "requirement flagged for review"
    assert event["details"]["requirement_id"] == req_id
    assert event["details"]["previous_state"] == "RECEIVED"
    assert event["details"]["reason"] == "seller signature missing"
    assert event["details"]["actor"] == "intake-api-key"


def test_reclassify_records_an_audit_event(client):
    file_id = _create_listing_file(client)["id"]
    doc_id = _upload_as(client, file_id, "unknown_document.pdf")
    client.post(f"/v1/documents/{doc_id}/classify")

    response = client.post(f"/v1/documents/{doc_id}/reclassify", json={"document_type": "CA_TDS"})
    assert response.status_code == 200, response.text

    events = client.get(f"/v1/documents/{doc_id}/audit").json()
    event = next(e for e in events if e["event_type"] == "DOCUMENT_RECLASSIFIED")
    assert event["document_id"] == doc_id
    assert event["status"] == "SUCCESS"
    assert event["message"] == "document reclassified by human review"
    assert event["details"]["previous_document_type"] == "UNKNOWN"
    assert event["details"]["document_type"] == "CA_TDS"
    assert event["details"]["actor"] == "intake-api-key"


def test_full_loop_verify_all_twelve_reaches_ready(client):
    """First real green light: twelve documents, twelve verifications, READY."""
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["verdict"] == "CONDITIONALLY_READY"

    queue = _queue(client, file_id)
    assert len(queue["pending_verification"]) == 12
    assert queue["exceptions"] == []
    assert queue["unknown_documents"] == []
    assert queue["unmatched_documents"] == []

    responses = [
        client.post(f"/v1/requirements/{item['requirement_id']}/verify", json={"note": "human review"})
        for item in queue["pending_verification"]
    ]
    assert all(r.status_code == 200 for r in responses)
    last = responses[-1].json()
    assert last["verdict"] == "READY"
    assert last["reason"] == "Ready — all 11 required requirements verified."

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert readout["verdict"] == "READY"
    assert all(r["state"] == "VERIFIED" for r in readout["requirements"])
    assert readout["unmatched_document_ids"] == []
    assert _queue(client, file_id)["pending_verification"] == []
