from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.models import AuditEvent, Document, Email
from app.db.session import Base
from app.domain.enums import BusinessOutcome, ProcessingStage, ProcessingStatus, StageStatus
from app.services.document_lifecycle import advance_stage, begin_run, begin_stage, complete, fail_permanently, route_to_review, schedule_retry

STALE = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'document-lifecycle.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def add_document(db) -> Document:
    email = Email(source_email_id="lifecycle-email", received_at=datetime.now(timezone.utc), sender_email="lifecycle@example.test", status="RECEIVED")
    db.add(email)
    db.flush()
    document = Document(
        email_id=email.id,
        document_name="lifecycle.pdf",
        mime_type="application/pdf",
        processing_status="IN_PROGRESS",
        current_stage="OCR",
        stage_status="SUCCESS",
        overall_status="IN_PROGRESS",
        retryable=True,
        next_attempt_at=STALE,
        attempt_count=3,
    )
    db.add(document)
    db.flush()
    return document


def test_route_to_review_writes_needs_review_state_and_records_audit(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        route_to_review(db, document, stage=ProcessingStage.CLASSIFICATION, reason="classifier needs a human", details={"failure_code": "LLM_REVIEW_REQUIRED"})

        assert document.decision == BusinessOutcome.NEEDS_REVIEW
        assert document.decision_reason == "classifier needs a human"
        assert document.current_stage == ProcessingStage.CLASSIFICATION
        assert document.stage_status == StageStatus.NEEDS_REVIEW
        assert document.overall_status == ProcessingStatus.COMPLETED
        assert document.processing_status == ProcessingStatus.COMPLETED
        assert document.retryable is False
        assert document.next_attempt_at is None
        assert document.attempt_count == 3

        pending_audits = [row for row in db.new if isinstance(row, AuditEvent)]
        assert len(pending_audits) == 1
        audit = pending_audits[0]
        assert audit.document_id == document.id
        assert audit.event_type == "NEEDS_REVIEW"
        assert audit.status == StageStatus.NEEDS_REVIEW
        assert audit.message == "classifier needs a human"
        assert audit.details == {"failure_code": "LLM_REVIEW_REQUIRED"}


def test_fail_permanently_writes_final_failure_state_and_records_audit(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        fail_permanently(db, document, stage=ProcessingStage.CLASSIFICATION, reason="retry budget exhausted")

        assert document.last_error == "retry budget exhausted"
        assert document.decision == BusinessOutcome.FAILED
        assert document.decision_reason == "retry budget exhausted"
        assert document.retryable is False
        assert document.current_stage == ProcessingStage.CLASSIFICATION
        assert document.next_attempt_at is None
        assert document.overall_status == ProcessingStatus.FINAL_FAILURE
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.stage_status == StageStatus.FINAL_FAILURE
        assert document.attempt_count == 3

        pending_audits = [row for row in db.new if isinstance(row, AuditEvent)]
        assert len(pending_audits) == 1
        audit = pending_audits[0]
        assert audit.document_id == document.id
        assert audit.event_type == "FINAL_FAILURE"
        assert audit.status == StageStatus.FINAL_FAILURE
        assert audit.message == "retry budget exhausted"
        assert audit.details == {"next_attempt_at": None}


def test_schedule_retry_writes_pending_retry_state_and_records_audit(tmp_path) -> None:
    factory = session_factory(tmp_path)
    scheduled_for = datetime(2026, 3, 1, 12, 5, tzinfo=timezone.utc)
    with factory() as db:
        document = add_document(db)
        schedule_retry(db, document, stage=ProcessingStage.CLASSIFICATION, reason="transient classification failure", next_attempt_at=scheduled_for)

        assert document.last_error == "transient classification failure"
        assert document.decision == BusinessOutcome.FAILED
        assert document.decision_reason == "transient classification failure"
        assert document.retryable is True
        assert document.current_stage == ProcessingStage.CLASSIFICATION
        assert document.next_attempt_at == scheduled_for
        assert document.overall_status == ProcessingStatus.PENDING_RETRY
        assert document.processing_status == ProcessingStatus.PENDING_RETRY
        assert document.stage_status == StageStatus.RETRY_SCHEDULED
        assert document.attempt_count == 3

        pending_audits = [row for row in db.new if isinstance(row, AuditEvent)]
        assert len(pending_audits) == 1
        audit = pending_audits[0]
        assert audit.document_id == document.id
        assert audit.event_type == "RETRY_SCHEDULED"
        assert audit.status == StageStatus.RETRY_SCHEDULED
        assert audit.message == "transient classification failure"
        assert audit.details == {"next_attempt_at": scheduled_for.isoformat()}


@pytest.mark.parametrize(
    ("decision", "stage", "expected_stage"),
    [
        (BusinessOutcome.READY_FOR_PROCESSING, ProcessingStage.BUSINESS_DECISION, ProcessingStage.COMPLETE),
        (BusinessOutcome.NOT_APPLICABLE, ProcessingStage.APPLICABILITY, ProcessingStage.APPLICABILITY),
    ],
)
def test_complete_writes_terminal_success_state_without_auditing(tmp_path, decision, stage, expected_stage) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        document.decision = decision
        complete(document, stage=stage)

        assert document.current_stage == expected_stage
        assert document.stage_status == StageStatus.SUCCESS
        assert document.overall_status == ProcessingStatus.COMPLETED
        assert document.processing_status == ProcessingStatus.COMPLETED
        assert document.retryable is False
        assert document.next_attempt_at is None
        assert document.attempt_count == 3

        assert [row for row in db.new if isinstance(row, AuditEvent)] == []


def test_begin_run_marks_in_progress_without_touching_stage_or_schedule(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        begin_run(document)

        assert document.current_stage == "OCR"
        assert document.stage_status == StageStatus.IN_PROGRESS
        assert document.processing_status == ProcessingStatus.IN_PROGRESS
        assert document.overall_status == ProcessingStatus.IN_PROGRESS
        assert document.next_attempt_at == STALE
        assert document.attempt_count == 3

        assert [row for row in db.new if isinstance(row, AuditEvent)] == []


def test_begin_stage_enters_stage_in_progress_and_keeps_stale_next_attempt_at(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        begin_stage(document, stage=ProcessingStage.OCR)

        assert document.current_stage == ProcessingStage.OCR
        assert document.stage_status == StageStatus.IN_PROGRESS
        assert document.processing_status == ProcessingStatus.IN_PROGRESS
        assert document.overall_status == ProcessingStatus.IN_PROGRESS
        assert document.next_attempt_at == STALE
        assert document.attempt_count == 3

        assert [row for row in db.new if isinstance(row, AuditEvent)] == []


def test_advance_stage_clears_next_attempt_at_and_reenters_in_progress(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = add_document(db)
        advance_stage(document)

        assert document.current_stage == "OCR"
        assert document.stage_status == StageStatus.IN_PROGRESS
        assert document.processing_status == ProcessingStatus.IN_PROGRESS
        assert document.overall_status == ProcessingStatus.IN_PROGRESS
        assert document.next_attempt_at is None
        assert document.attempt_count == 3

        assert [row for row in db.new if isinstance(row, AuditEvent)] == []
