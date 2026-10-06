from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.llm import LLMError, MockLLMProvider
from app.config import Settings
from app.db.models import Document, DocumentWorkItem, ProcessingAttempt
from app.db.session import Base
from app.domain.enums import ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.classification import LLMDocumentClassifier
from app.services.pipeline import DocumentPipeline


class FailOnceProvider:
    name = "test-fail-once"
    model = "test-v1"

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        self.remaining_failures = 1
        self.delegate = MockLLMProvider(model=self.model)

    def analyze(self, request):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise LLMError("transient classification failure", code=self.failure_code, retryable=True)
        return self.delegate.analyze(request)


class AlwaysFailProvider:
    name = "test-always-fail"
    model = "test-v1"

    def analyze(self, request):
        raise LLMError("transient classification failure", code="LLM_TIMEOUT", retryable=True)


def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'retry-accounting.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def payload(source: str) -> EmailIntakeRequest:
    return EmailIntakeRequest(
        source_email_id=source,
        received_at=datetime.now(timezone.utc),
        sender_email="retry@example.test",
        applicability=True,
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile="valid_remittance")],
    )


def run(tmp_path, provider):
    settings = Settings(llm_provider="mock", retry_max_attempts=3, document_storage_root=str(tmp_path / "documents"))
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=provider))
    db_factory = factory(tmp_path)
    with db_factory() as db:
        response = pipeline.process_email(payload(f"retry-{id(provider)}"), db)
        document = db.get(Document, response.documents[0].id)
        return response, document, db_factory


def test_retryable_failure_succeeds_on_inline_retry(tmp_path):
    response, document, db_factory = run(tmp_path, FailOnceProvider("LLM_TIMEOUT"))
    assert response.documents[0].processing_status == ProcessingStatus.COMPLETED
    assert response.documents[0].decision is not None
    assert document.retryable is False
    with db_factory() as db:
        assert db.scalar(select(DocumentWorkItem)) is None
        attempts = list(db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document.id, ProcessingAttempt.stage == ProcessingStage.CLASSIFICATION)))
        assert len(attempts) == 2


def test_retryable_failure_exhausts_inline_budget_as_terminal_failure(tmp_path):
    response, document, db_factory = run(tmp_path, AlwaysFailProvider())
    result = response.documents[0]
    assert result.processing_status == ProcessingStatus.FAILED
    assert result.overall_status == ProcessingStatus.FINAL_FAILURE
    assert document.stage_status == StageStatus.FINAL_FAILURE
    assert document.next_attempt_at is None
    assert document.retryable is False
    with db_factory() as db:
        assert db.scalar(select(DocumentWorkItem)) is None


def test_permanent_failure_is_terminal_immediately(tmp_path):
    class PermanentProvider:
        name = "test-permanent"
        model = "test-v1"
        def analyze(self, request):
            raise LLMError("provider configuration incomplete", code="LLM_AUTHENTICATION_ERROR", retryable=False)

    response, document, db_factory = run(tmp_path, PermanentProvider())
    assert response.documents[0].processing_status == ProcessingStatus.FAILED
    assert response.documents[0].overall_status == ProcessingStatus.FINAL_FAILURE
    assert document.retryable is False
    assert document.next_attempt_at is None
    with db_factory() as db:
        assert db.scalar(select(DocumentWorkItem)) is None
