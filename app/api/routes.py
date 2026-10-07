from __future__ import annotations

import base64
import binascii
import hmac
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import get_settings
from app.db.models import Document, ListingFile, Requirement
from app.domain.enums import DocumentType, ProcessingStage, RequirementState
from app.db.session import get_db
from app.schemas import AuditEventResponse, ConflictFinding, DetectExceptionsReport, DocumentInput, DocumentReclassifyRequest, DocumentReclassified, DocumentResult, EmailIntakeRequest, EmailIntakeResponse, ListingFileCreate, ListingFileCreated, ListingFileReadout, DocumentClassified, DocumentExtracted, DocumentUploaded, MissingSignatureFinding, N8nIngestRequest, OverdueRequirementFinding, ProcessingAttemptResponse, ReconciliationReport, ReadoutRequirement, RequirementActionResponse, RequirementFlagRequest, RequirementListResponse, RequirementVerifyRequest, ReviewQueue, ReviewQueueDocument, ReviewQueueItem, ReviewQueueUnmatched, VerdictResponse, RequirementSummary
from app.adapters.document_parser import DocumentFormatError, DocumentParser
from app.adapters.llm import LLMError
from app.services.audit import REVIEW_ACTOR, record_audit
from app.services.classification import LLMDocumentClassifier
from app.services.exception_detection import detect_exceptions, is_overdue
from app.services.normalization import document_normalized_fields, ensure_extracted_fields
from app.services.pipeline import DocumentPipeline
from app.services.property_resolution import unmatched_reason
from app.services.readiness import compute_verdict
from app.services.reconciliation import document_type_value, reconcile_listing_file, unmatched_document_ids
from app.services.requirement_catalog import ensure_ca_catalog
from app.services.requirement_engine import RequirementEngine
from app.services.storage import LocalDocumentStorage, StorageError
from app.services.text_sanitization import NulByteRejectedError, ensure_jsonb_value, sanitize_persisted_text

router = APIRouter()


def require_intake_api_key(x_intake_api_key: Optional[str] = Header(default=None)) -> None:
    expected = get_settings().intake_api_key
    if not x_intake_api_key or not hmac.compare_digest(x_intake_api_key, expected):
        raise HTTPException(status_code=401, detail="invalid intake API key")


def validation_detail(error: ValidationError, *, invalid: str, malformed: str) -> str:
    """Build a client-safe 422 detail: field paths and Pydantic messages only.

    Malformed JSON keeps its original wording; anything else reports the
    offending field and the contract it broke, never internal or database
    information.
    """
    issues = error.errors()
    if any(str(item.get("type", "")).startswith("json_") for item in issues):
        return malformed
    parts: list[str] = []
    for item in issues[:5]:
        location_parts = list(item.get("loc", ()))
        if len(location_parts) > 1 and location_parts[0] in {"body", "query", "path"}:
            location_parts = location_parts[1:]
        location = ".".join(str(part) for part in location_parts)
        parts.append(f"{location}: {item.get('msg')}" if location else str(item.get("msg")))
    return f"{invalid}: {'; '.join(parts)}" if parts else invalid


def enforce_request_body_size(request: Request) -> None:
    """Reject an oversized body from ``Content-Length`` alone (HTTP 413).

    Runs before the body is read, so an oversized payload never reaches JSON
    parsing, base64 decoding or storage.  A body without a Content-Length
    header (chunked transfer) cannot be assessed here and is bounded instead
    by the per-file and document-count limits applied after parsing.
    """
    content_length = request.headers.get("content-length")
    if content_length is None:
        return
    try:
        declared = int(content_length)
    except ValueError:
        return
    limit = get_settings().effective_max_request_body_bytes()
    if declared > limit:
        raise HTTPException(status_code=413, detail=f"request body exceeds {limit} bytes")


def enforce_document_count(documents: list) -> None:
    """Bound how many documents one request may create (HTTP 422)."""
    limit = get_settings().max_documents_per_intake
    if len(documents) > limit:
        raise HTTPException(status_code=422, detail=f"too many documents: {len(documents)} exceeds the maximum of {limit}")


