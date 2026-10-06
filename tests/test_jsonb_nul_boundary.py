"""Regression tests for NUL bytes inside JSONB values (Phase 8).

PostgreSQL ``jsonb`` input rejects ``\\u0000`` with SQLSTATE ``22P05``
(``unsupported Unicode escape sequence``) and aborts the whole transaction, so
one malformed structured value used to be able to take error and audit
persistence - and therefore the worker's reschedule path - down with it.

The fix is deliberately small:

* ``app/services/text_sanitization.py`` walks the Python value and rejects it
  when *any* nested string, key or item contains a NUL byte;
* the ``before_flush`` hook raises :class:`NulByteRejectedError` before
  SQLAlchemy emits a statement, leaving the in-memory value untouched;
* intake/processing paths translate the typed rejection into the controlled
  behaviour they already had (HTTP 422 for intake, NEEDS_REVIEW for a run);
* plain ``json`` columns (``processing_attempts.result``,
  ``side_effect_operations.response``) are out of scope and unchanged.

PostgreSQL integration tests run only when ``ENGINE_PG_TEST_DATABASE_URL`` is
set; they demonstrate both sides of the boundary - the raw ``22P05`` failure
that still exists when data bypasses the application, and the typed rejection
that now happens instead.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DBAPIError, DataError
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.db.models import (
    AuditEvent,
    Base,
    Document,
    DocumentWorkItem,
    Email,
    ExtractedField,
    ProcessingAttempt,
    ProcessingRun,
    SideEffectOperation,
)
from app.domain.enums import BusinessOutcome, DocumentType, ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import CustomerCandidates, DocumentInput, EmailIntakeRequest
from app.services.audit import record_audit
from app.services.classification import ClassificationResult
from app.services.pipeline import DocumentPipeline
from app.services.text_sanitization import (
    NulByteRejectedError,
    ensure_jsonb_value,
    jsonb_contains_nul,
    validate_persisted_object,
)
from app.services.work_queue import enqueue_document
from app.worker import DocumentWorker

PG_URL = os.environ.get("ENGINE_PG_TEST_DATABASE_URL")
NUL = "\x00"


# --------------------------------------------------------------------------
# shared scaffolding
# --------------------------------------------------------------------------


def make_settings(tmp_path, **overrides) -> Settings:
    options = {
        "llm_provider": "mock",
        "document_storage_root": str(tmp_path / "documents"),
        "processing_mode": "async",
    }
    options.update(overrides)
    return Settings(**options)


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'jsonb.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


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
        "documents": [
            DocumentInput(
                document_name="remittance.pdf",
                mime_type="application/pdf",
            )
        ],
    }
    fields.update(overrides)
    return EmailIntakeRequest(**fields)


def seed_document(session) -> Document:
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
    )
    session.add(document)
    session.flush()
    return document


def seed_attempt(session, document: Document) -> ProcessingAttempt:
    now = datetime.now(timezone.utc)
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
    return attempt


def value_contains_nul(value) -> bool:
    return jsonb_contains_nul(value)


# --------------------------------------------------------------------------
# 1. value-level detection
# --------------------------------------------------------------------------

REJECTED_SAMPLES = [
    ("scalar string", f"ABC{NUL}123"),
    ("nested dict value", {"customer": {"id": f"CUST{NUL}001"}}),
    ("nested dict key", {f"bad{NUL}key": "clean"}),
    ("list item", {"items": [{"description": f"abc{NUL}def"}]}),
    ("tuple item", {"items": (f"tuple{NUL}value",)}),
    ("deeply nested value", {"a": {"b": {"c": [None, 1, {"d": [f"deep{NUL}nest"]}]}}}),
    ("mixed clean and nul", {"clean": "ok", "bad": f"no{NUL}pe", "nested": {"fine": 1}}),
    ("nul only", NUL),
    ("nul in unicode text", f"unicodé{NUL}wörld{NUL}🧾"),
]


@pytest.mark.parametrize("label,value", REJECTED_SAMPLES, ids=[label for label, _ in REJECTED_SAMPLES])
def test_nul_bearing_jsonb_values_are_rejected(label: str, value) -> None:
    assert jsonb_contains_nul(value) is True
    with pytest.raises(NulByteRejectedError) as excinfo:
        ensure_jsonb_value(value, "documents.extracted_data")
    assert excinfo.value.column_label == "documents.extracted_data"
    assert NUL not in str(excinfo.value), "the offending value must not be echoed"


CLEAN_SAMPLES = [
    None,
    "",
    "plain text",
    0,
    1,
    -1,
    3.14,
    True,
    False,
    [],
    {},
    {"clean": "value"},
    {"nested": {"deep": [{"ok": "value"}, None, 7, 2.5, True]}},
    {"unicode": "café naïve — Ω με 🧾"},
]


@pytest.mark.parametrize("value", CLEAN_SAMPLES, ids=[repr(value)[:32] for value in CLEAN_SAMPLES])
def test_clean_jsonb_values_pass_unchanged(value) -> None:
    assert jsonb_contains_nul(value) is False
    ensure_jsonb_value(value, "documents.extracted_data")  # must not raise


def test_none_and_scalars_are_not_inspected_as_text() -> None:
    for value in (None, 0, 1.5, True, False, [], {}):
        assert jsonb_contains_nul(value) is False


# --------------------------------------------------------------------------
# 2. persistence boundary: the four JSONB columns
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "attribute,column_label",
    [
        ("extracted_data", "documents.extracted_data"),
        ("evidence", "documents.evidence"),
    ],
)
def test_document_jsonb_columns_are_rejected_at_flush(db, attribute: str, column_label: str) -> None:
    with db() as session:
        document = seed_document(session)
        payload = {"invoice_number": f"ABC{NUL}123", "customer": {"id": f"CUST{NUL}001"}}
        setattr(document, attribute, payload)
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        assert excinfo.value.column_label == column_label
        assert getattr(document, attribute) is payload, "the rejected value must stay unchanged in memory"
        assert document.document_name == "remittance.pdf", "no partial mutation"
        assert NUL not in str(excinfo.value)
        session.rollback()

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Document)) == 0, "no row may persist"


def test_extracted_field_value_is_rejected_at_flush(db) -> None:
    with db() as session:
        document = seed_document(session)
        attempt = seed_attempt(session, document)
        payload = {"raw": f"REM{NUL}-001"}
        field = ExtractedField(
            attempt_id=attempt.id,
            document_id=document.id,
            run_id=attempt.run_id,
            field_name="remittance_number",
            field_value=payload,
            value_type="string",
            source="OCR",
        )
        session.add(field)
        with pytest.raises(NulByteRejectedError) as excinfo:
            session.flush()
        assert excinfo.value.column_label == "extracted_fields.field_value"
        assert field.field_value is payload, "the rejected value must stay unchanged in memory"
        session.rollback()

    with db() as session:
        assert session.scalar(select(func.count()).select_from(ExtractedField)) == 0


def test_audit_details_are_rejected_before_the_row_exists(db) -> None:
    with db() as session:
        document = seed_document(session)
        payload = {"provider_metadata": {"stderr": f"bad{NUL}output"}}
        with pytest.raises(NulByteRejectedError) as excinfo:
            record_audit(
                session,
                document_id=document.id,
                event_type="STAGE_FAILED",
                status=StageStatus.RETRYABLE_FAILURE,
                message="provider failed",
                details=payload,
            )
            session.flush()
        assert excinfo.value.column_label == "audit_events.details"
        assert payload == {"provider_metadata": {"stderr": f"bad{NUL}output"}}, "the rejected value must stay unchanged"
        assert list(session.new) == [], "no audit row may even be pending"
        session.rollback()

    with db() as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_clean_jsonb_values_persist_unchanged(db) -> None:
    payload = {"invoice_number": "INV-1", "customer": {"id": "CUST-001"}, "items": [{"description": "ok"}]}
    with db() as session:
        document = seed_document(session)
        attempt = seed_attempt(session, document)
        document.extracted_data = payload
        document.evidence = {"decision": {"outcome": "READY_FOR_PROCESSING"}}
        attempt.result = {"provider": "test"}
        session.add(
            ExtractedField(
                attempt_id=attempt.id,
                document_id=document.id,
                run_id=attempt.run_id,
                field_name="invoice_number",
                field_value=payload,
                value_type="object",
                source="OCR",
            )
        )
        record_audit(
            session,
            document_id=document.id,
            event_type="STAGE_SUCCEEDED",
            status=StageStatus.SUCCESS,
            message="clean",
            details={"stage": ProcessingStage.OCR},
        )
        session.commit()
        document_id = document.id

    with db() as session:
        document = session.get(Document, document_id)
        assert document.extracted_data == payload
        assert document.evidence == {"decision": {"outcome": "READY_FOR_PROCESSING"}}
        field = session.scalar(select(ExtractedField).where(ExtractedField.document_id == document_id))
        assert field.field_value == payload
        audit = session.scalar(select(AuditEvent).where(AuditEvent.document_id == document_id))
        assert audit.details == {"stage": ProcessingStage.OCR}


def test_plain_json_columns_still_persist_nul_bearing_values(db) -> None:
    """``processing_attempts.result`` and ``side_effect_operations.response`` are unchanged."""
    payload = {"message": f"crm rejected{NUL} payload", "code": "HTTP_400"}
    with db() as session:
        document = seed_document(session)
        attempt = seed_attempt(session, document)
        attempt.result = payload
        operation = SideEffectOperation(
            document_id=document.id,
            action_type="CRM_ACTION",
            idempotency_key=f"document:{document.id}:CRM_ACTION",
            status="FAILED",
            response=payload,
        )
        session.add(operation)
        session.commit()
        document_id = document.id

    with db() as session:
        attempt = session.scalar(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id))
        operation = session.scalar(select(SideEffectOperation).where(SideEffectOperation.document_id == document_id))
        assert attempt.result == payload, "plain JSON must keep the NUL byte as before"
        assert operation.response == payload, "plain JSON must keep the NUL byte as before"


# --------------------------------------------------------------------------
# 3. intake: mock_profile is the externally triggerable path
# --------------------------------------------------------------------------


def test_intake_mock_profile_nul_is_rejected_before_any_row(db, tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path))
    payload = build_payload(documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile=f"x{NUL}y")])
    with db() as session:
        with pytest.raises(NulByteRejectedError) as excinfo:
            pipeline.process_email(payload, session)
        session.rollback()
    assert excinfo.value.column_label == "documents.evidence"
    assert f"x{NUL}y" not in str(excinfo.value)

    with db() as session:
        assert session.scalar(select(func.count()).select_from(Email)) == 0
        assert session.scalar(select(func.count()).select_from(Document)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_api_intake_mock_profile_nul_returns_422_not_500(client) -> None:
    payload = build_payload(
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile=f"x{NUL}y")]
    ).model_dump(mode="json")
    response = client.post("/v1/emails/intake", json=payload)
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "documents.evidence" in detail
    assert f"x{NUL}y" not in detail, "the offending value must not be echoed"
    assert NUL not in response.text


# --------------------------------------------------------------------------
# 4. processing paths: the typed rejection becomes NEEDS_REVIEW
# --------------------------------------------------------------------------


class NulOCR:
    """OCR stand-in whose structured observations carry a NUL byte."""

    name = "NUL_OCR"
    provider_order = ["NUL_OCR"]

    def extract_with_failover(self, *, document_name, mock_profile=None, storage_reference=None, mime_type=None, page_count=None):
        from app.adapters.ocr import ExtractionResult

        result = ExtractionResult(
            text="invoice_number: INV-42",
            extracted_data={"invoice_number": f"INV-42{NUL}bad"},
            score=0.96,
            provider="NUL_OCR",
        )
        return result, [], [("NUL_OCR", result)]


def nul_classification_result(*, extracted_data: dict | None = None, raw_metadata: dict | None = None) -> ClassificationResult:
    return ClassificationResult(
        document_type=DocumentType.INVOICE,
        confidence=0.95,
        normalized_confidence=0.95,
        applicable=True,
        evidence={},
        assessment=None,
        extracted_data=extracted_data if extracted_data is not None else {"invoice_number": "INV-1"},
        supplemental_information=[],
        customer_candidates=CustomerCandidates(),
        provider="mock",
        model="mock-v1",
        prompt_version="v1",
        schema_version="v1",
        provider_request_id="req-1",
        warnings=[],
        ambiguities=[],
        raw_metadata=raw_metadata if raw_metadata is not None else {},
    )


class FixedClassifier:
    def __init__(self, result: ClassificationResult) -> None:
        self.result = result

    def classify(self, **kwargs) -> ClassificationResult:
        return self.result


def run_and_assert_needs_review(pipeline, db) -> tuple[Document, list[AuditEvent], list[ProcessingRun]]:
    with db() as session:
        document = seed_document(session)
        session.commit()
        document_id = document.id

    with db() as session:
        document = session.get(Document, document_id)
        pipeline.execute_run(document, session, trigger="INTAKE", profile=None, email_applicable=True, worker_id="nul-worker")
        session.commit()

    with db() as session:
        document = session.get(Document, document_id)
        audits = list(session.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id)))
        runs = list(session.scalars(select(ProcessingRun).where(ProcessingRun.document_id == document_id)))
        assert document.stage_status == StageStatus.NEEDS_REVIEW
        assert document.decision == BusinessOutcome.NEEDS_REVIEW
        assert document.processing_status == ProcessingStatus.COMPLETED
        assert document.overall_status == ProcessingStatus.COMPLETED
        assert runs and runs[-1].outcome == "NEEDS_REVIEW"
        review_events = [audit for audit in audits if audit.event_type == "NEEDS_REVIEW"]
        assert review_events, "the run must be recorded as NEEDS_REVIEW"
        for audit in audits:
            assert audit.message is None or value_contains_nul(audit.message) is False
            if audit.details is not None:
                assert value_contains_nul(audit.details) is False
        return document, audits, runs


def test_extracted_field_nul_is_routed_to_needs_review(db, tmp_path) -> None:
    pipeline = DocumentPipeline(make_settings(tmp_path), extractor=NulOCR())
    document, audits, _runs = run_and_assert_needs_review(pipeline, db)

    assert document.extracted_data is None, "the rejected extraction data must never be assigned"
    reason = next(audit.message for audit in audits if audit.event_type == "NEEDS_REVIEW")
    assert "extracted_fields.field_value" in reason, "the original reason must be kept"
    assert f"INV-42{NUL}bad" not in reason

    with db() as session:
        assert session.scalar(select(func.count()).select_from(ExtractedField).where(ExtractedField.document_id == document.id)) == 0


def test_classification_extracted_data_nul_is_routed_to_needs_review(db, tmp_path) -> None:
    nul_data = {"invoice_number": f"INV-42{NUL}bad"}
    pipeline = DocumentPipeline(make_settings(tmp_path), classifier=FixedClassifier(nul_classification_result(extracted_data=nul_data)))
    document, audits, _runs = run_and_assert_needs_review(pipeline, db)

    assert document.extracted_data == {}, "the rejected classification data must never be assigned"
    assert value_contains_nul(document.extracted_data) is False
    reason = next(audit.message for audit in audits if audit.event_type == "NEEDS_REVIEW")
    assert "documents.extracted_data" in reason
    assert f"INV-42{NUL}bad" not in reason


def test_classification_evidence_nul_is_routed_to_needs_review(db, tmp_path) -> None:
    nul_metadata = {"raw": f"provider{NUL}payload"}
    pipeline = DocumentPipeline(make_settings(tmp_path), classifier=FixedClassifier(nul_classification_result(raw_metadata=nul_metadata)))
    document, audits, _runs = run_and_assert_needs_review(pipeline, db)

    assert "classification" not in (document.evidence or {}), "the rejected classification evidence must never be assigned"
    assert value_contains_nul(document.evidence) is False, "the NUL byte must never reach documents.evidence"
    reason = next(audit.message for audit in audits if audit.event_type == "NEEDS_REVIEW")
    assert "documents.evidence" in reason
    assert f"provider{NUL}payload" not in reason


def test_worker_does_not_strand_the_queue_item_when_jsonb_is_rejected(db, tmp_path, monkeypatch) -> None:
    """The failure-amplification scenario: the work item must still finish."""
    settings = make_settings(tmp_path)
    monkeypatch.setattr("app.worker.DocumentPipeline", lambda _settings: DocumentPipeline(_settings, extractor=NulOCR()))

    with db() as session:
        document = seed_document(session)
        item = enqueue_document(session, document.id)
        item_id = item.id
        session.commit()
        document_id = document.id

    worker = DocumentWorker(session_factory=db, settings=settings, worker_id="nul-worker", lease_seconds=30)
    assert worker.run_once() == document_id

    with db() as session:
        item = session.get(DocumentWorkItem, item_id)
        document = session.get(Document, document_id)
        assert item.status == "COMPLETED", "the queue item must not stay CLAIMED"
        assert document.stage_status == StageStatus.NEEDS_REVIEW
        assert session.scalar(select(func.count()).select_from(ExtractedField).where(ExtractedField.document_id == document_id)) == 0


# --------------------------------------------------------------------------
# 5. PostgreSQL integration (gated)
# --------------------------------------------------------------------------


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_jsonb_input_fails_with_22p05_when_bypassed() -> None:
    """BEFORE: the same value sent straight to PostgreSQL aborts with 22P05."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    payload = json.dumps({"customer": {"id": f"CUST{NUL}0001"}})
    marker = f"pg-jsonb-probe-{uuid.uuid4().hex}"
    try:
        with pytest.raises(DataError) as excinfo:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO audit_events (event_type, status, message, details) VALUES (:event_type, 'FAILED', 'probe', CAST(:details AS jsonb))"),
                    {"event_type": marker, "details": payload},
                )
        assert getattr(excinfo.value.orig, "sqlstate", None) == "22P05"
        assert "cannot be converted to text" in str(excinfo.value)
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM audit_events WHERE event_type = :event_type"), {"event_type": marker}).scalar() == 0
    finally:
        engine.dispose()


