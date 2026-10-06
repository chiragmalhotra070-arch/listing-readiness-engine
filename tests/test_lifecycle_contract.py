from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.llm import LLMError, MockLLMProvider
from app.config import Settings
from app.db.models import AuditEvent, Document, DocumentWorkItem, Email
from app.db.session import Base
from app.domain.enums import DocumentType, WorkItemStatus
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.classification import LLMDocumentClassifier
from app.services.work_queue import enqueue_document
from app.services.pipeline import DocumentPipeline
from app.worker import DocumentWorker

STALE = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)
NON_NULL = object()


class _FailOnceClassifier:
    name = "contract-fail-once"
    model = "test-v1"

    def __init__(self) -> None:
        self.remaining_failures = 1
        self.delegate = MockLLMProvider(model=self.model)

    def analyze(self, request):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise LLMError("transient classification failure", code="LLM_TIMEOUT", retryable=True)
        return self.delegate.analyze(request)


class _AlwaysFailClassifier:
    name = "contract-always-fail"
    model = "test-v1"

    def analyze(self, request):
        raise LLMError("transient classification failure", code="LLM_TIMEOUT", retryable=True)


class _NeedsReviewClassifier:
    name = "contract-needs-review"
    model = "test-v1"

    def analyze(self, request):
        raise LLMError("classifier needs a human", code="LLM_REVIEW_REQUIRED", retryable=False, needs_review=True)


class _NoopPipeline:
    def __init__(self, *args, **kwargs) -> None:
        pass

    def execute_run(self, *args, **kwargs):
        return None


class _StaticStorage:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def read(self, reference: str) -> bytes:
        return self.content


def factory(tmp_path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / f'{name}.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(llm_provider="mock", document_storage_root=str(tmp_path / "documents"))
    defaults.update(overrides)
    return Settings(**defaults)


def payload(source: str, *, profile: str = "valid_remittance") -> EmailIntakeRequest:
    return EmailIntakeRequest(
        source_email_id=source,
        received_at=datetime.now(timezone.utc),
        sender_email="contract@example.test",
        applicability=True,
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile=profile)],
    )


def add_email(db, source: str) -> Email:
    email = Email(source_email_id=source, received_at=datetime.now(timezone.utc), sender_email="contract@example.test", applicability=True, status="RECEIVED")
    db.add(email)
    db.flush()
    return email


def add_document(db, email: Email, **overrides) -> Document:
    fields = dict(
        email_id=email.id,
        document_name="contract.pdf",
        mime_type="application/pdf",
        document_type=DocumentType.REMITTANCE,
        extracted_data={},
        processing_status="PENDING",
        current_stage="DOCUMENT_PARSING",
        stage_status="PENDING",
        overall_status="PENDING",
    )
    fields.update(overrides)
    document = Document(**fields)
    db.add(document)
    db.flush()
    return document


def _dt(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def lifecycle_fields(document: Document) -> dict:
    return {
        "processing_status": str(document.processing_status),
        "overall_status": str(document.overall_status),
        "stage_status": str(document.stage_status),
        "current_stage": str(document.current_stage),
        "decision": str(document.decision) if document.decision is not None else None,
        "retryable": document.retryable,
        "next_attempt_at": _dt(document.next_attempt_at),
        "attempt_count": document.attempt_count,
    }


def assert_lifecycle_fields(document: Document, expected: dict) -> None:
    actual = lifecycle_fields(document)
    wanted = dict(expected)
    if "next_attempt_at" in wanted and wanted["next_attempt_at"] is not NON_NULL:
        wanted["next_attempt_at"] = _dt(wanted["next_attempt_at"])
    if wanted.get("next_attempt_at") is NON_NULL:
        assert actual["next_attempt_at"] is not None, "expected next_attempt_at to be set"
        wanted.pop("next_attempt_at")
        actual.pop("next_attempt_at")
    assert actual == wanted, f"\nactual:   {actual}\nexpected: {wanted}"


def test_worker_pre_run_write_marks_in_progress_and_keeps_stale_next_attempt_at(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("app.worker.DocumentPipeline", _NoopPipeline)
    db_factory = factory(tmp_path, "contract-pre-run")
    with db_factory() as db:
        document = add_document(db, add_email(db, "contract-pre-run"), next_attempt_at=STALE)
        enqueue_document(db, document.id)
        db.commit()
        document_id = document.id
        item_id = db.scalar(select(DocumentWorkItem)).id

    worker = DocumentWorker(session_factory=db_factory, worker_id="contract-worker", settings=make_settings(tmp_path, processing_mode="async"))
    assert worker.run_once() == document_id

    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "IN_PROGRESS",
                "overall_status": "IN_PROGRESS",
                "stage_status": "IN_PROGRESS",
                "current_stage": "DOCUMENT_PARSING",
                "decision": None,
                "retryable": None,
                "next_attempt_at": STALE,
                "attempt_count": 0,
            },
        )
        item = db.get(DocumentWorkItem, item_id)
        assert item.status == WorkItemStatus.COMPLETED
        assert item.lease_expires_at is None
        audits = list(db.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id, AuditEvent.event_type == "PROCESSING_STARTED")))
        assert len(audits) == 1
        assert str(audits[0].status) == "IN_PROGRESS"