def derived_source_attachment_id(source_email_id: str, index: int) -> str:
    """Derive ``documents.source_attachment_id`` for transport-supplied files.

    The value lands in ``documents.source_attachment_id`` (``varchar(255)``).
    With ``SOURCE_EMAIL_ID_MAX`` at 250 and ``index`` bounded by
    ``max_documents_per_intake`` (default 25, so two digits), the longest
    value under current rules is 250 + 1 + 2 = 253 characters, inside the
    column.  The guard keeps that invariant at the only two derivation sites
    if either ceiling is ever raised on its own.
    """
    derived = f"{source_email_id}:{index}"
    if len(derived) > 255:
        raise HTTPException(status_code=422, detail="source_email_id is too long to derive attachment ids")
    return derived


def encoded_attachment_limit() -> int:
    """Maximum accepted base64 length for one n8n document.

    Base64 inflates by 4/3, so a document at ``max_logical_file_bytes`` is
    encoded in at most this many characters.  Checked before decoding, keeping
    the existing HTTP 413 semantics of ``validate_file_content`` while
    stopping a grossly oversized payload from being expanded in memory.
    """
    settings = get_settings()
    return settings.max_logical_file_bytes * 4 // 3 + 4



def validate_file_content(content: bytes, *, filename: str) -> None:
    settings = get_settings()
    max_size = settings.max_logical_file_bytes
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail=f"attachment exceeds {max_size} bytes")
    if not content:
        raise HTTPException(status_code=422, detail="attachment must not be empty")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix == "pdf" and not content.startswith(b"%PDF-"):
        raise HTTPException(status_code=422, detail="attachment content is not a PDF")
    if suffix in {"xlsx", "xlsm"} and not content.startswith(b"PK"):
        raise HTTPException(status_code=422, detail="attachment content is not an XLSX/XLSM workbook")


def store_content(content: bytes, *, expected_hash: Optional[str] = None):
    settings = get_settings()
    storage = LocalDocumentStorage(settings.document_storage_root)
    try:
        return storage.store(content, expected_hash=expected_hash)
    except StorageError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def validate_and_store_content(content: bytes, *, filename: str, expected_hash: Optional[str] = None):
    validate_file_content(content, filename=filename)
    return store_content(content, expected_hash=expected_hash)


async def parse_intake_request(request: Request) -> EmailIntakeRequest:
    """Read and validate intake metadata before any file is touched.

    Order matters: metadata contract first (body size, schema, field limits,
    document count), file reads and storage second, pipeline third - so a
    metadata violation can never cause a database write, storage write, OCR,
    an LLM call or a CRM action.
    """
    enforce_request_body_size(request)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        raw_metadata = form.get("metadata")
        if not isinstance(raw_metadata, str):
            raise HTTPException(status_code=422, detail="multipart metadata field is required")
        try:
            payload = EmailIntakeRequest.model_validate_json(raw_metadata)
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=validation_detail(error, invalid="invalid metadata", malformed="metadata must be valid JSON")) from error
        except ValueError as error:
            raise HTTPException(status_code=422, detail="metadata must be valid JSON") from error
        enforce_document_count(payload.documents)
        uploads = [value for value in form.getlist("files") if isinstance(value, (UploadFile, StarletteUploadFile))]
        if len(uploads) != len(payload.documents):
            raise HTTPException(status_code=422, detail="one file is required for each document")
        for index, (document, upload) in enumerate(zip(payload.documents, uploads), start=1):
            content = await upload.read(get_settings().max_logical_file_bytes + 1)
            filename = upload.filename or document.document_name
            stored = validate_and_store_content(content, filename=filename, expected_hash=document.expected_content_hash)
            document.content_hash = stored.content_hash
            document.storage_reference = stored.storage_reference
            document.file_size_bytes = stored.size_bytes
            document.source_attachment_id = document.source_attachment_id or derived_source_attachment_id(payload.source_email_id, index)
            document.mime_type = upload.content_type or document.mime_type
        # The loop above overwrote fields from the transport (content hash,
        # storage reference, upload MIME type, derived attachment id), so the
        # request goes back through the contract before the pipeline sees it.
        try:
            return EmailIntakeRequest.model_validate(payload.model_dump())
        except ValidationError as error:
            raise HTTPException(status_code=422, detail=validation_detail(error, invalid="invalid metadata", malformed="metadata must be valid JSON")) from error
    try:
        payload = EmailIntakeRequest.model_validate(await request.json())
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=validation_detail(error, invalid="invalid metadata", malformed="request body must be valid JSON")) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="request body must be valid JSON") from error
    enforce_document_count(payload.documents)
    return payload


