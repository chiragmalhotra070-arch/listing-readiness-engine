from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.adapters.crm import CRMActionAdapter
from app.adapters.customer import CustomerLookupAdapter
from app.adapters.ocr import ExtractionResult, OCRExtractionAdapter
from app.adapters.llm import LLMError
from app.adapters.document_parser import DocumentFormatError, DocumentParser, extract_business_fields
from app.config import Settings
from app.db.models import AuditEvent, Customer, Document, Email, ExtractedField, ProcessingAttempt, ProcessingRun, SideEffectOperation
from app.domain.enums import BusinessOutcome, FailureType, ProcessingStage, ProcessingStatus, StageStatus
from app.schemas import AuditEventResponse, DocumentResult, EmailIntakeRequest, EmailIntakeResponse, ProcessingAttemptResponse
from app.services.audit import record_audit
from app.services.business_identity import claim_business_identity
from app.services.classification import DocumentClassifier, LLMDocumentClassifier
from app.services.decision_engine import DecisionInput, decide
from app.services.document_lifecycle import advance_stage, begin_stage, complete, fail_permanently, route_to_review, schedule_retry
from app.services.duplicate_detection import duplicate_key
from app.services.retry import RetryPolicy, classify_error
from app.services.validation import validate_required_fields
from app.services.storage import LocalDocumentStorage, StorageError
from app.services.text_sanitization import NulByteRejectedError, ensure_jsonb_value, ensure_structured_text, sanitize_persisted_text
from app.services.work_queue import enqueue_document

RUN_TRIGGERS = {
    "INTAKE", "AUTO_RETRY", "MANUAL_RETRY", "REPROCESS", "CRM_ACTION", "LEGACY", "UNKNOWN",
}


def value_type_of(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, (list, tuple)):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