def test_worker_final_failure_claim_completes_item_without_touching_document(tmp_path) -> None:
    db_factory = factory(tmp_path, "contract-terminal")
    with db_factory() as db:
        document = add_document(
            db,
            add_email(db, "contract-terminal"),
            processing_status="FAILED",
            overall_status="FINAL_FAILURE",
            stage_status="FINAL_FAILURE",
            current_stage="CLASSIFICATION",
            decision="FAILED",
            decision_reason="transient classification failure",
            retryable=False,
            last_error="transient classification failure",
            attempt_count=7,
        )
        enqueue_document(db, document.id)
        db.commit()
        document_id = document.id
        item_id = db.scalar(select(DocumentWorkItem)).id

    worker = DocumentWorker(session_factory=db_factory, worker_id="contract-worker", settings=make_settings(tmp_path, processing_mode="async"))
    assert worker.run_once() == document_id

    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "FAILED",
                "overall_status": "FINAL_FAILURE",
                "stage_status": "FINAL_FAILURE",
                "current_stage": "CLASSIFICATION",
                "decision": "FAILED",
                "retryable": False,
                "next_attempt_at": None,
                "attempt_count": 7,
            },
        )
        item = db.get(DocumentWorkItem, item_id)
        assert item.status == WorkItemStatus.COMPLETED
        assert list(db.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id))) == []


def test_worker_retryable_failure_reschedules_item_at_document_next_attempt_at(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path, processing_mode="async", retry_base_delay_seconds=0.0)

    def build_pipeline(s):
        return DocumentPipeline(s, classifier=LLMDocumentClassifier(s, provider=_FailOnceClassifier()))

    monkeypatch.setattr("app.worker.DocumentPipeline", build_pipeline)
    db_factory = factory(tmp_path, "contract-retry-handoff")
    with db_factory() as db:
        response = build_pipeline(settings).process_email(payload("contract-retry-handoff"), db)
        document_id = response.documents[0].id

    worker = DocumentWorker(session_factory=db_factory, worker_id="contract-worker", settings=settings)
    assert worker.run_once() == document_id

    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "PENDING_RETRY",
                "overall_status": "PENDING_RETRY",
                "stage_status": "RETRY_SCHEDULED",
                "current_stage": "CLASSIFICATION",
                "decision": "FAILED",
                "retryable": True,
                "next_attempt_at": NON_NULL,
                "attempt_count": 2,
            },
        )
        item = db.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id))
        assert item.status == WorkItemStatus.RETRY_SCHEDULED
        assert _dt(item.available_at) == _dt(document.next_attempt_at)
        assert item.lease_expires_at is None
        assert item.attempt_count == 1