async def parse_n8n_request(request: Request) -> EmailIntakeRequest:
    """Validate the n8n envelope first, then decode and store each document."""
    enforce_request_body_size(request)
    try:
        envelope = N8nIngestRequest.model_validate(await request.json())
    except ValidationError as error:
        raise HTTPException(status_code=422, detail=validation_detail(error, invalid="invalid metadata", malformed="request body must be valid n8n JSON")) from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="request body must be valid n8n JSON") from error
    enforce_document_count(envelope.documents)

    encoded_limit = encoded_attachment_limit()
    decoded_documents: list[tuple[object, bytes]] = []
    for document in envelope.documents:
        if len(document.content_base64) > encoded_limit:
            # Same status as validate_file_content would return for the
            # decoded file, but raised before base64 expansion.
            raise HTTPException(status_code=413, detail=f"attachment exceeds {get_settings().max_logical_file_bytes} bytes")
        try:
            content = base64.b64decode(document.content_base64, validate=True)
        except (binascii.Error, ValueError) as error:
            raise HTTPException(status_code=422, detail=f"document {document.document_name} has invalid base64 content") from error
        validate_file_content(content, filename=document.document_name)
        decoded_documents.append((document, content))

    documents: list[DocumentInput] = []
    for index, (document, content) in enumerate(decoded_documents, start=1):
        stored = store_content(content)
        documents.append(
            DocumentInput(
                document_name=document.document_name,
                mime_type=document.mime_type,
                storage_reference=stored.storage_reference,
                content_hash=stored.content_hash,
                source_attachment_id=document.source_attachment_id or derived_source_attachment_id(envelope.source_email_id, index),
                file_size_bytes=stored.size_bytes,
            )
        )
    return EmailIntakeRequest(
        source_email_id=envelope.source_email_id,
        received_at=envelope.received_at,
        sender_email=envelope.sender_email,
        sender_name=envelope.sender_name,
        sender_company=envelope.sender_company,
        subject=envelope.subject,
        body=envelope.body,
        documents=documents,
    )


def apply_correlation_headers(payload: EmailIntakeRequest, correlation_id: Optional[str], n8n_execution_id: Optional[str]) -> EmailIntakeRequest:
    data = payload.model_dump()
    data["correlation_id"] = payload.correlation_id or correlation_id
    data["n8n_execution_id"] = payload.n8n_execution_id or n8n_execution_id
    try:
        return EmailIntakeRequest.model_validate(data)
    except ValidationError as error:
        # Header-supplied correlation values are untrusted too: an oversized
        # X-Correlation-Id must fail here with 422, not later at the column.
        raise HTTPException(status_code=422, detail=validation_detail(error, invalid="invalid metadata", malformed="invalid metadata")) from error


def run_intake(payload: EmailIntakeRequest, db: Session) -> EmailIntakeResponse:
    try:
        return DocumentPipeline(get_settings()).process_email(payload, db)
    except NulByteRejectedError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/emails/intake", response_model=EmailIntakeResponse, status_code=201, dependencies=[Depends(require_intake_api_key)])
async def intake_email(request: Request, db: Session = Depends(get_db), x_correlation_id: Optional[str] = Header(default=None), x_n8n_execution_id: Optional[str] = Header(default=None)) -> EmailIntakeResponse:
    payload = apply_correlation_headers(await parse_intake_request(request), x_correlation_id, x_n8n_execution_id)
    return run_intake(payload, db)


