from __future__ import annotations

import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.llm import DEMO_FIXTURE_MARKER, LLMRequest, MockLLMProvider  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db.models import Base, BusinessIdentityOwnership, Customer, Document, Email  # noqa: E402
from app.db.session import get_db  # noqa: E402
from app.domain.enums import DocumentType  # noqa: E402
from app.main import app  # noqa: E402
from scripts.seed_demo_data import apply_seed  # noqa: E402

MARKER_LINE = f"{DEMO_FIXTURE_MARKER} - NOT A REAL FINANCIAL RECORD"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MIME = "application/pdf"

# Exact canned payloads the demo profiles must emit, independent of the
# implementation table in app/adapters/llm.py (DEMO_SEED_SPECIFICATION.md §7.3).
EXPECTED_PAYLOADS: dict[str, dict] = {
    "-invoice.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-2026-1042"},
        "ids": ["NSM-00418"],
        "names": ["Northstar Manufacturing LLC"],
    },
    "credit note.pdf": {
        "document_type": DocumentType.CREDIT_NOTE,
        "fields": {"note_number": "CM-2026-1042", "reference_invoice": "INV-2026-1042"},
        "ids": ["NSM-00418"],
        "names": [],
    },
    "remittance.pdf": {
        "document_type": DocumentType.REMITTANCE,
        "fields": {"remittance_number": "NSTM-091826-01"},
        "ids": ["NSM-00418"],
        "names": ["Northstar Manufacturing LLC"],
    },
    "bank-statement.pdf": {
        "document_type": DocumentType.BANK_STATEMENT,
        "fields": {"statement_start_date": "2026-09-01", "statement_end_date": "2026-09-18"},
        "ids": [],
        "names": ["Asterline Industrial Supply, Inc."],
    },
    "payment-register.xlsx": {
        "document_type": DocumentType.NOT_APPLICABLE,
        "fields": {},
        "ids": [],
        "names": [],
    },
    "invoice-eow-3006.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-EOW-3006"},
        "ids": ["CN0044"],
        "names": ["Awthentikz"],
    },
    "invoice-eow-3007-new-customer.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-EOW-3007"},
        "ids": [],
        "names": ["Harborline Components Ltd."],
    },
    "invoice-eow-3008-unusual-labels.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-EOW-3008"},
        "ids": ["LG001"],
        "names": ["La Galerie"],
    },
    "invoice-eow-3009-missing-invoice-number.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {},
        "ids": ["CUST-001"],
        "names": ["Fixture Customer"],
    },
    "invoice-eow-3010-name-only-duplicate.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-2026-1042"},
        "ids": [],
        "names": ["La Galerie"],
    },
    "invoice-eow-3011-conflicting-customer.pdf": {
        "document_type": DocumentType.INVOICE,
        "fields": {"invoice_number": "INV-EOW-3011"},
        "ids": ["CN0044"],
        "names": ["La Galerie"],
    },
    "remittance-eow-3012-duplicate.pdf": {
        "document_type": DocumentType.REMITTANCE,
        "fields": {"remittance_number": "REM-2026-009"},
        "ids": ["CUST-001"],
        "names": ["Fixture Customer"],
    },
    "remittance-eow-3013-multiple-customers.pdf": {
        "document_type": DocumentType.REMITTANCE,
        "fields": {"remittance_number": "REM-EOW-MULTI-3013"},
        "ids": ["VO001", "GC001"],
        "names": ["Vision Operations", "GE Capital"],
    },
    "purchase-order-eow-3014-not-applicable.pdf": {
        "document_type": DocumentType.NOT_APPLICABLE,
        "fields": {},
        "ids": [],
        "names": ["La Galerie"],
    },
    "invoice-eow-3015-difficult-scan.pdf": {
        "document_type": DocumentType.UNKNOWN,
        "fields": {},
        "ids": [],
        "names": [],
    },
}

