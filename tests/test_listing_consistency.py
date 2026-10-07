"""Slice 9: the consistency dimension -- normalize, resolve, detect exceptions.

Stage 1 canonicalises the shared extracted facts into ``normalized_data``
(exact-after-normalization, no fuzzy matching), Stage 2 quarantines
documents whose APN/address disagree with the listing file's property
identity (``property_mismatch``), and ``POST .../detect-exceptions``
evaluates R1 (cross-document field conflict), R2 (missing signatures) and
R3 (requirement overdue) against current state.  Nothing here changes
requirement generation, reconciliation matching beyond the quarantine
skip, or verdict computation -- R1/R2 raise ``EXCEPTION`` (which already
blocks), R3 only surfaces overdue work.
"""

from __future__ import annotations

from app.config import get_settings
from app.db.models import Document
from app.services.normalization import (
    document_normalized_fields,
    normalize_address,
    normalize_apn,
    normalize_date,
    normalize_extracted_data,
    normalize_person_name,
)
from tests.test_listing_api import (
    DEMO_COMPLETED,
    _create_listing_file,
    _ingest_demo_documents,
    _upload_as,
)


def _generate(client, file_id: int) -> None:
    assert client.post(f"/v1/listing-files/{file_id}/generate-requirements").status_code == 200


def _classify(client, document_id: int) -> None:
    assert client.post(f"/v1/documents/{document_id}/classify").status_code == 200


def _queue(client, file_id: int) -> dict:
    response = client.get(f"/v1/listing-files/{file_id}/review-queue")
    assert response.status_code == 200, response.text
    return response.json()


def _detect(client, file_id: int) -> dict:
    response = client.post(f"/v1/listing-files/{file_id}/detect-exceptions")
    assert response.status_code == 200, response.text
    return response.json()


# ---- Stage 1: value normalization (unit) ----


def test_normalize_address_canonicalizes_case_suffix_punctuation_and_whitespace():
    assert normalize_address("123 Main Street, Pasadena, CA 91101") == "123 main st pasadena ca 91101"
    assert normalize_address("123  MAIN   St. pasadena, ca") == "123 main st pasadena ca"
    assert normalize_address("123 Main St.,  Pasadena, CA") == "123 main st pasadena ca"
    assert normalize_address(None) is None


def test_normalize_apn_strips_separators():
    assert normalize_apn("5842-018-024") == "5842018024"
    assert normalize_apn(" 5842 018 024 ") == "5842018024"
    assert normalize_apn(None) is None


def test_normalize_person_name_lowercases_and_collapses_whitespace():
    assert normalize_person_name("  Jane   Seller! ") == "jane seller"
    assert normalize_person_name("Jane Seller") == normalize_person_name("jane  SELLER.")
    assert normalize_person_name(None) is None


def test_normalize_date_prefers_iso_and_parses_common_formats():
    assert normalize_date("2026-09-16") == "2026-09-16"
    assert normalize_date("09/16/2026") == "2026-09-16"
    assert normalize_date("September 16, 2026") == "2026-09-16"
    assert normalize_date("sometime in 2026") == "sometime in 2026"
    assert normalize_date(None) is None


def test_normalize_extracted_data_maps_only_the_shared_fact_fields():
    normalized = normalize_extracted_data(
        {
            "property_address": "123 Main Street, Pasadena, CA 91101",
            "apn": "5842-018-024",
            "seller_name": "Jane Seller",
            "signatures_present": True,
            "document_date": None,
            "brokerage_name": "Demo Realty",
        }
    )

    assert normalized == {
        "property_address": "123 main st pasadena ca 91101",
        "apn": "5842018024",
        "seller_name": "jane seller",
    }


def test_extract_returns_normalized_data_beside_byte_identical_raw_fields(client):
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_as(client, file_id, "demo-ca-tds.pdf")
    _classify(client, document_id)

    response = client.post(f"/v1/documents/{document_id}/extract")

    assert response.status_code == 200, response.text
    body = response.json()
    # Raw provider fields are untouched (the dashes survive).
    assert body["extracted_data"]["apn"] == "5842-018-024"
    assert body["extracted_data"]["document_date"] is None
    # The canonical view sits beside them.
    assert body["normalized_data"]["apn"] == "5842018024"
    assert body["normalized_data"]["property_address"] == "123 main st pasadena ca 91101"
    assert body["normalized_data"]["seller_name"] == "jane seller"
    assert "signatures_present" not in body["normalized_data"]
    assert "document_date" not in body["normalized_data"]