@router.post("/ingest/n8n", response_model=EmailIntakeResponse, status_code=201, dependencies=[Depends(require_intake_api_key)])
async def ingest_n8n(request: Request, db: Session = Depends(get_db), x_correlation_id: Optional[str] = Header(default=None), x_n8n_execution_id: Optional[str] = Header(default=None)) -> EmailIntakeResponse:
    payload = apply_correlation_headers(await parse_n8n_request(request), x_correlation_id, x_n8n_execution_id)
    return run_intake(payload, db)


@router.get("/documents/{document_id}", response_model=DocumentResult, dependencies=[Depends(require_intake_api_key)])
def get_document(document_id: int, db: Session = Depends(get_db)) -> DocumentResult:
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return DocumentPipeline(get_settings())._document_result(document, db)


@router.get("/documents/{document_id}/attempts", response_model=list[ProcessingAttemptResponse], dependencies=[Depends(require_intake_api_key)])
def get_attempts(document_id: int, db: Session = Depends(get_db)) -> list[ProcessingAttemptResponse]:
    if db.get(Document, document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    return DocumentPipeline(get_settings()).attempts(document_id, db)


@router.get("/documents/{document_id}/audit", response_model=list[AuditEventResponse], dependencies=[Depends(require_intake_api_key)])
def get_audit(document_id: int, db: Session = Depends(get_db)) -> list[AuditEventResponse]:
    if db.get(Document, document_id) is None:
        raise HTTPException(status_code=404, detail="document not found")
    return DocumentPipeline(get_settings()).audit(document_id, db)


@router.post("/documents/{document_id}/retry", response_model=DocumentResult, dependencies=[Depends(require_intake_api_key)])
def retry_document(document_id: int, db: Session = Depends(get_db)) -> DocumentResult:
    try:
        return DocumentPipeline(get_settings()).retry_document(document_id, db)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/documents/{document_id}/reprocess", response_model=DocumentResult, dependencies=[Depends(require_intake_api_key)])
def reprocess_document(document_id: int, db: Session = Depends(get_db)) -> DocumentResult:
    try:
        return DocumentPipeline(get_settings()).reprocess_document(document_id, db)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/documents/{document_id}/process", response_model=DocumentResult, dependencies=[Depends(require_intake_api_key)])
def process_document(document_id: int, db: Session = Depends(get_db)) -> DocumentResult:
    try:
        return DocumentPipeline(get_settings()).process_document_action(document_id, db)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


def persist_or_422(db: Session) -> None:
    """Flush the current unit of work, mapping NUL rejections to HTTP 422.

    The persistence boundary (``sanitize_session_text``) refuses a NUL byte in
    any structured or JSONB column; here that refusal becomes a client error
    instead of a 500.
    """
    try:
        db.flush()
    except NulByteRejectedError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@router.post("/listing-files", response_model=ListingFileCreated, status_code=201, dependencies=[Depends(require_intake_api_key)])
def create_listing_file(payload: ListingFileCreate, db: Session = Depends(get_db)) -> ListingFileCreated:
    """Stage 1 of the listing workflow: create the file.

    Deliberately does *not* generate requirements -- generation is its own
    visible stage (and is idempotent), so the orchestrator can show it.
    """
    ensure_jsonb_value(payload.property_attributes, "listing_files.property_attributes")
    listing_file = ListingFile(
        property_address=payload.property_address,
        apn=payload.apn,
        seller_name=payload.seller_name,
        property_attributes=payload.property_attributes,
    )
    db.add(listing_file)
    persist_or_422(db)
    db.commit()
    return ListingFileCreated(id=listing_file.id)


@router.post("/listing-files/{listing_file_id}/generate-requirements", response_model=RequirementListResponse, dependencies=[Depends(require_intake_api_key)])
def generate_listing_requirements(listing_file_id: int, db: Session = Depends(get_db)) -> RequirementListResponse:
    """Stage 2: seed the catalog and generate the file's requirements.

    Idempotent (get-or-create, never clobbers state), so the orchestrator
    can re-run it without damage.  Does not touch evidence or the verdict.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    ensure_ca_catalog(db)
    requirements = RequirementEngine().generate(db, listing_file)
    db.commit()
    return RequirementListResponse(
        requirements=[
            RequirementSummary(
                requirement_key=requirement.requirement_key,
                requirement_type=requirement.requirement_type,
                status=requirement.status,
                state=requirement.state,
            )
            for requirement in requirements
        ]
    )


@router.post("/listing-files/{listing_file_id}/documents", response_model=DocumentUploaded, status_code=201, dependencies=[Depends(require_intake_api_key)])
async def upload_listing_document(listing_file_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)) -> DocumentUploaded:
    """Stage 3: store one document and record it against the file.

    Validated and stored through the same helpers as chassis intake; the row
    starts ``UNKNOWN`` and unclassified -- classification is its own stage.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    filename = file.filename or ""
    if not filename:
        raise HTTPException(status_code=422, detail="file name is required")
    if len(filename) > 255:
        raise HTTPException(status_code=422, detail="file name is too long (maximum 255 characters)")
    content = await file.read(get_settings().max_logical_file_bytes + 1)
    stored = validate_and_store_content(content, filename=filename)
    document = Document(
        listing_file_id=listing_file.id,
        document_name=filename,
        mime_type=file.content_type or "application/octet-stream",
        content_hash=stored.content_hash,
        storage_reference=stored.storage_reference,
        file_size_bytes=stored.size_bytes,
        document_type=DocumentType.UNKNOWN,
        current_stage=ProcessingStage.CLASSIFICATION,
    )
    db.add(document)
    persist_or_422(db)
    db.commit()
    return DocumentUploaded(document_id=document.id)


def ensure_document_text(db: Session, document: Document) -> None:
    """Parse the stored file once if the document has no text yet.

    The listing workflow has no separate parse stage, so classification
    acquires its own input here: without the marker-bearing text a demo
    document would come back UNKNOWN even though the file is on disk.
    """
    if document.extracted_text or not document.storage_reference:
        return
    storage = LocalDocumentStorage(get_settings().document_storage_root)
    try:
        parsed = DocumentParser().parse(
            storage.resolve(document.storage_reference),
            mime_type=document.mime_type,
            document_name=document.document_name,
        )
    except (StorageError, DocumentFormatError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    document.extracted_text = sanitize_persisted_text(parsed.extracted_text)


@router.post("/documents/{document_id}/classify", response_model=DocumentClassified, dependencies=[Depends(require_intake_api_key)])
def classify_listing_document(document_id: int, db: Session = Depends(get_db)) -> DocumentClassified:
    """Stage 4: classify one stored document.

    Runs the configured provider (mock in dev/demo) and records the type
    and its confidence.  ``UNKNOWN`` is a valid honest outcome -- it sends
    the document to review rather than guessing.  The provider is called
    once and its per-type fields are persisted with the result, so the
    extract stage reports them instead of classifying a second time.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    ensure_document_text(db, document)
    try:
        result = LLMDocumentClassifier(get_settings()).classify(
            document_id=document.id,
            document_name=document.document_name,
            mime_type=document.mime_type,
            extracted_text=document.extracted_text or "",
            extracted_data=document.extracted_data or {},
        )
    except LLMError as error:
        raise HTTPException(status_code=422 if error.needs_review else 502, detail=str(error)) from error
    normalized = ensure_extracted_fields(result.extracted_data)
    document.document_type = result.document_type
    document.classification_confidence = result.confidence
    document.extracted_data = result.extracted_data
    document.normalized_data = normalized
    persist_or_422(db)
    db.commit()
    return DocumentClassified(
        document_id=document.id,
        document_type=result.document_type.value,
        classification_confidence=result.confidence,
    )


@router.post("/documents/{document_id}/extract", response_model=DocumentExtracted, dependencies=[Depends(require_intake_api_key)])
def extract_listing_document(document_id: int, db: Session = Depends(get_db)) -> DocumentExtracted:
    """Stage 5: the per-type schema fields for one document.

    Classification already ran the provider and persisted its fields, so
    this reports them; called before classify (or after a re-upload) it runs
    the provider itself.  Extraction never sets ``document_type`` -- that is
    the classify stage's write, so skipping a stage stays visible instead of
    silently doing two jobs.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    if not document.extracted_data:
        ensure_document_text(db, document)
        try:
            result = LLMDocumentClassifier(get_settings()).classify(
                document_id=document.id,
                document_name=document.document_name,
                mime_type=document.mime_type,
                extracted_text=document.extracted_text or "",
                extracted_data={},
            )
        except LLMError as error:
            raise HTTPException(status_code=422 if error.needs_review else 502, detail=str(error)) from error
        normalized = ensure_extracted_fields(result.extracted_data)
        document.extracted_data = result.extracted_data
        document.normalized_data = normalized
        persist_or_422(db)
        db.commit()
    return DocumentExtracted(
        document_id=document.id,
        document_type=document_type_value(document),
        extracted_data=document.extracted_data or {},
        normalized_data=document_normalized_fields(document),
    )


@router.post("/listing-files/{listing_file_id}/reconcile", response_model=ReconciliationReport, dependencies=[Depends(require_intake_api_key)])
def reconcile_listing(listing_file_id: int, db: Session = Depends(get_db)) -> ReconciliationReport:
    """Stage 6: match classified documents to the generated requirements.

    Moves matched requirements PENDING -> RECEIVED and records Evidence.
    Generation is deliberately *not* called here: a file with no
    requirements reconciles to an empty result, which the orchestrator sees
    rather than a hidden generation step.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    result = reconcile_listing_file(db, listing_file)
    db.commit()
    return ReconciliationReport(**asdict(result))


@router.post("/listing-files/{listing_file_id}/detect-exceptions", response_model=DetectExceptionsReport, dependencies=[Depends(require_intake_api_key)])
def detect_listing_exceptions(listing_file_id: int, db: Session = Depends(get_db)) -> DetectExceptionsReport:
    """Consistency pass: evaluate rules R1-R3 over the file's current state.

    R1 (cross-document field conflict) and R2 (missing signatures) raise
    ``EXCEPTION`` states with audit events; R3 (overdue) only reports.
    The verdict is deliberately not recomputed here -- run the verdict
    stage after this one to roll the new states up.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    report = detect_exceptions(
        db,
        listing_file,
        overdue_days=get_settings().requirement_overdue_days,
    )
    db.commit()
    return DetectExceptionsReport(
        listing_file_id=listing_file.id,
        conflicts=[ConflictFinding(**asdict(conflict)) for conflict in report.conflicts],
        missing_signatures=[
            MissingSignatureFinding(**asdict(finding)) for finding in report.missing_signatures
        ],
        overdue=[OverdueRequirementFinding(**asdict(entry)) for entry in report.overdue],
        exceptions_raised=report.exceptions_raised,
    )


@router.post("/listing-files/{listing_file_id}/verdict", response_model=VerdictResponse, dependencies=[Depends(require_intake_api_key)])
def compute_listing_verdict(listing_file_id: int, db: Session = Depends(get_db)) -> VerdictResponse:
    """Stage 7: roll requirement states up to the file-level verdict.

    Pure function of current requirement state -- re-running overwrites the
    previous verdict.  Reconciliation reaches RECEIVED, never VERIFIED, so
    CONDITIONALLY_READY is the highest verdict until a human verifies via
    POST /requirements/{id}/verify (Slice 8).
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    result = compute_verdict(db, listing_file)
    db.commit()
    return VerdictResponse(
        verdict=result.verdict.value,
        reason=result.reason,
        blocking_pending=result.blocking_pending,
        blocking_exceptions=result.blocking_exceptions,
        pending_verification=result.pending_verification,
        advisory=result.advisory,
    )


@router.get("/listing-files/{listing_file_id}", response_model=ListingFileReadout, dependencies=[Depends(require_intake_api_key)])
def get_listing_file_readout(listing_file_id: int, db: Session = Depends(get_db)) -> ListingFileReadout:
    """Stage 8: one read of the whole file -- facts, requirements, verdict.

    ``verdict``/``reason`` are what the verdict stage last stored (null
    until it runs), and ``unmatched_document_ids`` is the review queue:
    documents no requirement has evidence for.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    requirements = (
        db.query(Requirement)
        .filter(Requirement.listing_file_id == listing_file.id)
        .order_by(Requirement.requirement_key)
        .all()
    )
    overdue_days = get_settings().requirement_overdue_days
    return ListingFileReadout(
        id=listing_file.id,
        property_address=listing_file.property_address,
        apn=listing_file.apn,
        seller_name=listing_file.seller_name,
        property_attributes=listing_file.property_attributes,
        verdict=listing_file.readiness_verdict,
        reason=listing_file.readiness_reason,
        readiness_assessed_at=listing_file.readiness_assessed_at,
        requirements=[
            ReadoutRequirement(
                requirement_key=requirement.requirement_key,
                requirement_type=requirement.requirement_type,
                status=requirement.status,
                state=requirement.state,
                overdue=is_overdue(listing_file, requirement, threshold_days=overdue_days),
            )
            for requirement in requirements
        ],
        unmatched_document_ids=unmatched_document_ids(db, listing_file),
    )


def _queue_item(requirement: Requirement) -> ReviewQueueItem:
    return ReviewQueueItem(
        requirement_id=requirement.id,
        requirement_key=requirement.requirement_key,
        requirement_type=requirement.requirement_type,
        status=requirement.status,
        state=requirement.state,
        state_reason=requirement.state_reason,
    )


@router.get("/listing-files/{listing_file_id}/review-queue", response_model=ReviewQueue, dependencies=[Depends(require_intake_api_key)])
def get_review_queue(listing_file_id: int, db: Session = Depends(get_db)) -> ReviewQueue:
    """The human review queue: what needs a person, in five flat buckets.

    A projection of current state only -- no ranking, no confidence
    threshold.  ``unknown_documents`` is a classification outcome;
    ``unmatched_documents`` carries the readout's evidence-based projection
    (unknown ones included) with each document's unmatched reason;
    ``overdue`` surfaces PENDING requirements past the configured threshold
    without changing their state.
    """
    listing_file = db.get(ListingFile, listing_file_id)
    if listing_file is None:
        raise HTTPException(status_code=404, detail="listing file not found")
    overdue_days = get_settings().requirement_overdue_days
    requirements = (
        db.query(Requirement)
        .filter(Requirement.listing_file_id == listing_file.id)
        .order_by(Requirement.requirement_key)
        .all()
    )
    documents = (
        db.query(Document)
        .filter(Document.listing_file_id == listing_file.id)
        .order_by(Document.id)
        .all()
    )
    unmatched_ids = set(unmatched_document_ids(db, listing_file))
    return ReviewQueue(
        listing_file_id=listing_file.id,
        exceptions=[
            _queue_item(requirement)
            for requirement in requirements
            if requirement.state == RequirementState.EXCEPTION.value
        ],
        unknown_documents=[
            ReviewQueueDocument(document_id=document.id, document_name=document.document_name)
            for document in documents
            if document_type_value(document) == DocumentType.UNKNOWN.value
        ],
        unmatched_documents=[
            ReviewQueueUnmatched(
                document_id=document.id,
                document_name=document.document_name,
                reason=unmatched_reason(document, listing_file),
            )
            for document in documents
            if document.id in unmatched_ids
        ],
        pending_verification=[
            _queue_item(requirement)
            for requirement in requirements
            if requirement.state == RequirementState.RECEIVED.value
        ],
        overdue=[
            _queue_item(requirement)
            for requirement in requirements
            if is_overdue(listing_file, requirement, threshold_days=overdue_days)
        ],
    )


def _apply_requirement_action(
    db: Session,
    requirement: Requirement,
    *,
    new_state: str,
    state_reason: str,
    event_type: str,
    message: str,
    details: dict,
) -> RequirementActionResponse:
    """Move one requirement to a new state, audit it, re-roll the verdict.

    The audit row links to the requirement's first evidence document (when
    one exists) so it is reachable through ``GET /documents/{id}/audit``;
    the requirement ids live in ``details`` either way.
    """
    previous_state = requirement.state
    requirement.state = new_state
    requirement.state_reason = state_reason
    evidence_documents = sorted(requirement.evidence, key=lambda item: item.id)
    record_audit(
        db,
        document_id=evidence_documents[0].document_id if evidence_documents else None,
        event_type=event_type,
        status="SUCCESS",
        message=message,
        details={
            "requirement_id": requirement.id,
            "requirement_key": requirement.requirement_key,
            "previous_state": previous_state,
            "actor": REVIEW_ACTOR,
            **details,
        },
    )
    verdict = compute_verdict(db, requirement.listing_file)
    db.commit()
    return RequirementActionResponse(
        requirement_id=requirement.id,
        requirement_key=requirement.requirement_key,
        state=requirement.state,
        state_reason=requirement.state_reason,
        verdict=verdict.verdict.value,
        reason=verdict.reason,
    )


@router.post("/requirements/{requirement_id}/verify", response_model=RequirementActionResponse, dependencies=[Depends(require_intake_api_key)])
def verify_requirement(requirement_id: int, payload: RequirementVerifyRequest, db: Session = Depends(get_db)) -> RequirementActionResponse:
    """Human verification: RECEIVED / PENDING / EXCEPTION -> VERIFIED.

    Then re-rolls the file verdict: clearing an EXCEPTION on a blocking
    requirement lifts its hard block back toward CONDITIONALLY_READY.
    """
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="requirement not found")
    return _apply_requirement_action(
        db,
        requirement,
        new_state=RequirementState.VERIFIED.value,
        state_reason=payload.note or "verified by human review",
        event_type="REQUIREMENT_VERIFIED",
        message="requirement verified by human review",
        details={"note": payload.note},
    )


@router.post("/requirements/{requirement_id}/flag", response_model=RequirementActionResponse, dependencies=[Depends(require_intake_api_key)])
def flag_requirement(requirement_id: int, payload: RequirementFlagRequest, db: Session = Depends(get_db)) -> RequirementActionResponse:
    """Human flag: any state -> EXCEPTION, reason required.

    On a blocking requirement this makes the verdict NOT_READY; evidence
    is untouched (no deletion, no reassignment).
    """
    requirement = db.get(Requirement, requirement_id)
    if requirement is None:
        raise HTTPException(status_code=404, detail="requirement not found")
    return _apply_requirement_action(
        db,
        requirement,
        new_state=RequirementState.EXCEPTION.value,
        state_reason=payload.reason,
        event_type="REQUIREMENT_FLAGGED",
        message="requirement flagged for review",
        details={"reason": payload.reason},
    )


@router.post("/documents/{document_id}/reclassify", response_model=DocumentReclassified, dependencies=[Depends(require_intake_api_key)])
def reclassify_document(document_id: int, payload: DocumentReclassifyRequest, db: Session = Depends(get_db)) -> DocumentReclassified:
    """Correct an UNKNOWN classification so the next reconcile can match it.

    Only ``UNKNOWN`` documents may be reclassified: an already-classified
    document's evidence stands, and reassignment would rewrite history the
    evidence rows describe.  Reconciliation itself is *not* run here.
    """
    document = db.get(Document, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    previous_document_type = document_type_value(document)
    if previous_document_type != DocumentType.UNKNOWN.value:
        raise HTTPException(status_code=409, detail=f"document already classified as {previous_document_type}")
    document.document_type = payload.document_type.value
    record_audit(
        db,
        document_id=document.id,
        event_type="DOCUMENT_RECLASSIFIED",
        status="SUCCESS",
        message="document reclassified by human review",
        details={
            "previous_document_type": previous_document_type,
            "document_type": document.document_type,
            "actor": REVIEW_ACTOR,
        },
    )
    db.commit()
    return DocumentReclassified(document_id=document.id, document_type=document.document_type)