# DEMO_SEED_SPECIFICATION.md §3: fixture -> first-run decision from the seed.
DEMO_MATRIX: list[tuple[str, str, str]] = [
    ("-invoice.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("credit note.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("remittance.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("bank-statement.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("payment-register.xlsx", XLSX_MIME, "NOT_APPLICABLE"),
    ("invoice-eow-3006.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("invoice-eow-3007-new-customer.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("invoice-eow-3008-unusual-labels.pdf", PDF_MIME, "READY_FOR_PROCESSING"),
    ("invoice-eow-3009-missing-invoice-number.pdf", PDF_MIME, "NEEDS_REVIEW"),
    ("invoice-eow-3010-name-only-duplicate.pdf", PDF_MIME, "DUPLICATE"),
    ("invoice-eow-3011-conflicting-customer.pdf", PDF_MIME, "NEEDS_REVIEW"),
    ("remittance-eow-3012-duplicate.pdf", PDF_MIME, "DUPLICATE"),
    ("remittance-eow-3013-multiple-customers.pdf", PDF_MIME, "NEEDS_REVIEW"),
    ("purchase-order-eow-3014-not-applicable.pdf", PDF_MIME, "NOT_APPLICABLE"),
    ("invoice-eow-3015-difficult-scan.pdf", PDF_MIME, "NEEDS_REVIEW"),
]


def request_for(name: str, text: str = "", data: dict | None = None, metadata: dict | None = None) -> LLMRequest:
    return LLMRequest(
        document_name=name,
        mime_type=PDF_MIME,
        extracted_text=text,
        extracted_data=data or {},
        metadata=metadata or {},
    )


@pytest.mark.parametrize("filename", sorted(EXPECTED_PAYLOADS))
def test_demo_profile_payloads_are_exact(filename):
    provider = MockLLMProvider()
    expected = EXPECTED_PAYLOADS[filename]
    text = "" if filename.endswith(".xlsx") else MARKER_LINE
    result = provider.analyze(request_for(filename, text))
    assert result.document_type == expected["document_type"], filename
    assert result.extracted_fields == expected["fields"], filename
    candidates = result.customer_candidates
    assert candidates.customer_id_candidates == expected["ids"], filename
    assert candidates.customer_name_candidates == expected["names"], filename
    assert result.evidence.get("fixture_profile") == filename
    assert result.provider == "mock"


def test_generic_path_is_used_when_the_marker_is_absent():
    provider = MockLLMProvider()
    synthetic = {"remittance_number": "REM-MULTI-001", "customer_id": "CUST-001"}
    result = provider.analyze(request_for("remittance.pdf", "Remittance Number: REM-MULTI-001", synthetic))
    assert result.document_type == DocumentType.REMITTANCE
    assert result.extracted_fields == {"remittance_number": "REM-MULTI-001"}
    assert result.customer_candidates.customer_id_candidates == ["CUST-001"]
    assert "fixture_profile" not in result.evidence


def test_payment_register_profile_does_not_require_the_marker():
    provider = MockLLMProvider()
    result = provider.analyze(request_for("payment-register.xlsx", "no marker in these cells"))
    assert result.document_type == DocumentType.NOT_APPLICABLE


def test_explicit_mock_profile_takes_precedence_over_the_demo_profile():
    provider = MockLLMProvider()
    result = provider.analyze(request_for("-invoice.pdf", MARKER_LINE, metadata={"mock_profile": "valid_invoice"}))
    assert result.extracted_fields["invoice_number"] == "INV-LLM-001"


def test_unlisted_filename_with_marker_falls_through_to_the_generic_path():
    provider = MockLLMProvider()
    result = provider.analyze(request_for("other-document.pdf", MARKER_LINE))
    assert result.document_type == DocumentType.UNKNOWN
    assert "fixture_profile" not in result.evidence


def test_marker_bearing_text_under_another_name_stays_generic():
    provider = MockLLMProvider()
    result = provider.analyze(request_for("my-invoice-scan.pdf", MARKER_LINE, {"invoice_number": "INV-GREG-1"}))
    assert result.document_type == DocumentType.INVOICE
    assert result.extracted_fields == {"invoice_number": "INV-GREG-1"}
    assert "fixture_profile" not in result.evidence


def pdf_bytes(lines: list[str]) -> bytes:
    def escape(value: str) -> str:
        return value.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    operations = "BT /F1 12 Tf 20 250 Td " + " ".join(f"({escape(line)}) Tj 0 -14 Td" for line in lines) + "ET"
    stream = operations.encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 300] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, obj in enumerate(objects, start=1):
        offsets.append(len(output))
        output.extend(f"{index} 0 obj\n".encode("ascii") + obj + b"\nendobj\n")
    xref = len(output)
    output.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
    output.extend(b"".join(f"{offset:010d} 00000 n \n".encode("ascii") for offset in offsets[1:]))
    output.extend(f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return bytes(output)


def xlsx_bytes() -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = "Payment Register"
    worksheet.append(["Payment Date", "Payer", "Customer ID", "Payment Reference"])
    worksheet.append(["2026-09-02", "Redwood Packaging Co.", "RPK-00217", "PAY-2026-0001"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    return output.getvalue()


def fixture_content(filename: str) -> bytes:
    if filename.endswith(".xlsx"):
        return xlsx_bytes()
    return pdf_bytes([MARKER_LINE, filename])


def post_fixture(client: TestClient, filename: str, mime: str, source_email_id: str):
    # The API derives source_attachment_id as "<source_email_id>:<index>", and
    # that derived value must match SOURCE_ATTACHMENT_ID_PATTERN - so the
    # source id itself has to be free of spaces (fixture names like
    # "credit note.pdf" would otherwise fail re-validation with a 422).
    safe_source_id = source_email_id.replace(" ", "-")
    metadata = {
        "source_email_id": safe_source_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "demo@example.com",
        "documents": [{"document_name": filename, "mime_type": mime}],
    }
    return client.post(
        "/v1/emails/intake",
        data={"metadata": json.dumps(metadata)},
        files=[("files", (filename, fixture_content(filename), mime))],
    )


@pytest.fixture
def seeded_client(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "documents"))
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("LLM_BASE_URL", "")
    monkeypatch.setenv("LLM_MODEL", "mock-v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    get_settings.cache_clear()
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    with session_factory() as db:
        report = apply_seed(db)
        db.commit()
    assert sum(report["created"].values()) == 5

    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"})
    client.seed_session_factory = session_factory
    yield client
    app.dependency_overrides.clear()
    get_settings.cache_clear()


@pytest.mark.parametrize("filename,mime,expected_decision", DEMO_MATRIX)
def test_first_run_decision_matches_the_acceptance_matrix(seeded_client, filename, mime, expected_decision):
    response = post_fixture(seeded_client, filename, mime, source_email_id=f"demo-{filename}")
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    assert document["decision"] == expected_decision, document.get("decision_reason")
    if expected_decision == "DUPLICATE":
        assert document["duplicate"] is True


def test_full_matrix_run_leaves_the_seed_invariants_intact(seeded_client):
    for index, (filename, mime, _) in enumerate(DEMO_MATRIX):
        response = post_fixture(seeded_client, filename, mime, source_email_id=f"state-{index}-{filename}")
        assert response.status_code == 201, response.text

    with seeded_client.seed_session_factory() as db:
        customers = len(list(db.scalars(select(Customer))))
        emails = len(list(db.scalars(select(Email))))
        documents = len(list(db.scalars(select(Document))))
        ownership = len(list(db.scalars(select(BusinessIdentityOwnership))))

    assert customers == 5, "the matrix run must never insert customers"
    assert emails == 1 + len(DEMO_MATRIX)
    assert documents == 2 + len(DEMO_MATRIX)
    # seeded 2 + 3006 and 3008 claim ownership on their first run
    assert ownership == 4