PG_JSONB_CASES = [
    ("documents.extracted_data", "document"),
    ("documents.evidence", "document"),
    ("extracted_fields.field_value", "field"),
    ("audit_events.details", "audit"),
]


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
@pytest.mark.parametrize("column_label,kind", PG_JSONB_CASES, ids=[label for label, _ in PG_JSONB_CASES])
def test_postgres_jsonb_nul_is_rejected_before_postgresql_sees_it(column_label: str, kind: str) -> None:
    """AFTER: application detection -> typed rejection -> no 22P05, no row."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    payload = {"invoice_number": f"ABC{NUL}123", "customer": {"id": f"CUST{NUL}001"}}
    tables = ("documents", "audit_events", "extracted_fields")
    try:
        with engine.connect() as connection:
            before = {table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() for table in tables}

        with factory() as session:
            document = seed_document(session)
            connection = session.connection()
            target: object | None = None
            attribute = "details"
            if kind == "document":
                target = document
                attribute = column_label.split(".")[1]
                setattr(target, attribute, payload)
            elif kind == "field":
                attempt = seed_attempt(session, document)
                target = ExtractedField(
                    attempt_id=attempt.id,
                    document_id=document.id,
                    run_id=attempt.run_id,
                    field_name="invoice_number",
                    field_value=payload,
                    value_type="object",
                    source="OCR",
                )
                attribute = "field_value"
                session.add(target)

            with pytest.raises(NulByteRejectedError) as excinfo:
                if kind == "audit":
                    record_audit(
                        session,
                        document_id=document.id,
                        event_type="STAGE_FAILED",
                        status=StageStatus.RETRYABLE_FAILURE,
                        message="provider failed",
                        details=payload,
                    )
                session.flush()

            assert excinfo.value.column_label == column_label
            assert not isinstance(excinfo.value, DBAPIError), "the rejection must not come from PostgreSQL"
            assert NUL not in str(excinfo.value)
            if target is not None:
                assert getattr(target, attribute) is payload, "the rejected value must stay unchanged in memory"

            # PostgreSQL never saw the statement: the transaction is still usable,
            # which it would not be if jsonb_in had raised 22P05.
            assert connection.exec_driver_sql("SELECT 1").scalar() == 1
            session.rollback()

        with engine.connect() as connection:
            after = {table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar() for table in tables}
        assert after == before, "the rejected flush must not leave a row behind"
    finally:
        engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_intake_mock_profile_nul_returns_422_not_500() -> None:
    from fastapi.testclient import TestClient

    from app.db.session import get_db
    from app.main import app

    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    marker = f"pg-mock-{uuid.uuid4().hex}"
    payload = build_payload(
        source_email_id=marker,
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile=f"x{NUL}y")],
    ).model_dump(mode="json")

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    try:
        client = TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"}, raise_server_exceptions=False)
        response = client.post("/v1/emails/intake", json=payload)
        assert response.status_code == 422, response.text
        assert "documents.evidence" in response.json()["detail"]
        assert NUL not in response.text
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM emails WHERE source_email_id = :marker"), {"marker": marker}).scalar() == 0
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_plain_json_columns_still_accept_nul_values() -> None:
    """E/F: ``processing_attempts.result`` and ``side_effect_operations.response`` stay unchanged."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    payload = {"message": f"crm rejected{NUL} payload", "code": "HTTP_400"}
    try:
        with factory() as session:
            document = seed_document(session)
            attempt = seed_attempt(session, document)
            attempt.result = payload
            operation = SideEffectOperation(
                document_id=document.id,
                action_type="CRM_ACTION",
                idempotency_key=f"document:{document.id}:JSONB-PHASE",
                status="FAILED",
                response=payload,
            )
            session.add(operation)
            session.commit()
            document_id = document.id

        with factory() as session:
            attempt = session.scalar(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id))
            operation = session.scalar(select(SideEffectOperation).where(SideEffectOperation.document_id == document_id))
            assert attempt.result == payload, "plain JSON columns must keep accepting the value"
            assert operation.response == payload, "plain JSON columns must keep accepting the value"
    finally:
        engine.dispose()
