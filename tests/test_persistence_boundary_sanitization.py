"""Regression tests for the PostgreSQL text persistence boundary.

The production worker crashed 948 times with
``DataError: PostgreSQL text fields cannot contain NUL (0x00) bytes``
because untrusted text (provider stderr, PDF payloads, intake fields) reached
``text``/``varchar`` columns.  ``app/services/text_sanitization.py`` enforces
the invariant on ``before_flush`` with two distinct behaviours:

* **free-form prose** (``column.info["free_form_text"]``) is stripped of NUL
  bytes, so a diagnostic such as ``"tesseract stderr: bad\\x00byte"`` still
  persists as ``"tesseract stderr: badbyte"``;
* **structured/identity values** are **rejected** with
  :class:`NulByteRejectedError`, because silently deleting a byte from an
  identifier, hash or reference can collapse two distinct values onto one.

JSONB columns are *not* deferred: a NUL anywhere inside a nested value is
rejected, because PostgreSQL ``jsonb`` input would otherwise fail with SQLSTATE
``22P05`` and abort the transaction.  Plain ``json`` columns
(``processing_attempts.result``, ``side_effect_operations.response``) remain
deferred and unchanged.  PostgreSQL integration tests run only when
``ENGINE_PG_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import String, create_engine, event, func, inspect as sqlalchemy_inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.adapters.crm import CRMActionAdapter, CRMActionResult
from app.adapters.ocr import ExtractionError, OCRExtractionAdapter
from app.config import Settings
from app.db.models import (
    AuditEvent,
    Customer,
    Document,
    DocumentWorkItem,
    Email,
    ExtractedField,
    ProcessingAttempt,
    ProcessingRun,
    SideEffectOperation,
)
from app.db.session import Base
from app.domain.enums import BusinessOutcome, ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import DocumentInput, EmailIntakeRequest, EmailIntakeResponse
from app.services.audit import record_audit
from app.services.pipeline import DocumentPipeline
from app.services.storage import LocalDocumentStorage
from app.services.text_sanitization import (
    FREE_FORM_TEXT_INFO,
    NulByteRejectedError,
    ensure_structured_text,
    sanitize_persisted_object,
    sanitize_persisted_text,
    sanitize_session_text,
    validate_persisted_object,
)
from app.services.work_queue import enqueue_document, fail_work_item, reschedule_work_item

PG_URL = os.environ.get("ENGINE_PG_TEST_DATABASE_URL")
NUL = "\x00"

NUL_SHAPES = [
    "\x00",
    "a\x00b",
    "\x00leading",
    "trailing\x00",
    "unicodé\x00wörld\x00🧾",
]

EXPECTED_FREE_FORM = {
    "audit_events.message",
    "customers.customer_name",
    "document_work_items.last_error_message",
    "documents.decision_reason",
    "documents.extracted_text",
    "documents.last_error",
    "emails.body",
    "emails.sender_company",
    "emails.sender_name",
    "emails.subject",
    "listing_files.readiness_reason",
    "listing_files.seller_name",
    "processing_attempts.failure_message",
    "processing_runs.extracted_text",
    "requirements.state_reason",
}

STRUCTURED_SAMPLES = [
    "emails.source_email_id",
    "documents.content_hash",
    "documents.storage_reference",
    "documents.source_attachment_id",
    "documents.document_name",
    "emails.sender_email",
    "emails.correlation_id",
    "side_effect_operations.idempotency_key",
    "side_effect_operations.external_reference",
    "processing_attempts.idempotency_key",
    "processing_attempts.failure_code",
    "extracted_fields.field_name",
    "customers.customer_id",
    "document_work_items.last_error_code",
    "document_work_items.run_id",
]


# --------------------------------------------------------------------------
# shared scaffolding
# --------------------------------------------------------------------------


def make_settings(tmp_path, **overrides) -> Settings:
    """Intake-only settings: no real file access, no synchronous processing."""
    options = {
        "llm_provider": "mock",
        "document_storage_root": str(tmp_path / "documents"),
        "processing_mode": "async",
    }
    options.update(overrides)
    return Settings(**options)


@pytest.fixture
def db(tmp_path):
    """Yield a factory bound to a private SQLite schema."""
    engine = create_engine(f"sqlite:///{tmp_path / 'boundary.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


@pytest.fixture
def pipeline(tmp_path) -> DocumentPipeline:
    return DocumentPipeline(make_settings(tmp_path))


def build_payload(**overrides) -> EmailIntakeRequest:
    fields: dict = {
        "source_email_id": f"src-{uuid.uuid4().hex}",
        "received_at": datetime.now(timezone.utc),
        "sender_email": "api@acme.test",
        "sender_name": "Ann Lee",
        "sender_company": "Acme Corp",
        "subject": "Remittance advice",
        "body": "remittance body",
        "applicability": True,
        "correlation_id": "corr-1",
        "n8n_execution_id": "exec-1",
        "documents": [
            DocumentInput(
                document_name="remittance.pdf",
                mime_type="application/pdf",
                storage_reference="docs/missing.pdf",
                source_attachment_id="att-1",
                content_hash="a" * 64,
            )
        ],
    }
    fields.update(overrides)
    return EmailIntakeRequest(**fields)


def seed_document(session, **document_overrides) -> Document:
    """Create a bare email + document pair and flush them (values stay clean)."""
    email = Email(
        source_email_id=f"src-{uuid.uuid4().hex}",
        received_at=datetime.now(timezone.utc),
        sender_email="api@acme.test",
        status="RECEIVED",
    )
    session.add(email)
    session.flush()
    document = Document(
        email_id=email.id,
        document_name="remittance.pdf",
        mime_type="application/pdf",
        processing_status=ProcessingStatus.PENDING,
        current_stage=ProcessingStage.DOCUMENT_PARSING,
        stage_status=StageStatus.PENDING,
        overall_status=ProcessingStatus.PENDING,
        duplicate=False,
        document_type="UNKNOWN",
        **document_overrides,
    )
    session.add(document)
    session.flush()
    return document


def assert_text(value, *, label: str) -> str:
    assert isinstance(value, str), f"{label} is not a string: {value!r}"
    assert NUL not in value, f"{label} still contains a NUL byte: {value!r}"
    return value


def blank_pdf_bytes() -> bytes:
    """A valid single-page PDF with no text, forcing the OCR-fallback branch."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R >>",
        b"<< /Length 0 >>\nstream\n\nendstream",
    ]
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


