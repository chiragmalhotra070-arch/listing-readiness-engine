from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app.adapters.document_parser import DocumentParser
from app.config import Settings
from app.db.models import Document, Email, ProcessingRun
from app.db.session import Base
from app.domain.enums import ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.pipeline import DocumentPipeline
from app.services.storage import LocalDocumentStorage
from app.services.text_sanitization import sanitize_persisted_text
from app.services.work_queue import enqueue_document
from app.worker import DocumentWorker

PG_URL = os.environ.get("ENGINE_PG_TEST_DATABASE_URL")

CLEAN_TEXTS = [
    "",
    "remittance text",
    "invoice_number: INV-12345 / customer_id: CUST-001",
    "café naïve — Ω με 🧾",
    "\n\t  padded whitespace\n\n",
    "tab\tseparated\tcells",
]

NUL_TEXTS = [
    "\x00",
    "\x00\x00",
    "a\x00b",
    "\x00leading nul",
    "trailing nul\x00",
    "\x00both\x00ends\x00",
    "line one\x00\nline two\x00\x00line three",
    "héllo\x00wörld\x00🧾\x00",
    "\x00\n\x00",
]

ALL_TEXTS = CLEAN_TEXTS + NUL_TEXTS


def test_none_is_preserved() -> None:
    assert sanitize_persisted_text(None) is None


@pytest.mark.parametrize("value", CLEAN_TEXTS)
def test_clean_text_is_returned_unchanged(value: str) -> None:
    assert sanitize_persisted_text(value) == value


@pytest.mark.parametrize("value", NUL_TEXTS)
def test_nul_bytes_are_removed(value: str) -> None:
    result = sanitize_persisted_text(value)
    assert isinstance(result, str)
    assert "\x00" not in result


@pytest.mark.parametrize("value", ALL_TEXTS)
def test_only_nul_bytes_are_removed(value: str) -> None:
    assert sanitize_persisted_text(value) == value.replace("\x00", "")


@pytest.mark.parametrize("value", ALL_TEXTS)
def test_result_is_identical_byte_for_byte_apart_from_nul_removal(value: str) -> None:
    expected = value.replace("\x00", "")
    assert sanitize_persisted_text(value).encode("utf-8") == expected.encode("utf-8")


@pytest.mark.parametrize("value", ALL_TEXTS)
def test_length_shrinks_by_exactly_the_nul_count(value: str) -> None:
    assert len(sanitize_persisted_text(value)) == len(value) - value.count("\x00")


def test_nul_only_text_becomes_empty_string() -> None:
    assert sanitize_persisted_text("\x00\x00\x00") == ""


