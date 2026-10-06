from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import Document
from app.domain.enums import BusinessOutcome, ProcessingStage, ProcessingStatus, StageStatus
from app.services.audit import record_audit


def complete(document: Document, *, stage) -> None:
    document.current_stage = stage if document.decision == BusinessOutcome.NOT_APPLICABLE else ProcessingStage.COMPLETE
    document.stage_status = StageStatus.SUCCESS
    document.overall_status = ProcessingStatus.COMPLETED
    document.processing_status = ProcessingStatus.COMPLETED
    document.retryable = False
    document.next_attempt_at = None


def begin_run(document: Document) -> None:
    document.stage_status = StageStatus.IN_PROGRESS
    document.processing_status = ProcessingStatus.IN_PROGRESS
    document.overall_status = ProcessingStatus.IN_PROGRESS


def begin_stage(document: Document, *, stage) -> None:
    document.current_stage = stage
    document.stage_status = StageStatus.IN_PROGRESS
    document.processing_status = ProcessingStatus.IN_PROGRESS
    document.overall_status = ProcessingStatus.IN_PROGRESS


def advance_stage(document: Document) -> None:
    document.next_attempt_at = None
    document.stage_status = StageStatus.IN_PROGRESS
    document.processing_status = ProcessingStatus.IN_PROGRESS
    document.overall_status = ProcessingStatus.IN_PROGRESS


def fail_permanently(db: Session, document: Document, *, stage, reason: str) -> None:
    document.last_error = reason
    document.decision = BusinessOutcome.FAILED
    document.decision_reason = reason
    document.retryable = False
    document.current_stage = stage
    document.next_attempt_at = None
    document.overall_status = ProcessingStatus.FINAL_FAILURE
    document.processing_status = ProcessingStatus.FAILED
    document.stage_status = StageStatus.FINAL_FAILURE
    record_audit(db, document_id=document.id, event_type="FINAL_FAILURE", status=document.stage_status, message=reason, details={"next_attempt_at": None})


def schedule_retry(db: Session, document: Document, *, stage, reason: str, next_attempt_at) -> None:
    document.last_error = reason
    document.decision = BusinessOutcome.FAILED
    document.decision_reason = reason
    document.retryable = True
    document.current_stage = stage
    document.next_attempt_at = next_attempt_at
    document.overall_status = ProcessingStatus.PENDING_RETRY
    document.processing_status = ProcessingStatus.PENDING_RETRY
    document.stage_status = StageStatus.RETRY_SCHEDULED
    record_audit(db, document_id=document.id, event_type="RETRY_SCHEDULED", status=document.stage_status, message=reason, details={"next_attempt_at": next_attempt_at.isoformat() if next_attempt_at else None})


def route_to_review(db: Session, document: Document, *, stage, reason: str, details: dict[str, Any]) -> None:
    document.decision = BusinessOutcome.NEEDS_REVIEW
    document.decision_reason = reason
    document.current_stage = stage
    document.stage_status = StageStatus.NEEDS_REVIEW
    document.overall_status = ProcessingStatus.COMPLETED
    document.processing_status = ProcessingStatus.COMPLETED
    document.retryable = False
    document.next_attempt_at = None
    record_audit(db, document_id=document.id, event_type="NEEDS_REVIEW", status=StageStatus.NEEDS_REVIEW, message=reason, details=details)
