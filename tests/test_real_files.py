from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app.config import MAX_LOGICAL_FILE_BYTES
from app.config import get_settings
from app.services.storage import LocalDocumentStorage
from tests.test_api import text_pdf_bytes


FIXTURES = Path(__file__).parent / "fixtures"


def workbook_bytes(extension: str = ".xlsx") -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "Remittance"
    sheet.append(["Remittance Number", "Customer ID"])
    sheet.append(["REM-XLSX-001", "CUST-001"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def multipart_payload(source_email_id: str, documents: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source_email_id": source_email_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "synthetic@example.com",
        "documents": documents,
    }


def test_valid_text_pdf_is_parsed_and_decided(client: TestClient) -> None:
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    metadata = multipart_payload("real-pdf", [{"document_name": "remittance.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("remittance.pdf", content, "application/pdf"))])
    assert response.status_code == 201
    document = response.json()["documents"][0]
    assert document["decision"] == "READY_FOR_PROCESSING"
    assert document["business_reference"] == "REM-MULTI-001"
    assert document["content_hash"] == hashlib.sha256(content).hexdigest()
    assert document["storage_reference"].startswith("local://documents/")
    assert document["evidence"]["format_extraction"]["provider"] == "pypdf"


@pytest.mark.parametrize("extension", [".xlsx", ".xlsm"])
def test_xlsx_and_xlsm_are_parsed(client: TestClient, extension: str) -> None:
    content = (FIXTURES / "valid_remittance.xlsx").read_bytes()
    metadata = multipart_payload(f"real-excel-{extension}", [{"document_name": f"remittance{extension}", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", (f"remittance{extension}", content, metadata["documents"][0]["mime_type"]))])
    assert response.status_code == 201
    document = response.json()["documents"][0]
    assert document["decision"] == "READY_FOR_PROCESSING"
    assert document["evidence"]["format_extraction"]["provider"] == "openpyxl"


def test_corrupt_pdf_is_permanent_failure(client: TestClient) -> None:
    metadata = multipart_payload("corrupt-pdf", [{"document_name": "corrupt.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("corrupt.pdf", b"%PDF-not-valid", "application/pdf"))])
    assert response.status_code == 201
    document = response.json()["documents"][0]
    assert document["decision"] == "FAILED"
    assert document["failure_type"] == "PERMANENT"
    assert document["retryable"] is False


def test_corrupt_xlsx_is_permanent_failure(client: TestClient) -> None:
    metadata = multipart_payload("corrupt-xlsx", [{"document_name": "corrupt.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("corrupt.xlsx", b"PK-not-a-workbook", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"))])
    assert response.status_code == 201
    document = response.json()["documents"][0]
    assert document["decision"] == "FAILED"
    assert document["failure_type"] == "PERMANENT"


def test_empty_file_is_rejected(client: TestClient) -> None:
    metadata = multipart_payload("empty-file", [{"document_name": "empty.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("empty.pdf", b"", "application/pdf"))])
    assert response.status_code == 422


def test_expected_hash_mismatch_is_rejected(client: TestClient) -> None:
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    metadata = multipart_payload("hash-mismatch", [{"document_name": "remittance.pdf", "mime_type": "application/pdf", "expected_content_hash": "0" * 64}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("remittance.pdf", content, "application/pdf"))])
    assert response.status_code == 422


def test_file_at_logical_size_limit_is_accepted(client: TestClient) -> None:
    content = text_pdf_bytes()
    content += b"x" * (MAX_LOGICAL_FILE_BYTES - len(content))
    metadata = multipart_payload("limit-file", [{"document_name": "limit.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("limit.pdf", content, "application/pdf"))])
    assert len(content) == MAX_LOGICAL_FILE_BYTES
    assert response.status_code == 201


def test_file_over_logical_size_limit_is_rejected(client: TestClient) -> None:
    content = b"%PDF-" + (b"x" * (MAX_LOGICAL_FILE_BYTES + 1 - len(b"%PDF-")))
    metadata = multipart_payload("oversized-file", [{"document_name": "large.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("large.pdf", content, "application/pdf"))])
    assert response.status_code == 413


def test_seven_small_documents_are_accepted(client: TestClient) -> None:
    content = text_pdf_bytes()
    documents = [{"document_name": f"remittance-{index}.pdf", "mime_type": "application/pdf"} for index in range(7)]
    metadata = multipart_payload("seven-documents", documents)
    response = client.post(
        "/v1/emails/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[("files", (document["document_name"], content, "application/pdf")) for document in documents],
    )
    assert response.status_code == 201
    assert len(response.json()["documents"]) == 7


def test_mixed_documents_reject_when_one_file_exceeds_limit(client: TestClient) -> None:
    small_content = text_pdf_bytes()
    oversized_content = b"%PDF-" + (b"x" * (MAX_LOGICAL_FILE_BYTES + 1 - len(b"%PDF-")))
    documents = [
        {"document_name": "small-a.pdf", "mime_type": "application/pdf"},
        {"document_name": "small-b.pdf", "mime_type": "application/pdf"},
        {"document_name": "oversized.pdf", "mime_type": "application/pdf"},
    ]
    metadata = multipart_payload("mixed-size-documents", documents)
    response = client.post(
        "/v1/emails/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[
            ("files", ("small-a.pdf", small_content, "application/pdf")),
            ("files", ("small-b.pdf", small_content, "application/pdf")),
            ("files", ("oversized.pdf", oversized_content, "application/pdf")),
        ],
    )
    assert response.status_code == 413


def test_multiple_real_documents_keep_independent_results(client: TestClient) -> None:
    pdf = text_pdf_bytes()
    xlsx = workbook_bytes()
    metadata = multipart_payload("real-multiple", [
        {"document_name": "remittance.pdf", "mime_type": "application/pdf"},
        {"document_name": "remittance.xlsx", "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"},
    ])
    response = client.post(
        "/v1/emails/intake",
        data={"metadata": __import__("json").dumps(metadata)},
        files=[
            ("files", ("remittance.pdf", pdf, "application/pdf")),
            ("files", ("remittance.xlsx", xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    assert response.status_code == 201
    documents = response.json()["documents"]
    assert len(documents) == 2
    assert {document["document_name"] for document in documents} == {"remittance.pdf", "remittance.xlsx"}
    assert all(document["decision"] == "READY_FOR_PROCESSING" for document in documents)


def test_document_can_reprocess_from_durable_storage(client: TestClient) -> None:
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    metadata = multipart_payload("reprocess-real-file", [{"document_name": "remittance.pdf", "mime_type": "application/pdf"}])
    response = client.post("/v1/emails/intake", data={"metadata": __import__("json").dumps(metadata)}, files=[("files", ("remittance.pdf", content, "application/pdf"))])
    document = response.json()["documents"][0]
    original_hash = document["content_hash"]
    original_reference = document["storage_reference"]
    reprocessed = client.post(f"/v1/documents/{document['id']}/reprocess")
    assert reprocessed.status_code == 200
    result = reprocessed.json()
    assert result["decision"] == "READY_FOR_PROCESSING"
    assert result["content_hash"] == original_hash
    assert result["storage_reference"] == original_reference


def test_storage_reuses_physical_path_and_reads_original_bytes(tmp_path) -> None:
    storage = LocalDocumentStorage(str(tmp_path))
    content = b"synthetic content"
    first = storage.store(content)
    second = storage.store(content)
    assert first.storage_reference == second.storage_reference
    assert storage.read(first.storage_reference) == content
    assert len(list(tmp_path.rglob("*"))) == 3
