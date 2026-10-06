from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.db.models import Document, DocumentWorkItem, Email
from app.db.session import Base
from app.domain.enums import DocumentType, ProcessingStatus, StageStatus, WorkItemStatus
from app.services.work_queue import claim_next_work_item, complete_work_item, enqueue_document
from app.worker import DocumentWorker


def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'queue.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def create_email(db: Session, source: str = "queue-email") -> Email:
    email = Email(source_email_id=source, received_at=datetime.now(timezone.utc), sender_email="queue@example.test", status="RECEIVED")
    db.add(email)
    db.flush()
    return email


def create_document(db: Session, email: Email, *, document_id_suffix: str = "001", terminal: bool = False) -> Document:
    document = Document(
        email_id=email.id,
        document_name=f"queue-{document_id_suffix}.pdf",
        mime_type="application/pdf",
        document_type=DocumentType.REMITTANCE,
        extracted_data={"remittance_number": f"QUEUE-{document_id_suffix}"},
        processing_status=ProcessingStatus.COMPLETED if terminal else ProcessingStatus.PENDING,
        overall_status=ProcessingStatus.COMPLETED if terminal else ProcessingStatus.PENDING,
        current_stage="COMPLETE" if terminal else "OCR",
        stage_status=StageStatus.SUCCESS if terminal else StageStatus.PENDING,
        decision="READY_FOR_PROCESSING" if terminal else None,
    )
    db.add(document)
    db.flush()
    return document


def test_one_document_creates_one_work_item(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = create_document(db, create_email(db))
        item = enqueue_document(db, document.id)
        db.commit()
        assert item.document_id == document.id
        assert db.query(DocumentWorkItem).count() == 1


def test_five_documents_create_five_independent_work_items(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        email = create_email(db, "five-document-email")
        documents = [create_document(db, email, document_id_suffix=str(index)) for index in range(1, 6)]
        items = [enqueue_document(db, document.id) for document in documents]
        db.commit()
        assert len(items) == 5
        assert {item.document_id for item in items} == {document.id for document in documents}
        assert db.query(DocumentWorkItem).count() == 5


def test_claim_is_committed_before_processing_and_second_worker_cannot_claim(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = create_document(db, create_email(db))
        enqueue_document(db, document.id)
        db.commit()

    with factory() as first_db:
        claimed = claim_next_work_item(first_db, worker_id="worker-a", lease_seconds=30)
        assert claimed is not None
        first_db.commit()

    with factory() as observer_db:
        observed = observer_db.get(DocumentWorkItem, claimed.id)
        assert observed.status == WorkItemStatus.CLAIMED
        assert observed.worker_id == "worker-a"
        assert observed.claimed_at is not None
        assert observed.lease_expires_at is not None

    with factory() as second_db:
        assert claim_next_work_item(second_db, worker_id="worker-b", lease_seconds=30) is None


def test_two_workers_claim_different_work_items(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        email = create_email(db, "parallel-email")
        for index in range(2):
            document = create_document(db, email, document_id_suffix=f"parallel-{index}")
            enqueue_document(db, document.id)
        db.commit()

    with factory() as first_db:
        first = claim_next_work_item(first_db, worker_id="worker-a", lease_seconds=30)
        first_db.commit()
    with factory() as second_db:
        second = claim_next_work_item(second_db, worker_id="worker-b", lease_seconds=30)
        second_db.commit()

    assert first is not None and second is not None
    assert first.id != second.id
    assert first.document_id != second.document_id


def test_expired_lease_is_reclaimable(tmp_path) -> None:
    factory = session_factory(tmp_path)
    initial = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
    with factory() as db:
        document = create_document(db, create_email(db, "lease-email"))
        enqueue_document(db, document.id, available_at=initial)
        db.commit()
    with factory() as db:
        first = claim_next_work_item(db, worker_id="worker-a", lease_seconds=10, now=initial)
        db.commit()
    with factory() as db:
        recovered = claim_next_work_item(db, worker_id="worker-b", lease_seconds=10, now=initial + timedelta(seconds=11))
        db.commit()
    assert first is not None and recovered is not None
    assert recovered.id == first.id
    assert recovered.worker_id == "worker-b"
    assert recovered.attempt_count == 2


def test_completed_work_item_cannot_be_reclaimed(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = create_document(db, create_email(db, "completed-email"))
        item = enqueue_document(db, document.id)
        complete_work_item(item)
        db.commit()
    with factory() as db:
        assert claim_next_work_item(db, worker_id="worker-b", lease_seconds=10) is None


def test_worker_completes_terminal_document_without_reprocessing(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        document = create_document(db, create_email(db, "terminal-email"), terminal=True)
        item = enqueue_document(db, document.id)
        db.commit()
        item_id = item.id
        document_id = document.id

    worker = DocumentWorker(session_factory=factory, worker_id="worker-terminal", lease_seconds=30)
    assert worker.run_once() == document_id

    with factory() as db:
        stored_item = db.get(DocumentWorkItem, item_id)
        stored_document = db.get(Document, document_id)
        assert stored_item.status == WorkItemStatus.COMPLETED
        assert stored_document.attempt_count == 0