def store_blank_pdf(storage_root: str) -> str:
    storage = LocalDocumentStorage(storage_root)
    return storage.store(blank_pdf_bytes()).storage_reference


def _delete_pg_rows(engine, *, email_ids, document_ids) -> None:
    document_ids = set(document_ids)
    with engine.begin() as connection:
        for email_id in email_ids:
            rows = connection.execute(text("SELECT id FROM documents WHERE email_id = :email_id"), {"email_id": email_id})
            document_ids.update(row[0] for row in rows)

    document_statements = [
        "DELETE FROM extracted_fields WHERE document_id = :id",
        "DELETE FROM processing_attempts WHERE document_id = :id",
        "DELETE FROM audit_events WHERE document_id = :id",
        "DELETE FROM document_work_items WHERE document_id = :id",
        "DELETE FROM side_effect_operations WHERE document_id = :id",
        "DELETE FROM business_identity_ownership WHERE owner_document_id = :id",
        "DELETE FROM processing_runs WHERE document_id = :id",
        "DELETE FROM documents WHERE id = :id",
    ]
    with engine.begin() as connection:
        # Migration 0010 installs a BEFORE DELETE trigger that makes
        # extracted_fields immutable; cleanup has to step around it.  DISABLE
        # TRIGGER USER is a no-op when the schema was built by create_all alone.
        connection.execute(text("ALTER TABLE extracted_fields DISABLE TRIGGER USER"))
        try:
            for document_id in document_ids:
                for statement in document_statements:
                    connection.execute(text(statement), {"id": document_id})
            for email_id in email_ids:
                connection.execute(text("DELETE FROM audit_events WHERE email_id = :id"), {"id": email_id})
                connection.execute(text("DELETE FROM emails WHERE id = :id"), {"id": email_id})
        finally:
            connection.execute(text("ALTER TABLE extracted_fields ENABLE TRIGGER USER"))


