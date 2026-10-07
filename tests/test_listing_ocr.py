"""Listing extract path: text-insufficient PDFs route through the OCR failover.

``ensure_document_text`` parses with pypdf first; when a PDF has no
extractable text (scanned / image-only), the listing classify and extract
endpoints route through the chassis ``extract_with_failover``
(pypdf -> Tesseract) and record the winning provider in the document's
evidence and audit trail -- the same path the async pipeline already took.

The host has no tesseract binary, so these tests swap the OCR factory for
an adapter with an injected provider; the container installs
``tesseract-ocr`` and ``poppler-utils`` (Dockerfile) for the live proof.
"""

from __future__ import annotations

from app.adapters.ocr import ExtractionError, ExtractionResult, OCRExtractionAdapter, OCRRequest
from app.api import routes as routes_module
from tests.test_listing_api import DEMO_ROOT, _create_listing_file, _upload
from tests.test_persistence_boundary_sanitization import blank_pdf_bytes

#: What a real scan of the demo TDS would yield: enough marker text for the
#: mock's fixture gate, so the LLM stage demonstrably consumes OCR output.
OCR_TEXT = "FICTIONAL TRAINING DATA\nTransfer Disclosure Statement apn: 5842-018-024 seller: Jane Seller"


class FakeTesseract:
    """Injected Tesseract stand-in: fixed OCR text, records each request."""

    name = "tesseract"

    def __init__(self, text: str = OCR_TEXT, error: ExtractionError | None = None) -> None:
        self.text = text
        self.error = error
        self.requests: list[OCRRequest] = []

    def extract(self, request: OCRRequest) -> ExtractionResult:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return ExtractionResult(self.text, {}, 0.97, provider=self.name, pages_processed=1, provider_metadata={"language": "eng"})


def install_fake_ocr(monkeypatch, provider: FakeTesseract) -> FakeTesseract:
    monkeypatch.setattr(
        routes_module,
        "build_ocr_extractor",
        lambda settings: OCRExtractionAdapter(provider_order=["TESSERACT"], providers={"TESSERACT": provider}),
    )
    return provider


def _upload_bytes(client, file_id: int, filename: str, payload: bytes) -> int:
    response = client.post(f"/v1/listing-files/{file_id}/documents", files={"file": (filename, payload, "application/pdf")})
    assert response.status_code == 201, response.text
    return response.json()["document_id"]


def test_scanned_pdf_routes_through_ocr_and_records_provider(client, monkeypatch):
    provider = install_fake_ocr(monkeypatch, FakeTesseract())
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_bytes(client, file_id, "demo-ca-tds.pdf", blank_pdf_bytes())

    classify = client.post(f"/v1/documents/{document_id}/classify")
    assert classify.status_code == 200, classify.text
    assert classify.json()["document_type"] == "CA_TDS"

    extract = client.post(f"/v1/documents/{document_id}/extract")
    assert extract.status_code == 200, extract.text
    body = extract.json()
    assert body["document_type"] == "CA_TDS"
    assert body["extracted_data"]["apn"] == "5842-018-024"
    assert body["normalized_data"]["apn"] == "5842018024"

    result = client.get(f"/v1/documents/{document_id}").json()
    evidence = result["evidence"]
    assert evidence["format_extraction"]["provider"] == "pypdf"
    assert evidence["format_extraction"]["ocr_needed"] is True
    assert evidence["extraction"]["provider"] == "tesseract"
    assert evidence["extraction"]["quality_score"] == 0.97

    events = client.get(f"/v1/documents/{document_id}/audit").json()
    ocr_events = [
        event for event in events
        if event["event_type"] == "STAGE_SUCCEEDED" and (event["details"] or {}).get("provider") == "tesseract"
    ]
    assert ocr_events, events
    assert ocr_events[0]["message"] == "OCR fallback completed"
    assert ocr_events[0]["details"]["stage"] == "OCR"

    # The failover saw the stored file with its real page count -- the
    # non-mock provider path, not the mock OCR profile path.
    assert provider.requests
    request = provider.requests[0]
    assert request.storage_reference
    assert request.page_count == 1
    assert request.mime_type == "application/pdf"


def test_extract_before_classify_also_routes_through_ocr(client, monkeypatch):
    install_fake_ocr(monkeypatch, FakeTesseract())
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_bytes(client, file_id, "demo-ca-tds.pdf", blank_pdf_bytes())

    extract = client.post(f"/v1/documents/{document_id}/extract")
    assert extract.status_code == 200, extract.text
    body = extract.json()
    # Extraction never writes document_type (that is classify's job), but the
    # OCR text must have driven the field payload.
    assert body["extracted_data"]["apn"] == "5842-018-024"
    assert body["normalized_data"]["apn"] == "5842018024"

    evidence = client.get(f"/v1/documents/{document_id}").json()["evidence"]
    assert evidence["extraction"]["provider"] == "tesseract"


def test_text_sufficient_pdf_never_invokes_ocr(client, monkeypatch):
    def forbidden(settings):
        raise AssertionError("OCR failover must not run when pypdf found text")

    monkeypatch.setattr(routes_module, "build_ocr_extractor", forbidden)
    file_id = _create_listing_file(client)["id"]
    upload = _upload(client, file_id, filename="demo-ca-tds.pdf", content=(DEMO_ROOT / "completed" / "demo-ca-tds.pdf").read_bytes())
    assert upload.status_code == 201, upload.text
    document_id = upload.json()["document_id"]

    classify = client.post(f"/v1/documents/{document_id}/classify")
    assert classify.status_code == 200, classify.text
    assert classify.json()["document_type"] == "CA_TDS"

    evidence = client.get(f"/v1/documents/{document_id}").json()["evidence"]
    assert not (evidence or {}).get("format_extraction")


def test_ocr_total_failure_keeps_the_flow_alive_with_provider_errors(client, monkeypatch):
    provider = FakeTesseract(error=ExtractionError("tesseract exploded", code="PROVIDER_ERROR", retryable=True))
    install_fake_ocr(monkeypatch, provider)
    file_id = _create_listing_file(client)["id"]
    document_id = _upload_bytes(client, file_id, "demo-ca-tds.pdf", blank_pdf_bytes())

    classify = client.post(f"/v1/documents/{document_id}/classify")
    assert classify.status_code == 200, classify.text
    # No OCR text means no marker, so the mock honestly reports UNKNOWN.
    assert classify.json()["document_type"] == "UNKNOWN"

    evidence = client.get(f"/v1/documents/{document_id}").json()["evidence"]
    assert evidence["format_extraction"]["ocr_needed"] is True
    assert evidence["format_extraction"]["provider_errors"] == [
        {"provider": "TESSERACT", "failure_code": "PROVIDER_ERROR"}
    ]
    assert "extraction" not in evidence
