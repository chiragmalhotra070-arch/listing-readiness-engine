from __future__ import annotations

import base64
import binascii
import hmac
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, UploadFile
from pydantic import ValidationError
from sqlalchemy.orm import Session
from starlette.datastructures import UploadFile as StarletteUploadFile

from app.config import get_settings
from app.db.models import Document
from app.db.session import get_db
from app.schemas import AuditEventResponse, DocumentInput, DocumentResult, EmailIntakeRequest, EmailIntakeResponse, N8nIngestRequest, ProcessingAttemptResponse
from app.services.pipeline import DocumentPipeline
from app.services.storage import LocalDocumentStorage, StorageError
from app.services.text_sanitization import NulByteRejectedError

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