def test_reprocess_resets_to_in_progress_but_keeps_stale_next_attempt_at(tmp_path) -> None:
    content = b"contract-bytes"
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings)
    pipeline.storage = _StaticStorage(content)
    pipeline.execute_run = lambda *args, **kwargs: None
    db_factory = factory(tmp_path, "contract-reprocess")
    with db_factory() as db:
        document = add_document(
            db,
            add_email(db, "contract-reprocess"),
            processing_status="PENDING_RETRY",
            overall_status="PENDING_RETRY",
            stage_status="RETRY_SCHEDULED",
            current_stage="CRM_ACTION",
            decision="FAILED",
            decision_reason="crm timeout",
            last_error="crm timeout",
            retryable=True,
            next_attempt_at=STALE,
            attempt_count=5,
            storage_reference="contract/doc.pdf",
            content_hash=hashlib.sha256(content).hexdigest(),
        )
        db.commit()
        document_id = document.id

    with db_factory() as db:
        result = pipeline.reprocess_document(document_id, db)
        assert result.id == document_id

    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "IN_PROGRESS",
                "overall_status": "IN_PROGRESS",
                "stage_status": "IN_PROGRESS",
                "current_stage": "OCR",
                "decision": "FAILED",
                "retryable": True,
                "next_attempt_at": STALE,
                "attempt_count": 5,
            },
        )
        assert document.last_error == "crm timeout"
        audits = list(db.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id, AuditEvent.event_type == "PROCESSING_STARTED")))
        assert len(audits) == 1


def test_sync_rollup_marks_email_completed_with_failure_on_final_failure(tmp_path) -> None:
    settings = make_settings(tmp_path, retry_max_attempts=2)
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=_AlwaysFailClassifier()))
    db_factory = factory(tmp_path, "contract-rollup-fail")
    with db_factory() as db:
        response = pipeline.process_email(payload("contract-rollup-fail"), db)
        document_id = response.documents[0].id
    with db_factory() as db:
        email = db.scalar(select(Email))
        assert email.status == "COMPLETED_WITH_FAILURE"
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "FAILED",
                "overall_status": "FINAL_FAILURE",
                "stage_status": "FINAL_FAILURE",
                "current_stage": "CLASSIFICATION",
                "decision": "FAILED",
                "retryable": False,
                "next_attempt_at": None,
                "attempt_count": 4,
            },
        )


def test_async_intake_leaves_email_received(tmp_path) -> None:
    settings = make_settings(tmp_path, processing_mode="async")
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "contract-rollup-async")
    with db_factory() as db:
        pipeline.process_email(payload("contract-rollup-async"), db)
    with db_factory() as db:
        email = db.scalar(select(Email))
        assert email.status == "RECEIVED"
        document = db.scalar(select(Document))
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "PENDING",
                "overall_status": "PENDING",
                "stage_status": "PENDING",
                "current_stage": "DOCUMENT_PARSING",
                "decision": None,
                "retryable": None,
                "next_attempt_at": None,
                "attempt_count": 0,
            },
        )


def test_state_matrix_needs_review_from_classifier(tmp_path) -> None:
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=_NeedsReviewClassifier()))
    db_factory = factory(tmp_path, "contract-needs-review")
    with db_factory() as db:
        response = pipeline.process_email(payload("contract-needs-review"), db)
        document_id = response.documents[0].id
    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "COMPLETED",
                "overall_status": "COMPLETED",
                "stage_status": "NEEDS_REVIEW",
                "current_stage": "CLASSIFICATION",
                "decision": "NEEDS_REVIEW",
                "retryable": False,
                "next_attempt_at": None,
                "attempt_count": 2,
            },
        )
        email = db.scalar(select(Email))
        assert email.status == "COMPLETED"


def test_state_matrix_completed_happy_path(tmp_path) -> None:
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "contract-happy")
    with db_factory() as db:
        response = pipeline.process_email(payload("contract-happy"), db)
        document_id = response.documents[0].id
    with db_factory() as db:
        document = db.get(Document, document_id)
        assert_lifecycle_fields(
            document,
            {
                "processing_status": "COMPLETED",
                "overall_status": "COMPLETED",
                "stage_status": "SUCCESS",
                "current_stage": "COMPLETE",
                "decision": "READY_FOR_PROCESSING",
                "retryable": False,
                "next_attempt_at": None,
                "attempt_count": 6,
            },
        )