def test_normalized_fields_falls_back_when_the_column_is_null():
    """Rows extracted before Slice 9 have no column; readers normalize on the fly."""
    legacy = Document(extracted_data={"apn": "5842-018-024", "seller_name": "Jane Seller"})

    assert legacy.normalized_data is None
    assert document_normalized_fields(legacy) == {"apn": "5842018024", "seller_name": "jane seller"}


# ---- Stage 2: property-identity resolution ----


def test_wrong_apn_document_is_quarantined_and_its_requirement_stays_pending(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    wrong_doc = _upload_as(client, file_id, "wrong-apn-ca-tds.pdf")
    _classify(client, wrong_doc)

    reconcile = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert reconcile["evidence_created"] == 0
    assert reconcile["requirements_moved_to_received"] == []
    assert reconcile["unmatched_document_ids"] == [wrong_doc]

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    ca_tds = next(r for r in readout["requirements"] if r["requirement_key"] == "ca_tds")
    assert ca_tds["state"] == "PENDING"
    assert ca_tds["overdue"] is False
    assert readout["unmatched_document_ids"] == [wrong_doc]

    queue = _queue(client, file_id)
    assert queue["unmatched_documents"] == [
        {
            "document_id": wrong_doc,
            "document_name": "wrong-apn-ca-tds.pdf",
            "reason": "property_mismatch",
        }
    ]
    assert queue["unknown_documents"] == []


def test_dashless_apn_variant_matches_after_normalization(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    document_id = _upload_as(client, file_id, "no-dash-apn-ca-tds.pdf")
    _classify(client, document_id)

    reconcile = client.post(f"/v1/listing-files/{file_id}/reconcile").json()
    assert reconcile["evidence_created"] == 1
    assert reconcile["requirements_moved_to_received"] == ["ca_tds"]
    assert reconcile["unmatched_document_ids"] == []

    queue = _queue(client, file_id)
    assert queue["unmatched_documents"] == []
    assert queue["pending_verification"][0]["requirement_key"] == "ca_tds"


# ---- detect-exceptions endpoint surface ----


def test_detect_exceptions_requires_the_api_key(client):
    file_id = _create_listing_file(client)["id"]

    response = client.post(
        f"/v1/listing-files/{file_id}/detect-exceptions",
        headers={"X-Intake-API-Key": "wrong-key"},
    )

    assert response.status_code == 401


def test_detect_exceptions_unknown_listing_file_is_404(client):
    assert client.post("/v1/listing-files/9999/detect-exceptions").status_code == 404


# ---- Rule R1: cross-document field conflict ----


def test_r1_seller_name_conflict_raises_exception_on_evidence_backed_requirements(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    tds_doc = _upload_as(client, file_id, "demo-ca-tds.pdf")
    _classify(client, tds_doc)
    conflict_doc = _upload_as(client, file_id, "conflict-seller-ca-spq.pdf")
    _classify(client, conflict_doc)
    assert client.post(f"/v1/listing-files/{file_id}/reconcile").json()["evidence_created"] == 2

    report = _detect(client, file_id)

    assert set(report) == {
        "listing_file_id",
        "conflicts",
        "missing_signatures",
        "overdue",
        "exceptions_raised",
    }
    assert report["exceptions_raised"] == 2
    assert report["missing_signatures"] == []
    assert report["overdue"] == []
    assert len(report["conflicts"]) == 1
    conflict = report["conflicts"][0]
    assert conflict["field"] == "seller_name"
    assert set(conflict["values"]) == {"jane seller", "john doe"}
    assert set(conflict["document_ids"]) == {tds_doc, conflict_doc}
    assert set(conflict["requirement_keys"]) == {"ca_tds", "ca_spq"}
    assert "seller_name" in conflict["reason"]
    assert "jane seller" in conflict["reason"] and "john doe" in conflict["reason"]

    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["verdict"] == "NOT_READY"
    assert set(verdict["blocking_exceptions"]) == {"ca_tds", "ca_spq"}

    queue = _queue(client, file_id)
    assert {item["requirement_key"] for item in queue["exceptions"]} == {"ca_tds", "ca_spq"}
    assert "seller_name" in queue["exceptions"][0]["state_reason"]

    events = client.get(f"/v1/documents/{tds_doc}/audit").json()
    event = next(e for e in events if e["event_type"] == "EXCEPTION_DETECTED")
    assert event["document_id"] == tds_doc
    assert event["status"] == "SUCCESS"
    assert event["details"]["rule"] == "R1"
    assert event["details"]["previous_state"] == "RECEIVED"
    assert event["details"]["actor"] == "intake-api-key"


def test_r1_does_not_overwrite_an_existing_exception_reason(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    tds_doc = _upload_as(client, file_id, "demo-ca-tds.pdf")
    _classify(client, tds_doc)
    conflict_doc = _upload_as(client, file_id, "conflict-seller-ca-spq.pdf")
    _classify(client, conflict_doc)
    client.post(f"/v1/listing-files/{file_id}/reconcile")
    ca_tds_id = next(
        item["requirement_id"]
        for item in _queue(client, file_id)["pending_verification"]
        if item["requirement_key"] == "ca_tds"
    )
    flagged = client.post(
        f"/v1/requirements/{ca_tds_id}/flag",
        json={"reason": "seller signature missing"},
    )
    assert flagged.status_code == 200

    report = _detect(client, file_id)

    assert report["exceptions_raised"] == 1  # only ca_spq changes state
    queue = _queue(client, file_id)
    reasons = {item["requirement_key"]: item["state_reason"] for item in queue["exceptions"]}
    assert reasons["ca_tds"] == "seller signature missing"
    assert "seller_name" in reasons["ca_spq"]


# ---- Rule R2: missing signatures ----


def test_r2_missing_signatures_raises_exception_on_the_listing_agreement(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    document_id = _upload_as(client, file_id, "unsigned-listing-agreement.pdf")
    _classify(client, document_id)
    assert client.post(f"/v1/listing-files/{file_id}/reconcile").json()["evidence_created"] == 1

    report = _detect(client, file_id)

    assert report["conflicts"] == []
    assert report["overdue"] == []
    assert report["exceptions_raised"] == 1
    assert len(report["missing_signatures"]) == 1
    finding = report["missing_signatures"][0]
    assert finding["requirement_key"] == "listing_agreement"
    assert finding["document_type"] == "LISTING_AGREEMENT"
    assert finding["document_id"] == document_id
    assert "missing signatures" in finding["reason"]

    queue = _queue(client, file_id)
    exception = queue["exceptions"][0]
    assert exception["requirement_key"] == "listing_agreement"
    assert exception["state"] == "EXCEPTION"
    assert "missing signatures" in exception["state_reason"]


def test_r2_skips_documents_whose_signature_flag_is_absent(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    document_id = _upload_as(client, file_id, "demo-hoa-package.pdf")
    _classify(client, document_id)
    client.post(f"/v1/listing-files/{file_id}/reconcile")

    report = _detect(client, file_id)

    assert report["missing_signatures"] == []
    assert report["exceptions_raised"] == 0


# ---- Rule R3: overdue requirements (surfaced, never mutated) ----


def test_r3_overdue_requirements_are_surfaced_without_mutating_state(client, monkeypatch):
    monkeypatch.setenv("REQUIREMENT_OVERDUE_DAYS", "0")
    get_settings.cache_clear()
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)

    verdict_before = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict_before["verdict"] == "NOT_READY"

    report = _detect(client, file_id)

    assert report["conflicts"] == []
    assert report["missing_signatures"] == []
    assert report["exceptions_raised"] == 0
    assert len(report["overdue"]) == 12
    assert all(item["days_pending"] == 0 for item in report["overdue"])

    # The verdict is a pure function of state; detection changed no state.
    assert client.post(f"/v1/listing-files/{file_id}/verdict").json() == verdict_before

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert all(r["state"] == "PENDING" for r in readout["requirements"])
    assert all(r["overdue"] is True for r in readout["requirements"])

    queue = _queue(client, file_id)
    assert len(queue["overdue"]) == 12
    assert all(item["state"] == "PENDING" for item in queue["overdue"])
    assert queue["exceptions"] == []


def test_r3_defaults_to_a_seven_day_threshold(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)

    report = _detect(client, file_id)

    assert report["overdue"] == []
    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert all(r["overdue"] is False for r in readout["requirements"])
    assert _queue(client, file_id)["overdue"] == []


# ---- Regression: the clean demo file stays clean ----


def test_detect_exceptions_reports_no_findings_on_clean_demo_file(client):
    file_id = _create_listing_file(client)["id"]
    _generate(client, file_id)
    _ingest_demo_documents(client, file_id, DEMO_COMPLETED)
    client.post(f"/v1/listing-files/{file_id}/reconcile")

    report = _detect(client, file_id)

    assert report["conflicts"] == []
    assert report["missing_signatures"] == []
    assert report["overdue"] == []
    assert report["exceptions_raised"] == 0

    verdict = client.post(f"/v1/listing-files/{file_id}/verdict").json()
    assert verdict["verdict"] == "CONDITIONALLY_READY"
    assert verdict["blocking_exceptions"] == []

    readout = client.get(f"/v1/listing-files/{file_id}").json()
    assert all(r["overdue"] is False for r in readout["requirements"])
    queue = _queue(client, file_id)
    assert queue["exceptions"] == []
    assert queue["unmatched_documents"] == []
    assert len(queue["pending_verification"]) == 12
