from __future__ import annotations

import argparse
import os
import socket
import time
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import Document, DocumentWorkItem
from app.db.session import SessionLocal
from app.domain.enums import ProcessingStage, ProcessingStatus
from app.services.audit import record_audit
from app.services.document_lifecycle import begin_run
from app.services.pipeline import DocumentPipeline
from app.services.work_queue import claim_next_work_item, complete_work_item, document_is_terminal, fail_work_item, reschedule_work_item


class DocumentWorker:
    def __init__(self, *, session_factory=SessionLocal, worker_id: str | None = None, lease_seconds: int | None = None, settings=None) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds = lease_seconds or self.settings.work_queue_lease_seconds

    def run_once(self) -> int | None:
        with self.session_factory() as claim_db:
            item = claim_next_work_item(claim_db, worker_id=self.worker_id, lease_seconds=self.lease_seconds)
            if item is None:
                return None
            item_id = item.id
            document_id = item.document_id
            claim_db.commit()

        with self.session_factory() as db:
            item = db.get(DocumentWorkItem, item_id)
            document = db.get(Document, document_id)
            if item is None or document is None:
                if item is not None:
                    fail_work_item(item, code="DOCUMENT_NOT_FOUND", message="queued document was not found")
                    db.commit()
                return document_id
            if document_is_terminal(document):
                complete_work_item(item)
                db.commit()
                return document.id

            pipeline = DocumentPipeline(self.settings)
            profile = (document.evidence or {}).get("mock_profile")
            document.current_stage = document.current_stage or ProcessingStage.DOCUMENT_PARSING
            begin_run(document)
            record_audit(db, document_id=document.id, event_type="PROCESSING_STARTED", status=ProcessingStatus.IN_PROGRESS, message="document processing started", details={"stage": document.current_stage, "work_item_id": item.id})
            db.commit()
            trigger = "INTAKE" if not document.attempt_count else "AUTO_RETRY"
            pipeline.execute_run(
                document,
                db,
                trigger=trigger,
                profile=str(profile) if profile else None,
                email_applicable=document.email.applicability if document.email else None,
                worker_id=self.worker_id,
                work_item_id=item.id,
            )
            if document.processing_status == ProcessingStatus.PENDING_RETRY or document.overall_status == ProcessingStatus.PENDING_RETRY:
                reschedule_work_item(item, available_at=document.next_attempt_at or datetime.now(timezone.utc), code=document.last_error, message=document.decision_reason)
            else:
                # Non-terminal, non-pending states are drained on the next lease.
                complete_work_item(item)
            db.commit()
            return document.id

    def run_forever(self, *, poll_seconds: float = 1.0) -> None:
        while True:
            if self.run_once() is None:
                time.sleep(poll_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Process PostgreSQL-backed document work items")
    parser.add_argument("--once", action="store_true", help="process at most one work item")
    parser.add_argument("--worker-id", default=None)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args()
    worker = DocumentWorker(worker_id=args.worker_id)
    if args.once:
        worker.run_once()
    else:
        worker.run_forever(poll_seconds=args.poll_seconds)


if __name__ == "__main__":
    main()
