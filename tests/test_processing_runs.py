from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.adapters.llm import LLMError, MockLLMProvider
from app.config import Settings
from app.db.models import Document, DocumentWorkItem, ExtractedField, ProcessingAttempt, ProcessingRun
from app.db.session import Base
from app.domain.enums import ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.classification import LLMDocumentClassifier
from app.services.pipeline import DocumentPipeline

FIXTURE_PDF = Path(__file__).parent / "fixtures" / "valid_remittance.pdf"


class FailOnceProvider:
    name = "test-fail-once"
    model = "test-v1"

    def __init__(self) -> None:
        self.remaining_failures = 1
        self.delegate = MockLLMProvider(model=self.model)

    def analyze(self, request):
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise LLMError("transient classification failure", code="LLM_TIMEOUT", retryable=True)
        return self.delegate.analyze(request)


class NeedsReviewProvider:
    name = "test-needs-review"
    model = "test-v1"

    def analyze(self, request):
        raise LLMError("classifier needs a human", code="LLM_REVIEW_REQUIRED", retryable=False, needs_review=True)


def make_settings(tmp_path, **overrides) -> Settings:
    defaults = dict(llm_provider="mock", document_storage_root=str(tmp_path / "documents"))
    defaults.update(overrides)
    return Settings(**defaults)


def factory(tmp_path, name: str):
    engine = create_engine(f"sqlite:///{tmp_path / name}.db", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def payload(source: str, *, profile: str = "valid_remittance", applicability: bool = True) -> EmailIntakeRequest:
    return EmailIntakeRequest(
        source_email_id=source,
        received_at=datetime.now(timezone.utc),
        sender_email="runs@example.test",
        applicability=applicability,
        documents=[DocumentInput(document_name="remittance.pdf", mime_type="application/pdf", mock_profile=profile)],
    )


def runs_for(db, document_id: int) -> list[ProcessingRun]:
    return list(db.scalars(select(ProcessingRun).where(ProcessingRun.document_id == document_id).order_by(ProcessingRun.id)))


def attempts_for(db, document_id: int) -> list[ProcessingAttempt]:
    return list(db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id).order_by(ProcessingAttempt.id)))


def test_sync_intake_creates_one_run_holding_every_attempt(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "runs")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-intake"), db)
        document_id = response.documents[0].id
        document = db.get(Document, document_id)
        assert document.processing_status == ProcessingStatus.COMPLETED
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert len(runs) == 1
        run = runs[0]
        assert run.trigger == "INTAKE"
        assert run.completed_at is not None
        assert run.outcome == "COMPLETED"
        assert run.extracted_text
        attempts = attempts_for(db, document_id)
        assert attempts
        assert all(attempt.run_id == run.id for attempt in attempts)
        assert all(attempt.attempt_number == index + 1 for index, attempt in enumerate(attempts))


def test_extracted_fields_are_recorded_against_the_attempt_that_produced_them(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "fields")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-fields"), db)
        document_id = response.documents[0].id
    with db_factory() as db:
        run = runs_for(db, document_id)[0]
        fields = list(db.scalars(select(ExtractedField).where(ExtractedField.document_id == document_id)))
        assert fields
        assert {"OCR", "LLM"} <= {field.source for field in fields}
        attempts = {attempt.id: attempt for attempt in attempts_for(db, document_id)}
        for field in fields:
            assert field.run_id == run.id
            assert field.attempt_id in attempts
            assert attempts[field.attempt_id].document_id == document_id
            assert field.field_value is not None
            assert field.value_type in {"string", "number", "boolean", "null", "array", "object"}
            assert field.source in {"PARSER", "OCR", "LLM"}
        llm_fields = [field for field in fields if field.source == "LLM"]
        assert all(field.confidence is not None for field in llm_fields)
        classification_attempt = next(
            attempt for attempt in attempts.values() if attempt.stage == ProcessingStage.CLASSIFICATION
        )
        assert {field.attempt_id for field in llm_fields} == {classification_attempt.id}


def test_inline_retry_opens_a_second_run_without_resetting_the_attempt_budget(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=FailOnceProvider()))
    db_factory = factory(tmp_path, "inline-retry")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-inline-retry"), db)
        document_id = response.documents[0].id
        document = db.get(Document, document_id)
        assert document.processing_status == ProcessingStatus.COMPLETED
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert [run.trigger for run in runs] == ["INTAKE", "AUTO_RETRY"]
        assert [run.outcome for run in runs] == ["RETRY_SCHEDULED", "COMPLETED"]
        classification = [
            attempt for attempt in attempts_for(db, document_id)
            if attempt.stage == ProcessingStage.CLASSIFICATION
        ]
        assert [attempt.attempt_number for attempt in classification] == [2, 4]
        assert [attempt.run_id for attempt in classification] == [runs[0].id, runs[1].id]
        document = db.get(Document, document_id)
        assert document.attempt_count >= 4


def test_async_intake_defers_the_run_until_the_worker_claims_the_item(tmp_path):
    settings = make_settings(tmp_path, processing_mode="async")
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "async-defer")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-async"), db)
        document_id = response.documents[0].id
        assert runs_for(db, document_id) == []
        item = db.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id))
        assert item is not None
        pipeline.execute_run(db.get(Document, document_id), db, trigger="INTAKE", profile="valid_remittance", worker_id="w-1", work_item_id=item.id)
        db.commit()
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert len(runs) == 1
        assert runs[0].trigger == "INTAKE"
        assert runs[0].worker_id == "w-1"
        assert runs[0].work_item_id is not None