@contextmanager
def postgres_boundary():
    """Yield ``(factory, track)``; ``track(email_id=…, document_id=…)`` marks rows for cleanup."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    email_ids: set[int] = set()
    document_ids: set[int] = set()

    def track(*, email_id: int | None = None, document_id: int | None = None) -> None:
        if email_id:
            email_ids.add(email_id)
        if document_id:
            document_ids.add(document_id)

    try:
        yield factory, track
    finally:
        _delete_pg_rows(engine, email_ids=email_ids, document_ids=document_ids)
        engine.dispose()


# --------------------------------------------------------------------------
# classification + hook unit tests
# --------------------------------------------------------------------------


def test_flush_hook_is_registered() -> None:
    assert event.contains(Session, "before_flush", sanitize_session_text)


def test_every_string_column_is_classified() -> None:
    free_form: set[str] = set()
    structured: set[str] = set()
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if not isinstance(column.type, String):
                continue
            label = f"{table.name}.{column.name}"
            (free_form if column.info.get(FREE_FORM_TEXT_INFO) else structured).add(label)

    assert free_form == EXPECTED_FREE_FORM
    assert len(free_form) + len(structured) == 75
    for label in STRUCTURED_SAMPLES:
        assert label in structured, f"{label} must be structured (rejected, not rewritten)"


def test_sanitize_persisted_object_rewrites_free_form_only() -> None:
    email = Email(
        source_email_id=f"a{NUL}b",
        received_at=datetime.now(timezone.utc),
        sender_email=f"x{NUL}y",
        subject=f"{NUL}only{NUL}nul{NUL}",
        body="clean body",
        status="RECEIVED",
    )
    assert sanitize_persisted_object(email) == 1
    assert email.subject == "onlynul"
    assert email.source_email_id == f"a{NUL}b", "structured identifier must not be rewritten"
    assert email.sender_email == f"x{NUL}y", "structured address must not be rewritten"
    assert email.body == "clean body"
    assert email.correlation_id is None


def test_validate_persisted_object_rejects_structured_but_accepts_free_form() -> None:
    free_form_only = Email(
        source_email_id="clean-id",
        received_at=datetime.now(timezone.utc),
        sender_email="api@acme.test",
        subject=f"bad{NUL}subject",
        status="RECEIVED",
    )
    validate_persisted_object(free_form_only)  # free-form NUL is not an error

    structured = Email(
        source_email_id=f"a{NUL}b",
        received_at=datetime.now(timezone.utc),
        sender_email="api@acme.test",
        status="RECEIVED",
    )
    with pytest.raises(NulByteRejectedError) as excinfo:
        validate_persisted_object(structured)
    assert excinfo.value.column_label == "emails.source_email_id"
    assert "emails.source_email_id" in str(excinfo.value)
    assert f"a{NUL}b" not in str(excinfo.value), "the offending value must not be echoed"


def test_sanitize_persisted_object_leaves_clean_values_by_identity() -> None:
    email = Email(
        source_email_id="clean-id",
        received_at=datetime.now(timezone.utc),
        sender_email="api@acme.test",
        status="RECEIVED",
    )
    original = email.source_email_id
    assert sanitize_persisted_object(email) == 0
    assert validate_persisted_object(email) is None
    assert email.source_email_id is original
    assert email.subject is None


def test_jsonb_columns_are_rejected_not_rewritten() -> None:
    """A NUL anywhere inside JSONB is refused; the in-memory value stays intact."""
    extracted_data = {"note": f"bad{NUL}value"}
    evidence = {"warnings": [f"bad{NUL}warning"]}
    document = Document(
        email_id=1,
        document_name="clean.pdf",
        mime_type="application/pdf",
        processing_status=ProcessingStatus.PENDING,
        current_stage=ProcessingStage.DOCUMENT_PARSING,
        stage_status=StageStatus.PENDING,
        overall_status=ProcessingStatus.PENDING,
        duplicate=False,
        document_type="UNKNOWN",
        extracted_data=extracted_data,
        evidence=evidence,
    )
    assert sanitize_persisted_object(document) == 0  # JSONB is never rewritten
    with pytest.raises(NulByteRejectedError) as excinfo:
        validate_persisted_object(document)
    assert excinfo.value.column_label == "documents.extracted_data"
    assert document.extracted_data is extracted_data, "the rejected value must not be mutated"
    assert document.evidence is evidence, "the rejected value must not be mutated"
    assert document.document_name == "clean.pdf"
    assert NUL in document.extracted_data["note"] and NUL in document.evidence["warnings"][0]


def test_jsonb_evidence_column_is_rejected_on_its_own() -> None:
    evidence = {"warnings": [f"bad{NUL}warning"]}
    document = Document(
        email_id=1,
        document_name="clean.pdf",
        mime_type="application/pdf",
        processing_status=ProcessingStatus.PENDING,
        current_stage=ProcessingStage.DOCUMENT_PARSING,
        stage_status=StageStatus.PENDING,
        overall_status=ProcessingStatus.PENDING,
        duplicate=False,
        document_type="UNKNOWN",
        evidence=evidence,
    )
    with pytest.raises(NulByteRejectedError) as excinfo:
        validate_persisted_object(document)
    assert excinfo.value.column_label == "documents.evidence"
    assert document.evidence is evidence
    assert f"bad{NUL}warning" not in str(excinfo.value), "the offending value must not be echoed"


def test_plain_json_columns_remain_deferred() -> None:
    """``processing_attempts.result`` and ``side_effect_operations.response`` stay as they were."""
    now = datetime.now(timezone.utc)
    payload = {"message": f"crm rejected{NUL} payload", "code": "HTTP_400"}
    attempt = ProcessingAttempt(
        document_id=1,
        attempt_number=1,
        stage=ProcessingStage.CRM_ACTION,
        provider="mock-crm",
        started_at=now,
        status=StageStatus.PERMANENT_FAILURE,
        result=payload,
    )
    assert sanitize_persisted_object(attempt) == 0
    assert validate_persisted_object(attempt) is None  # plain JSON is never inspected
    assert attempt.result is payload

    operation = SideEffectOperation(
        document_id=1,
        action_type="CRM_ACTION",
        idempotency_key="document:1:CRM_ACTION",
        status="FAILED",
        response=payload,
    )
    assert sanitize_persisted_object(operation) == 0
    assert validate_persisted_object(operation) is None
    assert operation.response is payload


def test_persistence_helpers_ignore_non_orm_values() -> None:
    for value in (object(), {"a": "b"}, None, 42):
        assert sanitize_persisted_object(value) == 0
        assert validate_persisted_object(value) is None
    assert sqlalchemy_inspect(object(), raiseerr=False) is None


def test_ensure_structured_text_returns_clean_values_unchanged() -> None:
    assert ensure_structured_text(None, "emails.source_email_id") is None
    assert ensure_structured_text("abc", "emails.source_email_id") == "abc"
    clean = "unicodé — 🧾"
    assert ensure_structured_text(clean, "emails.source_email_id") is clean


def test_ensure_structured_text_rejects_nul() -> None:
    with pytest.raises(NulByteRejectedError) as excinfo:
        ensure_structured_text(f"abc{NUL}def", "emails.source_email_id")
    assert excinfo.value.column_label == "emails.source_email_id"


# --------------------------------------------------------------------------
# free-form text is sanitized (real intake path)
# --------------------------------------------------------------------------


def test_intake_free_form_fields_are_sanitized(db, pipeline) -> None:
    payload = build_payload(
        sender_name=f"Ann{NUL}a Lee",
        sender_company=f"{NUL}Acme{NUL}Corp{NUL}",
        subject=f"Rem{NUL}ittance adv{NUL}ice",
        body=f"line one{NUL}\nline two{NUL}{NUL}",
    )
    with db() as session:
        response = pipeline.process_email(payload, session)
        email_id, document_id = response.email_id, response.documents[0].id

    with db() as session:
        email = session.get(Email, email_id)
        document = session.get(Document, document_id)
        assert email.sender_name == "Anna Lee"
        assert email.sender_company == "AcmeCorp"
        assert email.subject == "Remittance advice"
        assert email.body == "line one\nline two"
        for label, value in (
            ("emails.sender_name", email.sender_name),
            ("emails.sender_company", email.sender_company),
            ("emails.subject", email.subject),
            ("emails.body", email.body),
        ):
            assert_text(value, label=label)
        # structured intake fields are stored verbatim (they were NUL-free)
        assert email.source_email_id == payload.source_email_id
        assert document.document_name == "remittance.pdf"
        assert document.content_hash == "a" * 64


@pytest.mark.parametrize("shape", NUL_SHAPES)
def test_free_form_nul_shapes_are_stripped(db, pipeline, shape: str) -> None:
    payload = build_payload(subject=f"{shape} remittance", body=shape)
    with db() as session:
        response = pipeline.process_email(payload, session)
        email_id = response.email_id

    with db() as session:
        email = session.get(Email, email_id)
        assert email.subject == f"{shape} remittance".replace(NUL, "")
        assert email.body == shape.replace(NUL, "")
        assert_text(email.subject, label="emails.subject")
        assert_text(email.body, label="emails.body")


def test_intake_leaves_none_columns_none(db, pipeline) -> None:
    payload = build_payload(
        sender_name=None,
        sender_company=None,
        subject=None,
        body=None,
        correlation_id=None,
        n8n_execution_id=None,
        documents=[DocumentInput(document_name="plain.pdf", mime_type="application/pdf")],
    )
    with db() as session:
        response = pipeline.process_email(payload, session)
        email_id, document_id = response.email_id, response.documents[0].id

    with db() as session:
        email = session.get(Email, email_id)
        document = session.get(Document, document_id)
        assert email.sender_name is None
        assert email.sender_company is None
        assert email.subject is None
        assert email.body is None
        assert email.correlation_id is None
        assert email.n8n_execution_id is None
        assert document.source_attachment_id is None
        assert document.content_hash is None


def test_intake_keeps_clean_values_byte_identical(db, pipeline) -> None:
    payload = build_payload(
        subject="café naïve — Ω με 🧾",
        documents=[DocumentInput(document_name="clean_name.pdf", mime_type="application/pdf")],
    )
    with db() as session:
        response = pipeline.process_email(payload, session)
        email_id, document_id = response.email_id, response.documents[0].id

    with db() as session:
        email = session.get(Email, email_id)
        document = session.get(Document, document_id)
        assert email.subject == payload.subject
        assert email.subject.encode("utf-8") == payload.subject.encode("utf-8")
        assert document.document_name == "clean_name.pdf"


# --------------------------------------------------------------------------
# structured / identity text is rejected, never rewritten
# --------------------------------------------------------------------------


@pytest.mark.parametrize("shape", NUL_SHAPES)
def test_structured_nul_shapes_are_rejected_at_intake(db, pipeline, shape: str) -> None:
    source = f"src-{uuid.uuid4().hex}{shape}"
    with db() as session:
        with pytest.raises(NulByteRejectedError) as excinfo:
            pipeline.process_email(build_payload(source_email_id=source), session)
        session.rollback()
    assert excinfo.value.column_label == "emails.source_email_id"

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Email)) == 0


def test_source_email_id_rejected_before_the_select(db, pipeline) -> None:
    """The idempotency SELECT must never run with a NUL-bearing key."""
    with db() as session:
        with pytest.raises(NulByteRejectedError):
            pipeline.process_email(build_payload(source_email_id=f"src-{uuid.uuid4().hex}{NUL}"), session)
        session.rollback()

    with db() as session:
        existing = seed_document(session)
        assert session.scalar(select(func.count()).select_from(Email)) == 1
        del existing


def test_source_email_id_is_rejected_even_on_a_bare_flush(db) -> None:
    """Defence in depth: the hook catches it if a caller bypasses process_email."""
    with db() as session:
        email = Email(
            source_email_id=f"src-{uuid.uuid4().hex}{NUL}",
            received_at=datetime.now(timezone.utc),
            sender_email="api@acme.test",
            status="RECEIVED",
        )
        session.add(email)
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        assert email.source_email_id.endswith(NUL), "value must not be rewritten"
        session.rollback()

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Email)) == 0
    assert excinfo.value.column_label == "emails.source_email_id"


def test_structured_intake_headers_are_rejected(db, pipeline) -> None:
    """sender_email / correlation_id / n8n_execution_id are identity, not prose."""
    for field in ("sender_email", "correlation_id", "n8n_execution_id"):
        with db() as session:
            with pytest.raises(NulByteRejectedError) as excinfo:
                pipeline.process_email(build_payload(**{field: f"clean{NUL}value"}), session)
            session.rollback()
        assert excinfo.value.column_label == f"emails.{field}"

        with db() as session:
            assert session.scalar(select(func.count()).select_from(Email)) == 0


def test_structured_identifiers_are_rejected_not_rewritten(db) -> None:
    cases = {
        "documents.content_hash": lambda document: setattr(document, "content_hash", f"a{NUL}b"),
        "documents.storage_reference": lambda document: setattr(document, "storage_reference", f"docs/{NUL}path.pdf"),
        "documents.source_attachment_id": lambda document: setattr(document, "source_attachment_id", f"att{NUL}-1"),
        "documents.document_name": lambda document: setattr(document, "document_name", f"inv{NUL}oice.pdf"),
    }
    for label, apply in cases.items():
        with db() as session:
            document = seed_document(session)
            apply(document)
            with pytest.raises(NulByteRejectedError) as excinfo:
                session.flush()
            session.rollback()
        assert excinfo.value.column_label == label


def test_structured_identifier_keeps_its_nul_until_the_flush_is_rejected(db) -> None:
    with db() as session:
        document = seed_document(session)
        document.content_hash = f"digest{NUL}tail"
        with pytest.raises(NulByteRejectedError):
            session.flush()
        assert document.content_hash == f"digest{NUL}tail", "must be rejected, not rewritten"
        session.rollback()

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0


def test_idempotency_key_is_rejected(db) -> None:
    with db() as session:
        document = seed_document(session)
        operation = SideEffectOperation(
            document_id=document.id,
            action_type="CRM_ACTION",
            idempotency_key=f"document:{document.id}{NUL}",
            status="PENDING",
        )
        session.add(operation)
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        assert operation.idempotency_key.endswith(NUL)
        session.rollback()
    assert excinfo.value.column_label == "side_effect_operations.idempotency_key"

    with db() as session:
        assert session.scalar(select(func.count()).select_from(SideEffectOperation)) == 0


def test_clean_source_email_id_still_deduplicates(db, pipeline) -> None:
    source = f"lookup-{uuid.uuid4().hex}"
    with db() as session:
        first = pipeline.process_email(build_payload(source_email_id=source), session)
        assert first.idempotent is False

    with db() as session:
        second = pipeline.process_email(build_payload(source_email_id=source), session)
        assert second.idempotent is True
        assert second.email_id == first.email_id
        assert second.source_email_id == source

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Email)) == 1


# --------------------------------------------------------------------------
# failure paths: diagnostics persist, malformed identifiers do not
# --------------------------------------------------------------------------


class NulFailingOCR:
    """An OCR provider whose diagnostic text carries a NUL byte."""

    name = "NUL_OCR"

    def extract(self, request) -> object:
        raise ExtractionError(f"tesseract stderr: bad{NUL}byte", code="PROVIDER_ERROR", retryable=True)


def test_ocr_failure_diagnostics_persist_without_nul(tmp_path, db) -> None:
    """The free-form diagnostic must be stored (sanitized), and the run must commit."""
    settings = make_settings(tmp_path)
    reference = store_blank_pdf(settings.document_storage_root)
    extractor = OCRExtractionAdapter(
        ["NUL_OCR"],
        mock_provider_order=["NUL_OCR"],
        providers={"NUL_OCR": NulFailingOCR()},
        storage=LocalDocumentStorage(settings.document_storage_root),
    )
    pipeline = DocumentPipeline(settings, extractor=extractor)

    with db() as session:
        response = pipeline.process_email(
            build_payload(documents=[DocumentInput(document_name="blank.pdf", mime_type="application/pdf", storage_reference=reference)]),
            session,
        )
        document_id = response.documents[0].id

    with db() as session:
        document = session.get(Document, document_id)
        pipeline.execute_run(document, session, trigger="INTAKE", profile=None, email_applicable=True, worker_id="nul-ocr")
        session.commit()

    with db() as session:
        attempts = list(session.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id)))
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id)))
        document = session.get(Document, document_id)
        item = session.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id))

        failure_messages = [attempt.failure_message for attempt in attempts if attempt.failure_message]
        assert "tesseract stderr: badbyte" in failure_messages
        for message in failure_messages:
            assert_text(message, label="processing_attempts.failure_message")
        # the structured provider identifier stayed intact
        assert {attempt.provider for attempt in attempts} >= {"NUL_OCR"}

        assert "tesseract stderr: badbyte" in [audit.message for audit in audits]
        for audit in audits:
            assert_text(audit.message, label="audit_events.message")

        assert document.last_error == "all OCR providers failed"
        assert document.decision_reason == "all OCR providers failed"
        assert_text(document.last_error, label="documents.last_error")

        # the worker hand-off (app/worker.py:66-67) copies those columns over
        assert document.processing_status == ProcessingStatus.PENDING_RETRY
        reschedule_work_item(
            item,
            available_at=document.next_attempt_at or datetime.now(timezone.utc),
            code=document.last_error,
            message=document.decision_reason,
        )
        session.commit()

    with db() as session:
        item = session.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id))
        assert item.last_error_message == "all OCR providers failed"
        assert_text(item.last_error_message, label="document_work_items.last_error_message")


class NulFailingCRM(CRMActionAdapter):
    def process(self, *, document_id: int, mock_profile: str | None = None, attempt_number: int = 1, idempotency_key: str) -> CRMActionResult:
        return CRMActionResult(False, f"crm rejected{NUL} payload", "HTTP_400")


def test_crm_failure_free_form_fields_persist(db, tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path), crm=NulFailingCRM())
    with db() as session:
        document = seed_document(session, decision=BusinessOutcome.FAILED)
        document_id = document.id
        session.commit()

    with db() as session:
        pipeline.process_document_action(document_id, session)
        document = session.get(Document, document_id)
        operation = session.scalar(select(SideEffectOperation).where(SideEffectOperation.document_id == document_id))
        attempts = list(session.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id)))
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id)))

        assert document.decision_reason == "crm rejected payload"
        assert document.last_error == "crm rejected payload"
        assert_text(document.decision_reason, label="documents.decision_reason")

        failure_messages = [attempt.failure_message for attempt in attempts if attempt.failure_message]
        assert "crm rejected payload" in failure_messages
        for message in failure_messages:
            assert_text(message, label="processing_attempts.failure_message")
        for audit in audits:
            assert_text(audit.message, label="audit_events.message")
        # JSON columns keep their bytes: recursive JSON sanitization is deferred.
        assert operation.response == {"message": f"crm rejected{NUL} payload", "code": "HTTP_400"}


class StructuredCRM(CRMActionAdapter):
    def process(self, *, document_id: int, mock_profile: str | None = None, attempt_number: int = 1, idempotency_key: str) -> CRMActionResult:
        return CRMActionResult(True, "crm action completed", external_reference=f"CRM{NUL}-1")


def test_adapter_supplied_structured_identifier_is_rejected(db, tmp_path) -> None:
    """A malformed structured identifier from an adapter must fail, not be rewritten."""
    pipeline = DocumentPipeline(make_settings(tmp_path), crm=StructuredCRM())
    with db() as session:
        document = seed_document(session, decision=BusinessOutcome.READY_FOR_PROCESSING)
        document_id = document.id
        session.commit()

    with db() as session:
        with pytest.raises(NulByteRejectedError) as excinfo:
            pipeline.process_document_action(document_id, session)
        session.rollback()
    assert excinfo.value.column_label == "side_effect_operations.external_reference"

    with db() as session:
        assert session.scalar(select(func.count()).select_from(SideEffectOperation)) == 0
        assert session.get(Document, document_id).decision_reason != "crm action completed"


# --------------------------------------------------------------------------
# work queue, extracted fields, customers
# --------------------------------------------------------------------------


def test_work_item_error_message_is_sanitized_but_code_is_rejected(db) -> None:
    now = datetime.now(timezone.utc)
    with db() as session:
        document = seed_document(session)
        item = enqueue_document(session, document.id)
        item_id = item.id
        session.commit()

    with db() as session:
        item = session.get(DocumentWorkItem, item_id)
        reschedule_work_item(item, available_at=now, code="NETWORK_TIMEOUT", message=f"message{NUL}y")
        fail_work_item(item, code="HTTP_400", message=f"fail{NUL}msg")
        session.commit()

    with db() as session:
        item = session.get(DocumentWorkItem, item_id)
        assert item.last_error_message == "failmsg"
        assert item.last_error_code == "HTTP_400"
        assert_text(item.last_error_message, label="document_work_items.last_error_message")

    with db() as session:
        item = session.get(DocumentWorkItem, item_id)
        item.last_error_code = f"CODE{NUL}1"
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        assert item.last_error_code == f"CODE{NUL}1", "must be rejected, not rewritten"
        session.rollback()
    assert excinfo.value.column_label == "document_work_items.last_error_code"


def test_extracted_field_name_is_rejected_and_customer_name_is_sanitized(db) -> None:
    now = datetime.now(timezone.utc)
    with db() as session:
        document = seed_document(session)
        run = ProcessingRun(document_id=document.id, trigger="INTAKE", started_at=now, completed_at=now, outcome="UNKNOWN")
        session.add(run)
        session.flush()
        attempt = ProcessingAttempt(
            document_id=document.id,
            run_id=run.id,
            attempt_number=1,
            stage=ProcessingStage.OCR,
            provider="test",
            started_at=now,
            completed_at=now,
            status=StageStatus.SUCCESS,
        )
        session.add(attempt)
        session.flush()
        session.add(
            ExtractedField(
                attempt_id=attempt.id,
                document_id=document.id,
                run_id=run.id,
                field_name=f"remittance{NUL}number",
                field_value={"raw": "REM-001"},
                value_type="string",
                source="OCR",
            )
        )
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        session.rollback()
    assert excinfo.value.column_label == "extracted_fields.field_name"

    with db() as session:
        document = seed_document(session)
        session.add(Customer(customer_id="CUST-999", customer_name=f"Acme{NUL}Holdings"))
        session.add(Customer(customer_id=f"CUST{NUL}-998", customer_name="Rejected"))
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        session.rollback()
    assert excinfo.value.column_label == "customers.customer_id"

    with db() as session:
        session.add(Customer(customer_id="CUST-999", customer_name=f"Acme{NUL}Holdings"))
        session.commit()
        customer = session.scalar(select(Customer).where(Customer.customer_id == "CUST-999"))
        assert customer.customer_name == "AcmeHoldings"
        assert_text(customer.customer_name, label="customers.customer_name")


def test_audit_message_is_sanitized(db) -> None:
    with db() as session:
        document = seed_document(session)
        record_audit(
            session,
            document_id=document.id,
            event_type="STAGE_FAILED",
            status=StageStatus.RETRYABLE_FAILURE,
            message=f"provider said: bad{NUL}thing",
            details={"stage": ProcessingStage.OCR},
        )
        session.commit()
        document_id = document.id

    with db() as session:
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id)))
        messages = [audit.message for audit in audits]
        assert "provider said: badthing" in messages
        for message in messages:
            assert_text(message, label="audit_events.message")
        for audit in audits:
            if audit.correlation_id is not None:
                assert_text(audit.correlation_id, label="audit_events.correlation_id")


# --------------------------------------------------------------------------
# API surface
# --------------------------------------------------------------------------


def test_api_rejects_structured_nul_with_422(client) -> None:
    payload = build_payload(source_email_id=f"src-{uuid.uuid4().hex}{NUL}").model_dump(mode="json")
    response = client.post("/v1/emails/intake", json=payload)
    assert response.status_code == 422
    assert "emails.source_email_id" in response.json()["detail"]


def test_api_does_not_reject_free_form_nul(client) -> None:
    """Free-form NUL must be sanitized at the boundary, not surfaced as 422."""
    payload = build_payload(
        subject=f"Rem{NUL}ittance adv{NUL}ice",
        documents=[DocumentInput(document_name="valid_remittance.pdf", mime_type="application/pdf", mock_profile="valid_remittance")],
    ).model_dump(mode="json")
    response = client.post("/v1/emails/intake", json=payload)
    assert response.status_code == 201
    body = response.json()
    assert body["documents"][0]["decision"] == "READY_FOR_PROCESSING"
    assert body["source_email_id"] == payload["source_email_id"]


# --------------------------------------------------------------------------
# PostgreSQL integration (gated)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_free_form_intake_fields_are_sanitized(tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path))
    source = f"pg-src-{uuid.uuid4().hex}"
    payload = build_payload(
        source_email_id=source,
        sender_name=f"Ann{NUL}a Lee",
        sender_company=f"{NUL}Acme{NUL}Corp",
        subject=f"Rem{NUL}ittance adv{NUL}ice",
        body=f"line one{NUL}\nline two{NUL}{NUL}",
    )
    with postgres_boundary() as (factory, track):
        with factory() as session:
            response = pipeline.process_email(payload, session)
            track(email_id=response.email_id, document_id=response.documents[0].id)
        with factory() as session:
            email = session.get(Email, response.email_id)
            document = session.get(Document, response.documents[0].id)
            # free-form prose is sanitized
            assert email.sender_name == "Anna Lee"
            assert email.sender_company == "AcmeCorp"
            assert email.subject == "Remittance advice"
            assert email.body == "line one\nline two"
            # structured intake fields are stored verbatim (they arrived clean)
            assert email.sender_email == "api@acme.test"
            assert email.correlation_id == "corr-1"
            assert email.n8n_execution_id == "exec-1"
            assert email.source_email_id == source
            assert document.document_name == "remittance.pdf"
            for value in (email.sender_name, email.sender_company, email.subject, email.body):
                assert_text(value, label="postgres free-form column")
            for value in (email.sender_email, email.correlation_id, email.n8n_execution_id, email.source_email_id, document.document_name):
                assert_text(value, label="postgres structured column")


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_structured_identifier_is_rejected(tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path))
    marker = f"pg-hash-{uuid.uuid4().hex}"
    payload = build_payload(
        source_email_id=marker,
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", content_hash=f"digest{NUL}tail")],
    )
    with postgres_boundary() as (factory, track):
        with factory() as session:
            with pytest.raises(NulByteRejectedError) as excinfo:
                pipeline.process_email(payload, session)
            session.rollback()
        assert excinfo.value.column_label == "documents.content_hash"
        with factory() as session:
            leaked = list(session.scalars(select(Email).where(Email.source_email_id == marker)))
            for email in leaked:
                track(email_id=email.id)
            assert not leaked, "a rejected structured identifier must leave no rows behind"


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_source_email_id_is_rejected(tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path))
    # matched on a NUL-free prefix: a NUL in a bind parameter is not a valid PG text value
    marker = f"pg-src-{uuid.uuid4().hex}"
    payload = build_payload(source_email_id=f"{marker}{NUL}")
    with postgres_boundary() as (factory, track):
        with factory() as session:
            with pytest.raises(NulByteRejectedError):
                pipeline.process_email(payload, session)
            session.rollback()
            leaked = list(session.scalars(select(Email).where(Email.source_email_id.startswith(marker))))
            for email in leaked:
                track(email_id=email.id)
            assert not leaked, "a rejected source_email_id must leave no rows behind"


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_audit_message_is_sanitized() -> None:
    with postgres_boundary() as (factory, track):
        with factory() as session:
            document = seed_document(session)
            track(email_id=document.email_id, document_id=document.id)
            record_audit(
                session,
                document_id=document.id,
                event_type="STAGE_FAILED",
                status=StageStatus.RETRYABLE_FAILURE,
                message=f"provider said: bad{NUL}thing",
                details={"stage": ProcessingStage.OCR},
            )
            session.commit()
            document_id = document.id

        with factory() as session:
            audits = list(session.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id)))
            messages = [audit.message for audit in audits]
            assert "provider said: badthing" in messages
            for message in messages:
                assert_text(message, label="postgres audit_events.message")


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_work_item_error_message_is_sanitized() -> None:
    now = datetime.now(timezone.utc)
    with postgres_boundary() as (factory, track):
        with factory() as session:
            document = seed_document(session)
            track(email_id=document.email_id, document_id=document.id)
            item = enqueue_document(session, document.id)
            item_id = item.id
            session.commit()

        with factory() as session:
            item = session.get(DocumentWorkItem, item_id)
            fail_work_item(item, code="HTTP_400", message=f"fail{NUL}msg")
            session.commit()

        with factory() as session:
            item = session.get(DocumentWorkItem, item_id)
            assert item.last_error_message == "failmsg"
            assert_text(item.last_error_message, label="postgres last_error_message")
