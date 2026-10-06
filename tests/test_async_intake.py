from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import Document, DocumentWorkItem
from app.db.session import Base
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.pipeline import DocumentPipeline
from app.worker import DocumentWorker


def session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sync-intake.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def payload(source: str, profiles: list[str]) -> EmailIntakeRequest:
    return EmailIntakeRequest(
        source_email_id=source,
        received_at=datetime.now(timezone.utc),
        sender_email="sync@example.test",
        applicability=True,
        documents=[DocumentInput(document_name=f"{profile}.pdf", mime_type="application/pdf", mock_profile=profile) for profile in profiles],
    )


def pipeline(tmp_path, mode: str = "sync") -> DocumentPipeline:
    return DocumentPipeline(Settings(processing_mode=mode, llm_provider="mock", document_storage_root=str(tmp_path / "documents")))


def test_async_intake_returns_persisted_pending_state_and_creates_work_item(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        response = pipeline(tmp_path, mode="async").process_email(payload("async-one", ["valid_remittance"]), db)
        document = db.scalar(select(Document))
        work_item = db.scalar(select(DocumentWorkItem))
        assert document is not None
        assert work_item is not None
        assert work_item.document_id == document.id
        assert response.documents[0].document_id == document.id
        assert response.documents[0].decision is None
        assert response.documents[0].processing_status == "PENDING"
        assert response.documents[0].current_stage == "DOCUMENT_PARSING"
        assert response.documents[0].stage_status == "PENDING"
        assert response.documents[0].overall_status == "PENDING"
        assert document.overall_status == "PENDING"


def test_async_intake_is_idempotent_without_duplicate_documents_or_work_items(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        first = pipeline(tmp_path, mode="async").process_email(payload("async-idempotent", ["valid_remittance"]), db)
        second = pipeline(tmp_path, mode="async").process_email(payload("async-idempotent", ["valid_remittance"]), db)
        documents = list(db.scalars(select(Document)))
        work_items = list(db.scalars(select(DocumentWorkItem)))
        assert second.idempotent is True
        assert second.documents[0].document_id == first.documents[0].document_id
        assert len(documents) == 1
        assert len(work_items) == 1


def test_worker_processes_queued_async_document_to_terminal_success(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        response = pipeline(tmp_path, mode="async").process_email(payload("async-worker", ["valid_remittance"]), db)
        document_id = response.documents[0].document_id
    worker = DocumentWorker(session_factory=factory, settings=Settings(processing_mode="async", llm_provider="mock", document_storage_root=str(tmp_path / "documents")))
    assert worker.run_once() == document_id
    with factory() as db:
        document = db.get(Document, document_id)
        assert document is not None
        assert document.overall_status == "COMPLETED"
        assert document.processing_status == "COMPLETED"
        assert document.decision is not None


def test_sync_intake_still_processes_inline_and_creates_no_work_items(tmp_path) -> None:
    factory = session_factory(tmp_path)
    with factory() as db:
        response = pipeline(tmp_path, mode="sync").process_email(payload("sync-one", ["valid_remittance"]), db)
        document = db.scalar(select(Document))
        assert document is not None
        assert response.documents[0].decision is not None
        assert document.overall_status == "COMPLETED"
        assert db.scalar(select(DocumentWorkItem)) is None


def test_multi_document_intake_returns_terminal_results_for_every_document(tmp_path) -> None:
    factory = session_factory(tmp_path)
    profiles = ["valid_remittance", "valid_invoice", "payment_advice", "not_applicable"]
    with factory() as db:
        response = pipeline(tmp_path).process_email(payload("sync-many", profiles), db)
        documents = list(db.scalars(select(Document).order_by(Document.id)))
        assert len(response.documents) == len(documents) == len(profiles)
        assert all(document.overall_status == "COMPLETED" for document in documents)
        assert all(document.processing_status == "COMPLETED" for document in documents)
        assert all(document.decision is not None for document in documents)
        assert list(db.scalars(select(DocumentWorkItem))) == []