def test_worker_attributes_runs_to_the_worker_and_the_work_item(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, processing_mode="async", retry_base_delay_seconds=0.0)
    provider = FailOnceProvider()

    def build_pipeline(s):
        return DocumentPipeline(s, classifier=LLMDocumentClassifier(s, provider=provider))

    monkeypatch.setattr("app.worker.DocumentPipeline", build_pipeline)
    db_factory = factory(tmp_path, "worker-attrib")
    with db_factory() as db:
        response = build_pipeline(settings).process_email(payload("runs-worker"), db)
        document_id = response.documents[0].id
        assert runs_for(db, document_id) == []

    from app.worker import DocumentWorker

    worker = DocumentWorker(session_factory=db_factory, worker_id="test-worker", settings=settings)
    assert worker.run_once() == document_id
    with db_factory() as db:
        document = db.get(Document, document_id)
        assert document.stage_status == StageStatus.RETRY_SCHEDULED
    assert worker.run_once() == document_id
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert [run.trigger for run in runs] == ["INTAKE", "AUTO_RETRY"]
        assert [run.outcome for run in runs] == ["RETRY_SCHEDULED", "COMPLETED"]
        assert all(run.worker_id == "test-worker" for run in runs)
        assert all(run.work_item_id is not None for run in runs)
        attempts = attempts_for(db, document_id)
        assert all(attempt.run_id is not None for attempt in attempts)
        classification = [a for a in attempts if a.stage == ProcessingStage.CLASSIFICATION]
        assert [a.attempt_number for a in classification] == [2, 4]
        assert [a.run_id for a in classification] == [runs[0].id, runs[1].id]


def test_manual_retry_and_reprocess_use_distinct_triggers(tmp_path):
    settings = make_settings(tmp_path, processing_mode="async")
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=FailOnceProvider()))
    db_factory = factory(tmp_path, "manual-triggers")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-manual"), db)
        document_id = response.documents[0].id
        document = db.get(Document, document_id)
        pipeline.execute_run(document, db, trigger="INTAKE", profile="valid_remittance")
        db.commit()
        assert document.stage_status == StageStatus.RETRY_SCHEDULED
        assert pipeline.retry_allowed(document)
        pipeline.retry_document(document_id, db)
        document = db.get(Document, document_id)
        assert document.processing_status == ProcessingStatus.COMPLETED
        stored = pipeline.storage.store(FIXTURE_PDF.read_bytes())
        document.storage_reference = stored.storage_reference
        document.content_hash = stored.content_hash
        db.flush()
        pipeline.reprocess_document(document_id, db)
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert [run.trigger for run in runs] == ["INTAKE", "MANUAL_RETRY", "REPROCESS"]
        assert all(run.completed_at is not None for run in runs)
        classification = [
            attempt for attempt in attempts_for(db, document_id)
            if attempt.stage == ProcessingStage.CLASSIFICATION
        ]
        assert len(classification) == 3
        numbers = [attempt.attempt_number for attempt in classification]
        assert numbers == sorted(numbers)
        assert len(set(numbers)) == 3
        assert [attempt.run_id for attempt in classification] == [run.id for run in runs]
        document = db.get(Document, document_id)
        assert document.attempt_count == len(attempts_for(db, document_id))


def test_crm_action_is_its_own_run(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings)
    db_factory = factory(tmp_path, "crm-run")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-crm"), db)
        document_id = response.documents[0].id
        pipeline.process_document_action(document_id, db)
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert [run.trigger for run in runs] == ["INTAKE", "CRM_ACTION"]
        assert runs[1].outcome == "COMPLETED"
        crm_attempt = next(
            attempt for attempt in attempts_for(db, document_id)
            if attempt.stage == ProcessingStage.CRM_ACTION
        )
        assert crm_attempt.run_id == runs[1].id


def test_a_new_run_does_not_reset_the_automatic_retry_budget(tmp_path):
    class AlwaysFailProvider:
        name = "test-always-fail"
        model = "test-v1"

        def analyze(self, request):
            raise LLMError("transient classification failure", code="LLM_TIMEOUT", retryable=True)

    settings = make_settings(tmp_path, processing_mode="async", retry_max_attempts=3, retry_base_delay_seconds=0.0)
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=AlwaysFailProvider()))
    db_factory = factory(tmp_path, "budget-spans-runs")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-budget"), db)
        document_id = response.documents[0].id
        document = db.get(Document, document_id)
        for _ in range(4):
            pipeline.execute_run(document, db, trigger="AUTO_RETRY", profile="valid_remittance")
            db.commit()
        assert document.processing_status == ProcessingStatus.FAILED
        assert document.stage_status == StageStatus.FINAL_FAILURE
        assert document.retryable is False
    with db_factory() as db:
        runs = runs_for(db, document_id)
        assert [run.trigger for run in runs] == ["AUTO_RETRY"] * 4
        assert [run.outcome for run in runs] == ["RETRY_SCHEDULED", "RETRY_SCHEDULED", "FAILED", "FAILED"]
        classification = [a for a in attempts_for(db, document_id) if a.stage == ProcessingStage.CLASSIFICATION]
        assert [a.attempt_number for a in classification] == [2, 4, 6, 8]
        assert len({a.run_id for a in classification}) == 4


def test_needs_review_run_keeps_the_extracted_text_and_the_ocr_score(tmp_path):
    settings = make_settings(tmp_path)
    pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=NeedsReviewProvider()))
    db_factory = factory(tmp_path, "needs-review-run")
    with db_factory() as db:
        response = pipeline.process_email(payload("runs-review"), db)
        document_id = response.documents[0].id
        document = db.get(Document, document_id)
        assert document.processing_status == ProcessingStatus.COMPLETED
        assert document.decision == "NEEDS_REVIEW"
        assert document.ocr_score is not None
    with db_factory() as db:
        run = runs_for(db, document_id)[0]
        assert run.outcome == "NEEDS_REVIEW"
        assert run.completed_at is not None
        assert run.extracted_text