class DocumentPipeline:
    def __init__(self, settings: Settings, *, extractor: OCRExtractionAdapter | None = None, classifier: DocumentClassifier | None = None, customer_lookup: CustomerLookupAdapter | None = None, crm: CRMActionAdapter | None = None) -> None:
        self.settings = settings
        self.retry_policy = RetryPolicy(settings)
        self.extractor = extractor or OCRExtractionAdapter(
            [name.strip() for name in settings.ocr_provider_order.split(",") if name.strip()],
            mock_provider_order=[name.strip() for name in settings.ocr_mock_provider_order.split(",") if name.strip()],
            storage=LocalDocumentStorage(settings.document_storage_root),
            tesseract_options={
                "tesseract_command": settings.ocr_tesseract_command,
                "pdftoppm_command": settings.ocr_pdftoppm_command,
                "language": settings.ocr_language,
                "timeout_seconds": settings.ocr_timeout_seconds,
                "max_pages": settings.ocr_max_pages,
                "render_dpi": settings.ocr_render_dpi,
                "max_rendered_image_bytes": settings.ocr_max_rendered_image_bytes,
                "temp_root": settings.ocr_temp_root,
            },
        )
        self.classifier = classifier or LLMDocumentClassifier(settings)
        self.customer_lookup = customer_lookup or CustomerLookupAdapter()
        self.crm = crm or CRMActionAdapter()
        self.storage = LocalDocumentStorage(settings.document_storage_root)
        self.document_parser = DocumentParser()
        self._run_stack: list[ProcessingRun] = []

    # ------------------------------------------------------------------
    # Run lifecycle (Q3/Q4/Q5): one ProcessingRun per pipeline execution.
    # ------------------------------------------------------------------
    def _begin_run(self, db: Session, document: Document, *, trigger: str, worker_id: str | None = None, work_item_id: int | None = None) -> ProcessingRun:
        if trigger not in RUN_TRIGGERS:
            raise ValueError(f"unknown run trigger: {trigger}")
        run = ProcessingRun(document_id=document.id, trigger=trigger, started_at=datetime.now(timezone.utc), worker_id=worker_id, work_item_id=work_item_id)
        db.add(run)
        db.flush()
        self._run_stack.append(run)
        return run

    def _end_run(self, db: Session, document: Document, run: ProcessingRun) -> None:
        while self._run_stack and self._run_stack[-1].id == run.id:
            self._run_stack.pop()
        if run.completed_at is None:
            run.completed_at = datetime.now(timezone.utc)
        if run.outcome is None:
            run.outcome = self._run_outcome(document)
        if run.extracted_text is None:
            run.extracted_text = sanitize_persisted_text(document.extracted_text)
        db.flush()

    @staticmethod
    def _run_outcome(document: Document) -> str:
        if document.stage_status == StageStatus.NEEDS_REVIEW or document.decision == BusinessOutcome.NEEDS_REVIEW:
            return "NEEDS_REVIEW"
        if document.processing_status == ProcessingStatus.PENDING_RETRY:
            return "RETRY_SCHEDULED"
        if document.processing_status == ProcessingStatus.FAILED:
            return "FAILED"
        if document.processing_status == ProcessingStatus.COMPLETED:
            return "COMPLETED"
        return "INTERRUPTED"

    def execute_run(
        self,
        document: Document,
        db: Session,
        *,
        trigger: str,
        profile: str | None = None,
        email_applicable: bool | None = None,
        worker_id: str | None = None,
        work_item_id: int | None = None,
    ) -> ProcessingRun:
        """Execute the pipeline funnel once and account for it as one run."""
        run = self._begin_run(db, document, trigger=trigger, worker_id=worker_id, work_item_id=work_item_id)
        try:
            self._run_from_current_stage(document, profile, email_applicable, db)
        except NulByteRejectedError as error:
            # A NUL byte inside JSONB would make PostgreSQL's jsonb parser fail
            # with 22P05 and abort the transaction, so the funnel refuses the
            # value first (validate-before-assign at every JSONB ingress) and the
            # typed rejection is translated here instead of escaping as a raw
            # database error.  Malformed structured extraction data is a
            # data-quality problem, so it goes through the existing NEEDS_REVIEW
            # pathway: the column name is kept as the reason, the offending
            # payload is never echoed, and no new outcome/status is invented.
            route_to_review(
                db,
                document,
                stage=document.current_stage,
                reason=str(error),
                details={"failure_code": "NUL_BYTE_REJECTED", "column": error.column_label},
            )
        finally:
            self._end_run(db, document, run)
        return run

    def _current_run(self) -> ProcessingRun | None:
        return self._run_stack[-1] if self._run_stack else None

    def _record_extracted_fields(self, db: Session, *, document_id: int, attempt: ProcessingAttempt | None, fields: dict[str, Any] | None, source: str, confidence: float | None = None) -> None:
        if attempt is None or not fields:
            return
        run = self._current_run()
        if run is None:
            return
        for name, value in fields.items():
            if not name:
                continue
            # ``extracted_fields.field_value`` is JSONB and the value comes from
            # a parser or model: reject a NUL-bearing observation before the row
            # exists so jsonb input is never reached.
            ensure_jsonb_value(value, "extracted_fields.field_value")
            db.add(
                ExtractedField(
                    attempt_id=attempt.id,
                    document_id=document_id,
                    run_id=run.id,
                    field_name=str(name)[:128],
                    field_value=value,
                    value_type=value_type_of(value),
                    source=source,
                    confidence=confidence,
                )
            )
        db.flush()

    def process_email(self, payload: EmailIntakeRequest, db: Session) -> EmailIntakeResponse:
        # The idempotency lookup sends this value as a query parameter, which the
        # before_flush hook cannot see.  source_email_id is an identifier, so a
        # NUL byte is rejected here rather than stripped: rewriting it would let
        # two distinct messages collapse onto a single idempotent match.
        source_email_id = ensure_structured_text(payload.source_email_id, "emails.source_email_id")
        existing = db.scalar(select(Email).where(Email.source_email_id == source_email_id))
        if existing:
            return self._response(existing, db, idempotent=True)
        email = Email(source_email_id=source_email_id, received_at=payload.received_at, sender_email=payload.sender_email, sender_name=payload.sender_name, sender_company=payload.sender_company, subject=payload.subject, body=payload.body, applicability=payload.applicability, correlation_id=payload.correlation_id, n8n_execution_id=payload.n8n_execution_id, status="RECEIVED")
        db.add(email)
        try:
            with db.begin_nested():
                db.flush()
        except IntegrityError:
            existing = db.scalar(select(Email).where(Email.source_email_id == source_email_id))
            if existing is None:
                raise
            return self._response(existing, db, idempotent=True)
        record_audit(db, email_id=email.id, event_type="PROCESSING_STARTED", status="SUCCESS", message="email accepted")
        for item in payload.documents:
            # ``mock_profile`` arrives straight from the request and is stored in
            # the JSONB ``documents.evidence`` column, so a NUL byte is refused
            # here - before any row exists - instead of reaching jsonb input.
            evidence = {"mock_profile": item.mock_profile}
            ensure_jsonb_value(evidence, "documents.evidence")
            document = Document(email_id=email.id, document_name=item.document_name, mime_type=item.mime_type, storage_reference=item.storage_reference, content_hash=item.content_hash, source_attachment_id=item.source_attachment_id, file_size_bytes=item.file_size_bytes, processing_status=ProcessingStatus.PENDING, current_stage=ProcessingStage.DOCUMENT_PARSING, stage_status=StageStatus.PENDING, overall_status=ProcessingStatus.PENDING, duplicate=False, document_type="UNKNOWN", evidence=evidence)
            db.add(document)
            db.flush()
            record_audit(db, email_id=email.id, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="email ingestion completed", details={"stage": ProcessingStage.EMAIL_INGESTION})
            if self.settings.processing_mode.strip().lower() == "async":
                enqueue_document(db, document.id)
            else:
                self._run_synchronously(document, item.mock_profile, payload.applicability, db)
        if self.settings.processing_mode.strip().lower() != "async":
            email.status = "COMPLETED_WITH_FAILURE" if any(document.overall_status == ProcessingStatus.FINAL_FAILURE for document in email.documents) else "COMPLETED"
        db.commit()
        return self._response(email, db, idempotent=False)

    def _run_synchronously(self, document: Document, profile: str | None, email_applicable: bool | None, db: Session) -> None:
        attempts = 0
        while True:
            self.execute_run(document, db, trigger="INTAKE" if attempts == 0 else "AUTO_RETRY", profile=profile, email_applicable=email_applicable)
            attempts += 1
            if document.processing_status != ProcessingStatus.PENDING_RETRY or not document.retryable:
                return
            if attempts >= self.retry_policy.max_attempts:
                document.processing_status = ProcessingStatus.FAILED
                document.overall_status = ProcessingStatus.FINAL_FAILURE
                document.stage_status = StageStatus.FINAL_FAILURE
                document.next_attempt_at = None
                document.retryable = False
                document.last_error = document.last_error or "retry budget exhausted"
                return
            advance_stage(document)

    def retry_document(self, document_id: int, db: Session) -> DocumentResult:
        document = db.get(Document, document_id)
        if document is None:
            raise LookupError("document not found")
        if not self.retry_allowed(document):
            raise ValueError("document is not eligible for retry")
        profile = (document.evidence or {}).get("mock_profile")
        if document.current_stage == ProcessingStage.CRM_ACTION:
            result = self.process_document_action(document_id, db)
            return result
        advance_stage(document)
        record_audit(db, document_id=document.id, event_type="RETRY_STARTED", status=StageStatus.IN_PROGRESS, message="manual retry started", details={"stage": document.current_stage})
        self.execute_run(document, db, trigger="MANUAL_RETRY", profile=str(profile) if profile else None)
        db.commit()
        return self._document_result(document, db)

    def reprocess_document(self, document_id: int, db: Session) -> DocumentResult:
        document = db.get(Document, document_id)
        if document is None:
            raise LookupError("document not found")
        if not document.storage_reference:
            raise ValueError("document has no durable storage reference")
        try:
            content = self.storage.read(document.storage_reference)
        except StorageError as error:
            raise ValueError(str(error)) from error
        import hashlib
        if document.content_hash and hashlib.sha256(content).hexdigest() != document.content_hash:
            raise ValueError("stored document content hash does not match metadata")
        begin_stage(document, stage=ProcessingStage.OCR)
        record_audit(db, document_id=document.id, event_type="PROCESSING_STARTED", status=StageStatus.IN_PROGRESS, message="document reprocessing started", details={"storage_reference": document.storage_reference})
        self.execute_run(document, db, trigger="REPROCESS", profile=(document.evidence or {}).get("mock_profile"))
        db.commit()
        return self._document_result(document, db)

    def retry_allowed(self, document: Document) -> bool:
        return document.stage_status in {StageStatus.RETRY_SCHEDULED} and bool(document.retryable)

    def process_document_action(self, document_id: int, db: Session) -> DocumentResult:
        document = db.get(Document, document_id)
        if document is None:
            raise LookupError("document not found")
        operation_key = f"document:{document.id}:CRM_ACTION"
        operation = db.scalar(select(SideEffectOperation).where(SideEffectOperation.idempotency_key == operation_key))
        if operation and operation.status == "SUCCESS":
            return self._document_result(document, db)
        if document.decision not in {BusinessOutcome.READY_FOR_PROCESSING, BusinessOutcome.FAILED} and document.current_stage != ProcessingStage.CRM_ACTION:
            return self._document_result(document, db)
        if operation is None:
            operation = SideEffectOperation(document_id=document.id, action_type="CRM_ACTION", idempotency_key=operation_key, status="PENDING")
            db.add(operation)
            db.flush()
        attempt_number = self._stage_attempt_count(document.id, ProcessingStage.CRM_ACTION, db) + 1
        document.current_stage = ProcessingStage.CRM_ACTION
        document.stage_status = StageStatus.IN_PROGRESS
        document.overall_status = ProcessingStatus.IN_PROGRESS
        record_audit(db, document_id=document.id, event_type="STAGE_STARTED", status=StageStatus.IN_PROGRESS, message="CRM action started", details={"idempotency_key": operation_key})
        run = self._begin_run(db, document, trigger="CRM_ACTION")
        try:
            result = self.crm.process(document_id=document.id, mock_profile=(document.evidence or {}).get("mock_profile"), attempt_number=attempt_number, idempotency_key=operation_key)
            if result.success:
                operation.status = "SUCCESS"
                operation.external_reference = result.external_reference
                operation.response = {"message": result.message}
                document.decision = BusinessOutcome.PROCESSED
                document.decision_reason = result.message
                document.current_stage = ProcessingStage.COMPLETE
                document.stage_status = StageStatus.SUCCESS
                document.overall_status = ProcessingStatus.COMPLETED
                document.processing_status = ProcessingStatus.COMPLETED
                document.retryable = False
                self._attempt(db, document.id, ProcessingStage.CRM_ACTION, "mock-crm", StageStatus.SUCCESS, result={"message": result.message}, idempotency_key=operation_key)
                record_audit(db, document_id=document.id, event_type="PROCESSING_COMPLETED", status=StageStatus.SUCCESS, message=result.message)
            else:
                failure_type, retryable = classify_error(result.code or "UNKNOWN")
                operation.status = "FAILED"
                operation.response = {"message": result.message, "code": result.code}
                decision = self.retry_policy.schedule(attempt_number) if retryable else None
                exhausted = not retryable or decision.exhausted
                document.decision = BusinessOutcome.FAILED
                document.decision_reason = result.message
                document.last_error = result.message
                document.retryable = retryable
                document.processing_status = ProcessingStatus.FAILED if exhausted else ProcessingStatus.PENDING_RETRY
                document.overall_status = ProcessingStatus.FINAL_FAILURE if exhausted else ProcessingStatus.PENDING_RETRY
                document.stage_status = StageStatus.FINAL_FAILURE if exhausted else StageStatus.RETRY_SCHEDULED
                document.next_attempt_at = decision.next_attempt_at if decision and not exhausted else None
                self._attempt(db, document.id, ProcessingStage.CRM_ACTION, "mock-crm", StageStatus.PERMANENT_FAILURE if not retryable else (StageStatus.FINAL_FAILURE if exhausted else StageStatus.RETRYABLE_FAILURE), failure_code=result.code, failure_message=result.message, failure_type=failure_type, retryable=retryable, next_attempt_at=document.next_attempt_at, idempotency_key=operation_key)
                record_audit(db, document_id=document.id, event_type="FINAL_FAILURE" if exhausted else "RETRY_SCHEDULED", status=document.stage_status, message=result.message, details={"retryable": retryable, "next_attempt_at": document.next_attempt_at.isoformat() if document.next_attempt_at else None})
        finally:
            self._end_run(db, document, run)
        db.commit()
        return self._document_result(document, db)

    def _run_from_current_stage(self, document: Document, profile: str | None, email_applicable: bool | None, db: Session) -> None:
        parser_result = None
        if document.storage_reference and not profile:
            try:
                path = self.storage.resolve(document.storage_reference)
                parser_result = self.document_parser.parse(path, mime_type=document.mime_type, document_name=document.document_name)
            except DocumentFormatError as error:
                document.next_attempt_at = None
                document.decision = BusinessOutcome.FAILED
                document.decision_reason = str(error)
                document.last_error = str(error)
                document.retryable = False
                document.processing_status = ProcessingStatus.FAILED
                document.overall_status = ProcessingStatus.FINAL_FAILURE
                document.current_stage = ProcessingStage.DOCUMENT_PARSING
                document.stage_status = StageStatus.PERMANENT_FAILURE
                self._attempt(db, document.id, ProcessingStage.DOCUMENT_PARSING, error.code, StageStatus.PERMANENT_FAILURE, failure_code=error.code, failure_message=str(error), failure_type=FailureType.PERMANENT, retryable=False)
                record_audit(db, document_id=document.id, event_type="FINAL_FAILURE", status=StageStatus.PERMANENT_FAILURE, message=str(error), details={"stage": ProcessingStage.DOCUMENT_PARSING, "provider": "format-parser"})
                return
            except StorageError as error:
                self._schedule_or_fail(document, ProcessingStage.ATTACHMENT_RETRIEVAL, str(error), True, db)
                return

        if parser_result is not None and parser_result.metadata.get("text_sufficient"):
            extraction = ExtractionResult(parser_result.extracted_text, parser_result.extracted_data, 0.96)
            provider_errors = []
            provider_results = [(parser_result.provider, extraction)]
            parse_attempt = self._attempt(db, document.id, ProcessingStage.DOCUMENT_PARSING, parser_result.provider, StageStatus.SUCCESS, result=parser_result.metadata)
            record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="document parsing completed", details={"stage": ProcessingStage.DOCUMENT_PARSING, "provider": parser_result.provider, **parser_result.metadata})
            merged_evidence = {**(document.evidence or {}), "format_extraction": {"provider": parser_result.provider, "metadata": parser_result.metadata, "warnings": parser_result.warnings}}
            ensure_jsonb_value(merged_evidence, "documents.evidence")
            document.evidence = merged_evidence
        elif parser_result is not None:
            extraction, provider_errors, provider_results = self.extractor.extract_with_failover(document_name=document.document_name, mock_profile=profile, storage_reference=document.storage_reference, mime_type=document.mime_type, page_count=parser_result.metadata.get("page_count"))
            parse_attempt = self._attempt(db, document.id, ProcessingStage.DOCUMENT_PARSING, parser_result.provider, StageStatus.SUCCESS, result=parser_result.metadata)
            record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="document parsing completed; OCR fallback required", details={"stage": ProcessingStage.DOCUMENT_PARSING, "provider": parser_result.provider, **parser_result.metadata})
            merged_evidence = {**(document.evidence or {}), "format_extraction": {"provider": parser_result.provider, "metadata": parser_result.metadata, "warnings": parser_result.warnings, "ocr_needed": True}}
            ensure_jsonb_value(merged_evidence, "documents.evidence")
            document.evidence = merged_evidence
        else:
            parse_attempt = None
            extraction, provider_errors, provider_results = self.extractor.extract_with_failover(document_name=document.document_name, mock_profile=profile, storage_reference=document.storage_reference, mime_type=document.mime_type)
        if parser_result is not None and parser_result.extracted_data:
            self._record_extracted_fields(db, document_id=document.id, attempt=parse_attempt, fields=parser_result.extracted_data, source="PARSER")
        for provider, error in provider_errors:
            self._attempt(db, document.id, ProcessingStage.OCR, provider, StageStatus.RETRYABLE_FAILURE if error.retryable else StageStatus.PERMANENT_FAILURE, failure_code=error.code, failure_message=str(error), failure_type=FailureType.RETRYABLE if error.retryable else FailureType.PERMANENT, retryable=error.retryable)
            record_audit(db, document_id=document.id, event_type="STAGE_FAILED", status=StageStatus.RETRYABLE_FAILURE if error.retryable else StageStatus.PERMANENT_FAILURE, message=str(error), details={"stage": ProcessingStage.OCR, "provider": provider, "failure_code": error.code})
            if provider != self.extractor.provider_order[-1]:
                record_audit(db, document_id=document.id, event_type="PROVIDER_FALLBACK", status=StageStatus.IN_PROGRESS, message="falling back to next OCR provider", details={"from_provider": provider})
        if extraction is None:
            all_retryable = bool(provider_errors) and all(error.retryable for _, error in provider_errors)
            self._schedule_or_fail(document, ProcessingStage.OCR, "all OCR providers failed", all_retryable, db)
            return
        if extraction.text and not extraction.extracted_data:
            extraction = replace(extraction, extracted_data=extract_business_fields(extraction.text))
        ocr_attempt: ProcessingAttempt | None = None
        for provider, result in provider_results:
            ocr_attempt = self._attempt(db, document.id, ProcessingStage.OCR, provider, StageStatus.SUCCESS, quality_score=result.score, result={"fallback_used": result.fallback_used, "provider": result.provider, "warnings": result.warnings, "pages_processed": result.pages_processed, "provider_metadata": result.provider_metadata})
        # Preserve the full text for this run before any early-return gate (Q4),
        # and keep the OCR score even when the run is routed to review.
        run = self._current_run()
        if run is not None and run.extracted_text is None:
            run.extracted_text = sanitize_persisted_text(extraction.text)
        document.ocr_score = extraction.score
        ocr_ran = parser_result is None or not parser_result.metadata.get("text_sufficient")
        if ocr_ran:
            self._record_extracted_fields(db, document_id=document.id, attempt=ocr_attempt, fields=extraction.extracted_data, source="OCR", confidence=extraction.score)
        if extraction.score < self.settings.ocr_fallback_min_score or (extraction.score < self.settings.ocr_acceptable_score and len(provider_results) > 1):
            route_to_review(db, document, stage=ProcessingStage.OCR, reason="OCR quality remains below review threshold", details={"quality_scores": [result.score for _, result in provider_results], "providers": [result.provider or provider for provider, result in provider_results]})
            return
        extraction_evidence = {"fallback_used": extraction.fallback_used, "quality_score": extraction.score, "provider": extraction.provider, "warnings": extraction.warnings, "pages_processed": extraction.pages_processed, "provider_metadata": extraction.provider_metadata}
        merged_evidence = {**(document.evidence or {}), "extraction": extraction_evidence}
        ensure_jsonb_value(extraction.extracted_data, "documents.extracted_data")
        ensure_jsonb_value(merged_evidence, "documents.evidence")
        document.extracted_text = sanitize_persisted_text(extraction.text)
        document.extracted_data = extraction.extracted_data
        document.evidence = merged_evidence
        document.current_stage = ProcessingStage.CLASSIFICATION
        document.stage_status = StageStatus.SUCCESS
        document.processing_status = ProcessingStatus.IN_PROGRESS
        record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="OCR completed", details={"quality_score": extraction.score})

        try:
            classification = self.classifier.classify(document_id=document.id, document_name=document.document_name, mime_type=document.mime_type, extracted_text=document.extracted_text or "", extracted_data=document.extracted_data or {}, parser_metadata=(parser_result.metadata if parser_result else {}), mock_profile=profile)
        except LLMError as error:
            if error.needs_review:
                route_to_review(db, document, stage=ProcessingStage.CLASSIFICATION, reason=str(error), details={"failure_code": error.code, "provider_metadata": error.metadata})
            else:
                self._schedule_or_fail(document, ProcessingStage.CLASSIFICATION, str(error), error.retryable, db)
            self._attempt(db, document.id, ProcessingStage.CLASSIFICATION, "llm", StageStatus.NEEDS_REVIEW if error.needs_review else (StageStatus.RETRYABLE_FAILURE if error.retryable else StageStatus.PERMANENT_FAILURE), failure_code=error.code, failure_message=str(error), failure_type=None if error.needs_review else (FailureType.RETRYABLE if error.retryable else FailureType.PERMANENT), retryable=error.retryable, result=error.metadata)
            record_audit(db, document_id=document.id, event_type="NEEDS_REVIEW" if error.needs_review else "STAGE_FAILED", status=StageStatus.NEEDS_REVIEW if error.needs_review else (StageStatus.RETRYABLE_FAILURE if error.retryable else StageStatus.PERMANENT_FAILURE), message=str(error), details={"stage": ProcessingStage.CLASSIFICATION, "failure_code": error.code, "retryable": error.retryable, "provider_metadata": error.metadata})
            return
        document_fields = {key: value for key, value in (document.extracted_data or {}).items() if key not in {"customer_id", "sender_customer_id"}}
        merged_extracted_data = {**document_fields, **classification.extracted_data}
        classification_result = {"provider": classification.provider, "model": classification.model, "prompt_version": classification.prompt_version, "schema_version": classification.schema_version, "provider_request_id": classification.provider_request_id, "model_confidence": classification.confidence, "normalized_confidence": classification.normalized_confidence, "evidence": classification.evidence, "assessment": classification.assessment, "ambiguities": classification.ambiguities, "warnings": classification.warnings, "raw_metadata": classification.raw_metadata, "extracted_fields": classification.extracted_data, "supplemental_information": classification.supplemental_information, "customer_candidates": classification.customer_candidates.model_dump()}
        # Validate both JSONB writes - and the merged result - before mutating
        # either, so a rejection leaves the document exactly as it was.
        merged_evidence = {**(document.evidence or {}), "classification": classification_result}
        ensure_jsonb_value(merged_extracted_data, "documents.extracted_data")
        ensure_jsonb_value(merged_evidence, "documents.evidence")
        document.extracted_data = merged_extracted_data
        document.document_type = classification.document_type
        document.classification_confidence = classification.confidence
        document.evidence = merged_evidence
        classification_attempt = self._attempt(db, document.id, ProcessingStage.CLASSIFICATION, classification.provider, StageStatus.SUCCESS, quality_score=classification.normalized_confidence, result=classification_result)
        self._record_extracted_fields(db, document_id=document.id, attempt=classification_attempt, fields=classification.extracted_data, source="LLM", confidence=classification.normalized_confidence)
        record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="classification completed", details={"provider": classification.provider, "model": classification.model, "prompt_version": classification.prompt_version, "schema_version": classification.schema_version, "model_confidence": classification.confidence, "normalized_confidence": classification.normalized_confidence, "ambiguities": classification.ambiguities, "warnings": classification.warnings})
        if not classification.applicable:
            document.decision = BusinessOutcome.NOT_APPLICABLE
            document.decision_reason = "document is not applicable"
            self._finish_business(document, ProcessingStage.APPLICABILITY, db)
            return

        match = self.customer_lookup.lookup(db=db, extracted_data=document.extracted_data or {}, customer_candidates=classification.customer_candidates)
        contradiction = match.customer_type == "NEEDS_REVIEW"
        customer = None
        if match.customer_id and not contradiction:
            customer = db.scalar(select(Customer).where(Customer.customer_id == match.customer_id))
            if customer is None:
                customer = Customer(customer_id=match.customer_id, customer_name=match.customer_name or match.customer_id)
                db.add(customer)
                db.flush()
            document.customer_id = customer.id
        customer_needs_review = contradiction
        self._attempt(db, document.id, ProcessingStage.CUSTOMER_RESOLUTION, "database-customer-lookup", StageStatus.SUCCESS, result={"customer_id": match.customer_id, "strong_ids": sorted(match.strong_ids), "customer_type": match.customer_type, "reason": match.reason})
        record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="customer resolution completed", details={"customer_id": match.customer_id, "customer_type": match.customer_type, "reason": match.reason})

        validation = validate_required_fields(classification.document_type, document.extracted_data or {})
        key = duplicate_key(classification.document_type, match.customer_id, document.extracted_data)
        ownership = claim_business_identity(db, document=document, document_type=classification.document_type, customer=customer, extracted_data=document.extracted_data) if customer is not None and not customer_needs_review else None
        document.duplicate = ownership.is_duplicate if ownership else False
        document.duplicate_of_document_id = ownership.owner_document_id if ownership and ownership.is_duplicate else None
        duplicate_determinate = (key.value is not None and not customer_needs_review) or match.customer_type == "NEW_CUSTOMER"
        self._attempt(db, document.id, ProcessingStage.DUPLICATE_DETECTION, "local-database", StageStatus.SUCCESS, result={"key": key.value, "duplicate": document.duplicate, "ownership": ownership.outcome if ownership else "NOT_APPLICABLE", "owner_document_id": ownership.owner_document_id if ownership else None})
        self._attempt(db, document.id, ProcessingStage.FIELD_VALIDATION, "rule-engine", StageStatus.SUCCESS, result={"missing_fields": validation.missing_fields})
        record_audit(db, document_id=document.id, event_type="STAGE_SUCCEEDED", status=StageStatus.SUCCESS, message="duplicate detection and field validation completed", details={"missing_fields": validation.missing_fields, "duplicate": document.duplicate, "ownership": ownership.outcome if ownership else "NOT_APPLICABLE", "owner_document_id": ownership.owner_document_id if ownership else None})
        decision = decide(DecisionInput(applicable=True, document_type=classification.document_type, ocr_score=document.ocr_score or 0, fallback_used=any(result.fallback_used for _, result in provider_results), duplicate=document.duplicate, duplicate_determinate=duplicate_determinate, customer_needs_review=customer_needs_review, contradiction=contradiction, required_fields_valid=validation.valid), acceptable_ocr_score=self.settings.ocr_acceptable_score, fallback_min_score=self.settings.ocr_fallback_min_score)
        merged_evidence = {**(document.evidence or {}), "customer_resolution": {"customer_type": match.customer_type, "customer_id": match.customer_id, "reason": match.reason}, "ocr": {"providers": [provider for provider, _ in provider_results], "quality_scores": [result.score for _, result in provider_results]}, "decision": {"outcome": decision.outcome, "reason": decision.reason, "duplicate_key": key.value, "missing_fields": validation.missing_fields}}
        ensure_jsonb_value(merged_evidence, "documents.evidence")
        document.decision = decision.outcome
        document.decision_reason = decision.reason
        document.evidence = merged_evidence
        if decision.outcome == BusinessOutcome.NEEDS_REVIEW:
            route_to_review(db, document, stage=ProcessingStage.BUSINESS_DECISION, reason=decision.reason, details=document.evidence["decision"])
        else:
            self._finish_business(document, ProcessingStage.BUSINESS_DECISION, db)

    def _schedule_or_fail(self, document: Document, stage: ProcessingStage, reason: str, retryable: bool, db: Session) -> None:
        policy = self._retry_decision_for_stage(document.id, stage, retryable, db)
        if policy is None or policy.exhausted:
            fail_permanently(db, document, stage=stage, reason=reason)
        else:
            schedule_retry(db, document, stage=stage, reason=reason, next_attempt_at=policy.next_attempt_at)

    def _retry_decision_for_stage(self, document_id: int, stage: ProcessingStage, retryable: bool, db: Session):
        attempt_number = self._stage_attempt_count(document_id, stage, db) + 1
        return self.retry_policy.schedule(attempt_number) if retryable else None

    def _finish_business(self, document: Document, stage: ProcessingStage, db: Session) -> None:
        complete(document, stage=stage)
        self._attempt(db, document.id, stage, "rule-engine", StageStatus.SUCCESS, result={"decision": document.decision})
        record_audit(db, document_id=document.id, event_type="DECISION_MADE", status=StageStatus.SUCCESS, message=document.decision_reason or "decision made", details={"decision": document.decision})

    def _attempt(self, db: Session, document_id: int, stage: ProcessingStage, provider: str, status: StageStatus, *, failure_code: str | None = None, failure_message: str | None = None, failure_type: FailureType | None = None, retryable: bool = False, next_attempt_at: datetime | None = None, quality_score: float | None = None, result: dict[str, Any] | None = None, idempotency_key: str | None = None) -> ProcessingAttempt:
        document = db.get(Document, document_id)
        document.attempt_count += 1
        run = self._current_run()
        if run is None:
            # Safety net: never emit an attempt that is not attributed to a run.
            run = ProcessingRun(document_id=document_id, trigger="UNKNOWN", started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc), outcome="UNKNOWN")
            db.add(run)
            db.flush()
        attempt = ProcessingAttempt(document_id=document_id, run_id=run.id, attempt_number=document.attempt_count, stage=stage, provider=provider, started_at=datetime.now(timezone.utc), completed_at=datetime.now(timezone.utc), status=status, failure_code=failure_code, failure_message=failure_message, failure_type=failure_type, retryable=retryable, next_attempt_at=next_attempt_at, quality_score=quality_score, result=result, idempotency_key=idempotency_key)
        db.add(attempt)
        db.flush()
        return attempt

    def _stage_attempt_count(self, document_id: int, stage: ProcessingStage, db: Session) -> int:
        return len(list(db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id, ProcessingAttempt.stage == stage))))

    def _document_result(self, document: Document, db: Session) -> DocumentResult:
        latest = db.scalar(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document.id).order_by(ProcessingAttempt.id.desc()))
        extracted_data = document.extracted_data or {}
        business_reference = next((extracted_data.get(field) for field in ("invoice_number", "remittance_number", "payment_reference", "note_number", "account_number") if extracted_data.get(field)), None)
        customer = db.get(Customer, document.customer_id) if document.customer_id else None
        return DocumentResult(id=document.id, document_name=document.document_name, document_id=document.id, source_email_id=document.email.source_email_id if document.email else None, correlation_id=document.email.correlation_id if document.email else None, n8n_execution_id=document.email.n8n_execution_id if document.email else None, business_document_type=document.document_type, business_reference=str(business_reference) if business_reference else None, duplicate=document.duplicate, customer_id=customer.customer_id if customer else None, customer_type=(document.evidence or {}).get("customer_resolution", {}).get("customer_type"), processing_attempt_id=latest.id if latest else None, decision=document.decision, decision_reason=document.decision_reason, processing_status=document.processing_status, retryable=document.retryable, failure_type=latest.failure_type if latest else None, evidence=document.evidence, current_stage=document.current_stage, stage_status=document.stage_status, overall_status=document.overall_status, next_attempt_at=document.next_attempt_at, attempt_count=document.attempt_count, retry_allowed=self.retry_allowed(document), storage_reference=document.storage_reference, content_hash=document.content_hash, file_size_bytes=document.file_size_bytes, source_attachment_id=document.source_attachment_id)

    def _response(self, email: Email, db: Session, *, idempotent: bool) -> EmailIntakeResponse:
        documents = list(db.scalars(select(Document).where(Document.email_id == email.id).order_by(Document.id)))
        return EmailIntakeResponse(email_id=email.id, source_email_id=email.source_email_id, correlation_id=email.correlation_id, n8n_execution_id=email.n8n_execution_id, documents=[self._document_result(document, db) for document in documents], idempotent=idempotent)

    def attempts(self, document_id: int, db: Session) -> list[ProcessingAttemptResponse]:
        return [ProcessingAttemptResponse.model_validate(attempt, from_attributes=True) for attempt in db.scalars(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id).order_by(ProcessingAttempt.id))]

    def audit(self, document_id: int, db: Session) -> list[AuditEventResponse]:
        return [AuditEventResponse.model_validate(event, from_attributes=True) for event in db.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id).order_by(AuditEvent.id))]