def nul_pdf_bytes() -> bytes:
    """A minimal but structurally valid PDF whose text layer contains a NUL."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = b"BT /F1 12 Tf 72 720 Td (REM-NUL-001 line1\x00line2) Tj ET"
    objects[3] = b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_position = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size " + str(len(objects) + 1).encode() + b" /Root 1 0 R >>\nstartxref\n"
    out += str(xref_position).encode() + b"\n%%EOF"
    return bytes(out)


def store_nul_pdf(storage_root: str) -> tuple[str, str]:
    storage = LocalDocumentStorage(storage_root)
    stored = storage.store(nul_pdf_bytes())
    raw = DocumentParser().parse(
        storage.resolve(stored.storage_reference),
        mime_type="application/pdf",
        document_name="nul_remittance.pdf",
    ).extracted_text
    assert "\x00" in raw, "the crafted fixture must exercise the NUL path"
    return stored.storage_reference, sanitize_persisted_text(raw)


def make_settings(tmp_path) -> Settings:
    return Settings(llm_provider="mock", document_storage_root=str(tmp_path / "documents"))


def intake_payload(source: str, storage_reference: str) -> EmailIntakeRequest:
    return EmailIntakeRequest(
        source_email_id=source,
        received_at=datetime.now(timezone.utc),
        sender_email="nul@example.test",
        applicability=True,
        documents=[
            DocumentInput(
                document_name="nul_remittance.pdf",
                mime_type="application/pdf",
                storage_reference=storage_reference,
            )
        ],
    )


def sqlite_factory(path: Path):
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def assert_extracted_text_is_clean(db, document_id: int, expected: str) -> None:
    document = db.get(Document, document_id)
    run = db.scalar(select(ProcessingRun).where(ProcessingRun.document_id == document_id))
    assert document is not None and document.extracted_text is not None
    assert run is not None and run.extracted_text is not None
    assert "\x00" not in document.extracted_text
    assert "\x00" not in run.extracted_text
    assert document.extracted_text == expected
    assert run.extracted_text == expected


def test_pipeline_persists_extracted_text_without_nul(tmp_path) -> None:
    settings = make_settings(tmp_path)
    reference, expected = store_nul_pdf(settings.document_storage_root)
    pipeline = DocumentPipeline(settings)
    factory = sqlite_factory(tmp_path / "pipeline-nul.db")

    with factory() as db:
        response = pipeline.process_email(intake_payload("nul-intake", reference), db)
        document_id = response.documents[0].id

    with factory() as db:
        assert_extracted_text_is_clean(db, document_id, expected)


def test_worker_persists_extracted_text_without_nul(tmp_path) -> None:
    settings = make_settings(tmp_path)
    reference, expected = store_nul_pdf(settings.document_storage_root)
    factory = sqlite_factory(tmp_path / "worker-nul.db")

    with factory() as db:
        email = Email(
            source_email_id=f"nul-worker-{uuid.uuid4().hex}",
            received_at=datetime.now(timezone.utc),
            sender_email="nul@example.test",
            status="RECEIVED",
        )
        db.add(email)
        db.flush()
        document = Document(
            email_id=email.id,
            document_name="nul_remittance.pdf",
            mime_type="application/pdf",
            storage_reference=reference,
            processing_status=ProcessingStatus.PENDING,
            current_stage=ProcessingStage.DOCUMENT_PARSING,
            stage_status=StageStatus.PENDING,
            overall_status=ProcessingStatus.PENDING,
            duplicate=False,
            document_type="UNKNOWN",
        )
        db.add(document)
        db.flush()
        enqueue_document(db, document.id)
        db.commit()
        document_id = document.id

    worker = DocumentWorker(session_factory=factory, settings=settings, worker_id="nul-worker", lease_seconds=30)
    assert worker.run_once() == document_id

    with factory() as db:
        assert_extracted_text_is_clean(db, document_id, expected)


def _delete_created_rows(engine, *, document_id: int, email_id: int) -> None:
    statements = [
        "DELETE FROM extracted_fields WHERE document_id = :document_id",
        "DELETE FROM processing_attempts WHERE document_id = :document_id",
        "DELETE FROM audit_events WHERE document_id = :document_id OR email_id = :email_id",
        "DELETE FROM document_work_items WHERE document_id = :document_id",
        "DELETE FROM side_effect_operations WHERE document_id = :document_id",
        "DELETE FROM business_identity_ownership WHERE owner_document_id = :document_id",
        "DELETE FROM processing_runs WHERE document_id = :document_id",
        "DELETE FROM documents WHERE id = :document_id",
        "DELETE FROM emails WHERE id = :email_id",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement), {"document_id": document_id, "email_id": email_id})


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_pipeline_persists_extracted_text_without_nul(tmp_path) -> None:
    settings = make_settings(tmp_path)
    reference, expected = store_nul_pdf(settings.document_storage_root)

    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    source = f"nul-pg-{uuid.uuid4().hex}"
    document_id = None
    email_id = None
    try:
        pipeline = DocumentPipeline(settings)
        with factory() as db:
            response = pipeline.process_email(intake_payload(source, reference), db)
            document_id = response.documents[0].id
            email_id = response.email_id

        with factory() as db:
            assert_extracted_text_is_clean(db, document_id, expected)
    finally:
        if document_id is not None and email_id is not None:
            _delete_created_rows(engine, document_id=document_id, email_id=email_id)
        engine.dispose()
