from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.models import Document, DocumentWorkItem
from app.domain.enums import ProcessingStatus, StageStatus, WorkItemStatus


def enqueue_document(db: Session, document_id: int, *, available_at: datetime | None = None) -> DocumentWorkItem:
    item = DocumentWorkItem(
        document_id=document_id,
        run_id=uuid4().hex,
        status=WorkItemStatus.PENDING,
        available_at=available_at or datetime.now(timezone.utc),
    )
    db.add(item)
    db.flush()
    return item


def claim_next_work_item(db: Session, *, worker_id: str, lease_seconds: int, now: datetime | None = None) -> DocumentWorkItem | None:
    current_time = now or datetime.now(timezone.utc)
    eligible = or_(
        DocumentWorkItem.status.in_([WorkItemStatus.PENDING, WorkItemStatus.RETRY_SCHEDULED]),
        (DocumentWorkItem.status == WorkItemStatus.CLAIMED) & (DocumentWorkItem.lease_expires_at <= current_time),
    )
    item = db.scalar(
        select(DocumentWorkItem)
        .where(eligible, DocumentWorkItem.available_at <= current_time)
        .order_by(DocumentWorkItem.available_at, DocumentWorkItem.id)
        .with_for_update(skip_locked=True)
        .limit(1)
    )
    if item is None:
        return None
    item.status = WorkItemStatus.CLAIMED
    item.claimed_at = current_time
    item.lease_expires_at = current_time + timedelta(seconds=lease_seconds)
    item.worker_id = worker_id
    item.attempt_count += 1
    item.last_error_code = None
    item.last_error_message = None
    db.flush()
    return item


def complete_work_item(item: DocumentWorkItem) -> None:
    item.status = WorkItemStatus.COMPLETED
    item.completed_at = datetime.now(timezone.utc)
    item.lease_expires_at = None


def fail_work_item(item: DocumentWorkItem, *, code: str, message: str) -> None:
    item.status = WorkItemStatus.FAILED
    item.last_error_code = code
    item.last_error_message = message
    item.completed_at = datetime.now(timezone.utc)
    item.lease_expires_at = None


def reschedule_work_item(item: DocumentWorkItem, *, available_at: datetime, code: str | None = None, message: str | None = None) -> None:
    item.status = WorkItemStatus.RETRY_SCHEDULED
    item.available_at = available_at
    item.lease_expires_at = None
    item.last_error_code = code
    item.last_error_message = message


def document_is_terminal(document: Document) -> bool:
    return document.overall_status in {ProcessingStatus.COMPLETED, ProcessingStatus.FINAL_FAILURE} or document.stage_status in {StageStatus.NEEDS_REVIEW, StageStatus.FINAL_FAILURE}
