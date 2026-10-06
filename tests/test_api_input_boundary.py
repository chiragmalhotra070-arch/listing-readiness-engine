"""API input-boundary tests: request metadata validation (Phase 9).

Oversized or structurally invalid *metadata* used to pass straight through
Pydantic and travel into the application, where the first hard failure could
be PostgreSQL itself (``value too long for type character varying(n)``, SQLSTATE
22001) or an unbounded string silently carried into OCR/LLM/CRM work.  The
contract now rejects it at the request boundary:

* every ``max_length`` equals the column it is persisted into;
* patterns are used only for shapes the system defines (MIME types, sha256
  hashes, path-ish attachment ids and storage references);
* a document-count limit and a Content-Length check bound one request's
  aggregate cost (configurable, HTTP 413 for body size).

Ordering is part of the contract: body size and metadata are validated before
any file is read or stored, so a rejected request creates no Email, Document,
work item, attempt or audit row, runs no OCR/LLM/CRM code and writes no file.

PostgreSQL integration tests (the BEFORE state - an ORM write that really does
fail with 22001 - and the AFTER state) run only when
``ENGINE_PG_TEST_DATABASE_URL`` is set.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import DataError
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api import routes
from app.config import Settings, get_settings
from app.db.models import AuditEvent, Document, DocumentWorkItem, Email, ProcessingAttempt, ProcessingRun
from app.db.session import Base, get_db
from app.main import app
from app.schemas import (
    BODY_MAX_LENGTH,
    SOURCE_EMAIL_ID_MAX,
    DocumentInput,
    EmailIntakeRequest,
    N8nIngestRequest,
)

PG_URL = os.environ.get("ENGINE_PG_TEST_DATABASE_URL")
FIXTURES = Path(__file__).parent / "fixtures"
VALID_PDF = (FIXTURES / "valid_remittance.pdf").read_bytes()

COUNTED_TABLES = (Email, Document, DocumentWorkItem, ProcessingRun, ProcessingAttempt, AuditEvent)


# --------------------------------------------------------------------------
# scaffolding
# --------------------------------------------------------------------------


class Api:
    """TestClient plus a handle on the database it writes to."""

    def __init__(self, client: TestClient, factory) -> None:
        self.client = client
        self.factory = factory

    def counts(self) -> dict[str, int]:
        with self.factory() as session:
            return {table.__tablename__: session.scalar(select(func.count()).select_from(table)) for table in COUNTED_TABLES}


@pytest.fixture
def api():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"}, raise_server_exceptions=False)
    yield Api(client, factory)
    app.dependency_overrides.clear()
    engine.dispose()


def intake_payload(source_email_id: str | None = None, **overrides) -> dict:
    data = {
        "source_email_id": source_email_id or f"boundary-{uuid.uuid4().hex}",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "boundary@example.test",
        "applicability": True,
        "documents": [{"document_name": "remittance.pdf", "mime_type": "application/pdf", "mock_profile": "valid_remittance"}],
    }
    data.update(overrides)
    return data


def post_json(api: Api, data: dict, headers: dict | None = None):
    return api.client.post("/v1/emails/intake", json=data, headers=headers or {})


def post_multipart(api: Api, metadata: dict, files=None):
    attachments = files if files is not None else [("files", ("remittance.pdf", VALID_PDF, "application/pdf"))]
    return api.client.post("/v1/emails/intake", data={"metadata": json.dumps(metadata)}, files=attachments)


def n8n_envelope(source_email_id: str | None = None, documents: list[dict] | None = None, **overrides) -> dict:
    data = {
        "source_email_id": source_email_id or f"n8n-boundary-{uuid.uuid4().hex}",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "n8n-boundary@example.test",
        "subject": "n8n ingestion",
        "documents": documents
        or [
            {
                "document_name": "remittance.pdf",
                "mime_type": "application/pdf",
                "source_attachment_id": "gmail-attachment-1",
                "content_base64": base64.b64encode(VALID_PDF).decode("ascii"),
            }
        ],
    }
    data.update(overrides)
    return data


def post_n8n(api: Api, envelope: dict, headers: dict | None = None):
    return api.client.post("/v1/ingest/n8n", json=envelope, headers=headers or {})


def post_transport(api: Api, transport: str, **overrides):
    """Post one intake request on the given transport with ``overrides`` applied."""
    if transport == "json":
        return post_json(api, intake_payload(**overrides))
    if transport == "multipart":
        metadata = intake_payload(**overrides)
        return post_multipart(api, metadata)
    if transport == "n8n":
        return post_n8n(api, n8n_envelope(**overrides))
    raise AssertionError(f"unknown transport {transport}")


TRANSPORTS = ["json", "multipart", "n8n"]


class _ExplodingPipeline:
    """Fails the test if an invalid request ever reaches the pipeline."""

    def __init__(self, *args, **kwargs) -> None:
        raise AssertionError("the pipeline must not run for a rejected request")


def settings_with(**updates) -> Settings:
    return get_settings().model_copy(update=updates)


def storage_files(root: str) -> list[Path]:
    base = Path(root)
    return [path for path in base.rglob("*") if path.is_file()] if base.exists() else []


# --------------------------------------------------------------------------
# A. existing valid requests continue to pass
# --------------------------------------------------------------------------


def test_valid_json_intake_is_accepted(api: Api) -> None:
    response = post_json(api, intake_payload())
    assert response.status_code == 201, response.text
    assert response.json()["documents"][0]["decision"] == "READY_FOR_PROCESSING"


def test_valid_multipart_intake_is_accepted(api: Api) -> None:
    response = post_multipart(api, intake_payload())
    assert response.status_code == 201, response.text
    document = response.json()["documents"][0]
    assert document["content_hash"] == __import__("hashlib").sha256(VALID_PDF).hexdigest()


def test_valid_n8n_intake_is_accepted(api: Api) -> None:
    response = post_n8n(api, n8n_envelope())
    assert response.status_code == 201, response.text
    assert response.json()["documents"][0]["id"]


def test_valid_requests_with_unusual_free_form_text_are_accepted(api: Api) -> None:
    subject = 'RE: FW: "Sonderzeichen" — Übergabe (1/2) 🧾 100%'
    company = "Müller & Söhne ( GmbH ) & Co. KG"
    name = "Señor Ñandú"
    body = "line one\r\n\ttabbed line two … ünïcode ✓"
    response = post_json(api, intake_payload(subject=subject, sender_company=company, sender_name=name, body=body))
    assert response.status_code == 201, response.text
    with api.factory() as session:
        email = session.scalar(select(Email).where(Email.source_email_id == response.json()["source_email_id"]))
        assert email.subject == subject
        assert email.sender_company == company
        assert email.sender_name == name
        assert email.body == body


# --------------------------------------------------------------------------
# B. exactly-at-limit / one-over-limit
# --------------------------------------------------------------------------


def _fill(length: int, char: str = "a") -> str:
    return char * length


TOP_LEVEL_BOUNDARIES = [
    ("source_email_id", SOURCE_EMAIL_ID_MAX, "a"),
    ("sender_email", 320, None),
    ("sender_name", 255, "n"),
    ("sender_company", 255, "c"),
    ("subject", 1000, "s"),
    ("body", BODY_MAX_LENGTH, "b"),
    ("correlation_id", 255, "k"),
    ("n8n_execution_id", 255, "e"),
]

DOCUMENT_BOUNDARIES = [
    ("document_name", 255, "d"),
    ("mock_profile", 64, "m"),
    ("content_hash", 128, "0"),
    ("source_attachment_id", 255, "1"),
]


def _value_for(field: str, length: int, char: str | None, source_email_id: str) -> str:
    if field == "source_email_id":
        # unique within the run, exactly ``length`` characters
        prefix = uuid.uuid4().hex
        return (prefix + "a" * length)[:length]
    if field == "sender_email":
        return "a" * (length - len("@example.test")) + "@example.test"
    if field == "content_hash":
        # must stay inside the hash alphabet as well as the length limit
        return "0" * length
    if char is None:
        char = "x"
    return _fill(length, char)


@pytest.mark.parametrize("field,limit,char", TOP_LEVEL_BOUNDARIES, ids=[name for name, _, _ in TOP_LEVEL_BOUNDARIES])
def test_top_level_field_boundaries(api: Api, field: str, limit: int, char: str | None) -> None:
    source = uuid.uuid4().hex
    at_limit = _value_for(field, limit, char, source)
    payload = intake_payload(source)
    payload[field] = at_limit
    response = post_json(api, payload)
    assert response.status_code == 201, f"{field} at exactly {limit} characters must be accepted: {response.text}"

    payload = intake_payload()
    payload[field] = _value_for(field, limit + 1, char, source)
    response = post_json(api, payload)
    assert response.status_code == 422, f"{field} one over the limit must be rejected: {response.text}"
    assert field in response.json()["detail"]


@pytest.mark.parametrize("field,limit,char", DOCUMENT_BOUNDARIES, ids=[name for name, _, _ in DOCUMENT_BOUNDARIES])
def test_document_field_boundaries(api: Api, field: str, limit: int, char: str) -> None:
    document = {"document_name": "remittance.pdf", "mime_type": "application/pdf", field: _value_for(field, limit, char, "")}
    response = post_json(api, intake_payload(documents=[document]))
    assert response.status_code == 201, f"documents.{field} at exactly {limit} characters must be accepted: {response.text}"

    document = {"document_name": "remittance.pdf", "mime_type": "application/pdf", field: _value_for(field, limit + 1, char, "")}
    response = post_json(api, intake_payload(documents=[document]))
    assert response.status_code == 422, f"documents.{field} one over the limit must be rejected: {response.text}"
    assert field in response.json()["detail"]


def test_storage_reference_length_and_shape_boundaries(api: Api) -> None:
    reference = "local://documents/" + "ab/cd/" + "0" * 64
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "storage_reference": reference}]))
    assert response.status_code == 201, response.text

    for invalid in ("has space.pdf", "local://documents/ab/cd/0" * 64, 'quote"ref.pdf', "tab\tref.pdf"):
        response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "storage_reference": invalid}]))
        assert response.status_code == 422, f"{invalid!r} must be rejected: {response.text}"
        assert "storage_reference" in response.json()["detail"]


# --------------------------------------------------------------------------
# C. identifiers
# --------------------------------------------------------------------------


def test_contract_constraints_live_on_the_request_models() -> None:
    """The limits are part of the schema, so every caller inherits them."""
    with pytest.raises(ValidationError):
        DocumentInput(document_name="x.pdf", mime_type="not-a-mime")
    with pytest.raises(ValidationError):
        DocumentInput(document_name="x.pdf", mime_type="application/pdf", expected_content_hash="nope")
    with pytest.raises(ValidationError):
        DocumentInput(document_name="x.pdf", mime_type="application/pdf", source_attachment_id="a" * 256)
    assert DocumentInput(document_name="Separate Remittance Advice (1) (1).pdf", mime_type="application/pdf").document_name == "Separate Remittance Advice (1) (1).pdf"

    request = EmailIntakeRequest(
        source_email_id="a" * SOURCE_EMAIL_ID_MAX,
        received_at=datetime.now(timezone.utc),
        sender_email="boundary@example.test",
        documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf"}],
    )
    assert request.source_email_id == "a" * SOURCE_EMAIL_ID_MAX
    with pytest.raises(ValidationError):
        EmailIntakeRequest(
            source_email_id="a" * (SOURCE_EMAIL_ID_MAX + 1),
            received_at=datetime.now(timezone.utc),
            sender_email="boundary@example.test",
            documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf"}],
        )

    with pytest.raises(ValidationError):
        N8nIngestRequest(
            source_email_id="n8n-boundary",
            received_at=datetime.now(timezone.utc),
            sender_email="boundary@example.test",
            documents=[{"document_name": "remittance.pdf", "mime_type": "not-a-mime", "content_base64": "AA=="}],
        )


def test_oversized_source_email_id_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload("a" * (SOURCE_EMAIL_ID_MAX + 1)))
    assert response.status_code == 422
    assert "source_email_id" in response.json()["detail"]


def test_oversized_correlation_id_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(correlation_id="c" * 256))
    assert response.status_code == 422
    assert "correlation_id" in response.json()["detail"]


def test_oversized_n8n_execution_id_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(n8n_execution_id="e" * 256))
    assert response.status_code == 422
    assert "n8n_execution_id" in response.json()["detail"]


@pytest.mark.parametrize("header", ["X-Correlation-Id", "X-N8n-Execution-Id"])
def test_oversized_correlation_headers_are_rejected(api: Api, header: str) -> None:
    response = post_json(api, intake_payload(), headers={header: "h" * 256})
    assert response.status_code == 422, response.text
    assert "correlation" in response.json()["detail"].lower() or "n8n_execution_id" in response.json()["detail"]


def test_oversized_source_attachment_id_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "source_attachment_id": "a" * 256}]))
    assert response.status_code == 422
    assert "source_attachment_id" in response.json()["detail"]


@pytest.mark.parametrize("bad_hash", ["not-a-hash", "0" * 63, "0" * 65, "g" * 64, ""])
def test_invalid_expected_content_hash_shape_is_rejected(api: Api, bad_hash: str) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "expected_content_hash": bad_hash}]))
    assert response.status_code == 422, response.text
    assert "expected_content_hash" in response.json()["detail"]


def test_sha256_shaped_expected_hash_reaches_the_content_check_not_the_schema(api: Api) -> None:
    # a structurally valid but wrong hash is the existing content-mismatch 422
    response = post_multipart(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "expected_content_hash": "0" * 64}]))
    assert response.status_code == 422
    assert "expected_content_hash" not in response.json()["detail"]


# --------------------------------------------------------------------------
# D. human-entered metadata
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,limit",
    [("sender_name", 255), ("sender_company", 255), ("subject", 1000), ("body", BODY_MAX_LENGTH)],
    ids=["sender_name", "sender_company", "subject", "body"],
)
def test_oversized_human_metadata_is_rejected(api: Api, field: str, limit: int) -> None:
    response = post_json(api, intake_payload(**{field: "x" * (limit + 1)}))
    assert response.status_code == 422
    assert field in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_sender_email_over_column_width_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(sender_email="a" * 321))
    assert response.status_code == 422
    assert "sender_email" in response.json()["detail"]


# --------------------------------------------------------------------------
# E. document metadata
# --------------------------------------------------------------------------


def test_oversized_document_name_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "n" * 256, "mime_type": "application/pdf"}]))
    assert response.status_code == 422
    assert "document_name" in response.json()["detail"]


@pytest.mark.parametrize("mime_type", ["not-a-mime", "application", "", "/pdf", "application/pdf\nX-Injected: 1", "a b/c"])
def test_malformed_mime_type_is_rejected(api: Api, mime_type: str) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": mime_type}]))
    assert response.status_code == 422, response.text
    assert "mime_type" in response.json()["detail"]


def test_mime_type_with_parameters_is_accepted(api: Api) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": 'application/pdf; name="remittance.pdf"'}]))
    assert response.status_code == 201, response.text


def test_oversized_mime_type_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/" + "x" * 256}]))
    assert response.status_code == 422
    assert "mime_type" in response.json()["detail"]


def test_oversized_mock_profile_is_rejected(api: Api) -> None:
    response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "mock_profile": "p" * 65}]))
    assert response.status_code == 422
    assert "mock_profile" in response.json()["detail"]


def test_unbounded_file_size_values_are_rejected(api: Api) -> None:
    for value in (-1, 2**40):
        response = post_json(api, intake_payload(documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf", "file_size_bytes": value}]))
        assert response.status_code == 422, f"file_size_bytes={value} must be rejected: {response.text}"
        assert "file_size_bytes" in response.json()["detail"]


# --------------------------------------------------------------------------
# F. request structure
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda data: data.pop("source_email_id"), id="missing-source_email_id"),
        pytest.param(lambda data: data.pop("sender_email"), id="missing-sender_email"),
        pytest.param(lambda data: data.update(documents=[]), id="empty-documents"),
        pytest.param(lambda data: data.update(documents="not-a-list"), id="documents-wrong-type"),
        pytest.param(lambda data: data.update(documents=[{"document_name": "remittance.pdf"}]), id="document-missing-mime_type"),
        pytest.param(lambda data: data.update(documents=[{"mime_type": "application/pdf"}]), id="document-missing-document_name"),
        pytest.param(lambda data: data.update(received_at="not-a-datetime"), id="received_at-wrong-type"),
        pytest.param(lambda data: data.update(applicability="perhaps"), id="applicability-wrong-type"),
    ],
)
def test_malformed_request_shapes_are_rejected(api: Api, mutate) -> None:
    data = intake_payload()
    mutate(data)
    response = post_json(api, data)
    assert response.status_code == 422, response.text
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_too_many_documents_are_rejected(api: Api) -> None:
    limit = get_settings().max_documents_per_intake
    documents = [{"document_name": f"doc-{index}.pdf", "mime_type": "application/pdf"} for index in range(limit + 1)]
    response = post_json(api, intake_payload(documents=documents))
    assert response.status_code == 422, response.text
    assert "too many documents" in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_document_count_limit_is_configurable(api: Api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "get_settings", lambda: settings_with(max_documents_per_intake=2))
    documents = [{"document_name": f"doc-{index}.pdf", "mime_type": "application/pdf"} for index in range(2)]
    assert post_json(api, intake_payload(documents=documents)).status_code == 201

    documents = [{"document_name": f"doc-{index}.pdf", "mime_type": "application/pdf"} for index in range(3)]
    response = post_json(api, intake_payload(documents=documents))
    assert response.status_code == 422
    assert "maximum of 2" in response.json()["detail"]


def test_default_body_limit_is_derived_from_document_and_file_limits() -> None:
    settings = Settings(max_documents_per_intake=10, max_logical_file_bytes=300, max_request_body_bytes=0)
    assert settings.effective_max_request_body_bytes() == 10 * 300 * 4 // 3 + 1024 * 1024
    assert Settings(max_request_body_bytes=4096).effective_max_request_body_bytes() == 4096


def test_request_body_over_the_limit_returns_413(api: Api, monkeypatch) -> None:
    monkeypatch.setattr(routes, "get_settings", lambda: settings_with(max_request_body_bytes=1024))
    response = post_json(api, intake_payload(body="b" * 4000))
    assert response.status_code == 413, response.text
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}

    # a body inside the same limit is still accepted
    assert post_json(api, intake_payload(subject="small")).status_code == 201


# --------------------------------------------------------------------------
# G. both transport paths
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_oversized_source_email_id_is_rejected_on_every_transport(api: Api, transport: str) -> None:
    response = post_transport(api, transport, source_email_id="a" * (SOURCE_EMAIL_ID_MAX + 1))
    assert response.status_code == 422, response.text
    assert "source_email_id" in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_oversized_subject_is_rejected_on_every_transport(api: Api, transport: str) -> None:
    response = post_transport(api, transport, subject="s" * 1001)
    assert response.status_code == 422, response.text
    assert "subject" in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_malformed_mime_type_is_rejected_on_every_transport(api: Api, transport: str) -> None:
    def apply():
        if transport == "n8n":
            documents = n8n_envelope()["documents"]
            documents[0]["mime_type"] = "not-a-mime"
            return post_n8n(api, n8n_envelope(documents=documents))
        documents = intake_payload()["documents"]
        documents[0]["mime_type"] = "not-a-mime"
        if transport == "json":
            return post_json(api, intake_payload(documents=documents))
        return post_multipart(api, intake_payload(documents=documents))

    response = apply()
    assert response.status_code == 422, response.text
    assert "mime_type" in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_too_many_documents_are_rejected_on_every_transport(api: Api, transport: str) -> None:
    limit = get_settings().max_documents_per_intake
    documents = [{"document_name": f"doc-{index}.pdf", "mime_type": "application/pdf", "mock_profile": "valid_remittance"} for index in range(limit + 1)]
    if transport == "n8n":
        documents = [
            {
                "document_name": f"doc-{index}.pdf",
                "mime_type": "application/pdf",
                "content_base64": base64.b64encode(VALID_PDF).decode("ascii"),
            }
            for index in range(limit + 1)
        ]
        response = post_n8n(api, n8n_envelope(documents=documents))
    else:
        response = post_transport(api, transport, documents=documents)
    assert response.status_code == 422, response.text
    assert "too many documents" in response.json()["detail"]
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_n8n_encoded_payload_over_the_file_limit_returns_413_before_decoding(api: Api, monkeypatch) -> None:
    storage_root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"phase9-n8n-{uuid.uuid4().hex}")
    monkeypatch.setattr(routes, "get_settings", lambda: settings_with(max_logical_file_bytes=64, document_storage_root=storage_root))
    content = b"%PDF-" + b"A" * 500
    envelope = n8n_envelope(documents=[{"document_name": "big.pdf", "mime_type": "application/pdf", "content_base64": base64.b64encode(content).decode("ascii")}])
    response = post_n8n(api, envelope)
    assert response.status_code == 413, response.text
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}
    assert storage_files(storage_root) == [], "the payload must be refused before it is stored"


# --------------------------------------------------------------------------
# H. persistence protection
# --------------------------------------------------------------------------


def test_value_that_would_overflow_a_varchar_stops_at_request_validation(api: Api) -> None:
    """A 300-character sender_name can no longer reach ``varchar(255)``."""
    response = post_json(api, intake_payload(sender_name="n" * 300))
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    assert "sender_name" in detail
    for leak in ("character varying", "22001", "psycopg", "DataError", "SQL"):
        assert leak not in response.text, f"internal details leaked: {detail}"
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_invalid_source_email_id_never_reaches_the_idempotency_lookup(api: Api) -> None:
    response = post_json(api, intake_payload("a" * (SOURCE_EMAIL_ID_MAX + 1)))
    assert response.status_code == 422
    assert api.counts()["emails"] == 0


# --------------------------------------------------------------------------
# I. no side effects for rejected requests
# --------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["json", "multipart", "n8n"])
def test_rejected_requests_create_no_business_records(api: Api, transport: str, monkeypatch) -> None:
    storage_root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"phase9-storage-{uuid.uuid4().hex}")
    monkeypatch.setattr(routes, "get_settings", lambda: settings_with(document_storage_root=storage_root))
    monkeypatch.setattr(routes, "DocumentPipeline", _ExplodingPipeline)

    before = api.counts()
    if transport == "json":
        response = post_json(api, intake_payload(subject="x" * 1001))
    elif transport == "multipart":
        response = post_multipart(api, intake_payload(subject="x" * 1001))
    else:
        response = post_n8n(api, n8n_envelope(subject="x" * 1001))

    assert response.status_code == 422, response.text
    assert api.counts() == before, "a rejected request must not create Email/Document/work/attempt/audit rows"
    assert storage_files(storage_root) == [], "a rejected request must not store a file"
    assert before == {table.__tablename__: 0 for table in COUNTED_TABLES}


def test_multipart_metadata_is_validated_before_any_file_is_read(api: Api, monkeypatch) -> None:
    """A metadata violation cannot cause storage, processing or OCR."""
    storage_root = os.path.join(os.environ.get("TMPDIR", "/tmp"), f"phase9-storage-{uuid.uuid4().hex}")
    monkeypatch.setattr(routes, "get_settings", lambda: settings_with(document_storage_root=storage_root))
    monkeypatch.setattr(routes, "DocumentPipeline", _ExplodingPipeline)

    metadata = intake_payload()
    metadata["documents"][0]["document_name"] = "n" * 256
    response = post_multipart(api, metadata, files=[("files", ("remittance.pdf", VALID_PDF, "application/pdf"))])
    assert response.status_code == 422
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}
    assert storage_files(storage_root) == []


# --------------------------------------------------------------------------
# J. hard request-body cap (ASGI receive boundary, no Content-Length)
# --------------------------------------------------------------------------


def _chunked(payload: bytes, chunk: int = 512):
    for start in range(0, len(payload), chunk):
        yield payload[start : start + chunk]


def _raw_request(transport: str, metadata: dict) -> tuple[str, dict[str, str], bytes]:
    """Build the raw body and headers for one transport, without httpx framing."""
    if transport in {"json", "n8n"}:
        url = "/v1/ingest/n8n" if transport == "n8n" else "/v1/emails/intake"
        return url, {"content-type": "application/json"}, json.dumps(metadata).encode()
    boundary = "phase9b-hard-cap-boundary"
    body = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="metadata"\r\n\r\n'.encode()
        + json.dumps(metadata).encode()
        + f'\r\n--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="remittance.pdf"\r\nContent-Type: application/pdf\r\n\r\n'.encode()
        + VALID_PDF
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return "/v1/emails/intake", {"content-type": f"multipart/form-data; boundary={boundary}"}, body


@pytest.mark.parametrize("transport", ["json", "multipart", "n8n"], ids=["json", "multipart", "n8n"])
def test_body_without_content_length_over_the_hard_cap_returns_413(api: Api, monkeypatch, transport: str) -> None:
    """No Content-Length header: the shared ASGI receive boundary still refuses the body."""
    monkeypatch.setattr("app.main.get_settings", lambda: settings_with(max_request_body_bytes=1000))
    seen: dict = {}
    header_check = routes.enforce_request_body_size

    def spy(request):
        seen["content-length"] = request.headers.get("content-length")
        return header_check(request)

    monkeypatch.setattr(routes, "enforce_request_body_size", spy)

    metadata = n8n_envelope(subject="s" * 2000) if transport == "n8n" else intake_payload(subject="s" * 2000)
    url, headers, body = _raw_request(transport, metadata)
    assert len(body) > 1000, "the body must exceed the configured hard cap for this test to mean anything"

    response = api.client.post(url, content=_chunked(body), headers=headers)
    assert response.status_code == 413, response.text
    assert response.json()["detail"] == "request body exceeds 1000 bytes"
    assert seen.get("content-length") is None, "the request must genuinely lack Content-Length, so the header check cannot have produced this 413"
    assert api.counts() == {table.__tablename__: 0 for table in COUNTED_TABLES}


@pytest.mark.parametrize("transport", ["json", "multipart", "n8n"], ids=["json", "multipart", "n8n"])
def test_body_without_content_length_under_the_cap_succeeds(api: Api, transport: str) -> None:
    """Chunked requests without Content-Length are still accepted unchanged."""
    metadata = n8n_envelope() if transport == "n8n" else intake_payload()
    url, headers, body = _raw_request(transport, metadata)
    response = api.client.post(url, content=_chunked(body), headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["source_email_id"] == metadata["source_email_id"]


def test_derived_source_attachment_id_fits_the_column() -> None:
    """The derived attachment id cannot overflow varchar(255) under current rules."""
    longest_index = get_settings().max_documents_per_intake
    derived = routes.derived_source_attachment_id("a" * SOURCE_EMAIL_ID_MAX, longest_index)
    assert derived == f"{'a' * SOURCE_EMAIL_ID_MAX}:{longest_index}"
    assert len(derived) == SOURCE_EMAIL_ID_MAX + 1 + len(str(longest_index))
    assert len(derived) <= 255

    # The guard itself: a source id/index combination that could not fit is
    # refused at the derivation site instead of at the column.
    with pytest.raises(HTTPException) as caught:
        routes.derived_source_attachment_id("a" * 254, 10000)
    assert caught.value.status_code == 422


def test_max_source_email_id_derives_a_storable_attachment_id(api: Api) -> None:
    """End to end: a 250-character source id still derives a fitting id."""
    source = "a" * SOURCE_EMAIL_ID_MAX
    metadata = intake_payload(source, documents=[{"document_name": "remittance.pdf", "mime_type": "application/pdf"}])
    response = post_multipart(api, metadata)
    assert response.status_code == 201, response.text
    derived = response.json()["documents"][0]["source_attachment_id"]
    assert derived == f"{source}:1"
    assert len(derived) == SOURCE_EMAIL_ID_MAX + 2
    assert len(derived) <= 255


# --------------------------------------------------------------------------
# PostgreSQL: BEFORE / AFTER
# --------------------------------------------------------------------------


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_oversized_metadata_fails_with_22001_when_bypassed() -> None:
    """BEFORE: without the API boundary, the same value fails inside PostgreSQL."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    marker = f"pg-overflow-{uuid.uuid4().hex}"
    try:
        with factory() as session:
            session.add(
                Email(
                    source_email_id=marker,
                    received_at=datetime.now(timezone.utc),
                    sender_email="overflow@example.test",
                    sender_name="n" * 300,
                    status="RECEIVED",
                )
            )
            with pytest.raises(DataError) as excinfo:
                session.flush()
            assert getattr(excinfo.value.orig, "sqlstate", None) == "22001"
            session.rollback()
        with factory() as session:
            assert session.scalar(select(func.count()).select_from(Email).where(Email.source_email_id == marker)) == 0
    finally:
        engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_oversized_metadata_is_rejected_by_the_api_before_postgresql() -> None:
    """AFTER: the API refuses it with 422 and PostgreSQL is never asked to store it."""
    engine = create_engine(PG_URL)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    marker = f"pg-boundary-{uuid.uuid4().hex}"

    def override_get_db():
        session = factory()
        try:
            yield session
        finally:
            session.close()

    def counts() -> dict[str, int]:
        with engine.connect() as connection:
            return {
                table.__tablename__: connection.execute(text(f"SELECT count(*) FROM {table.__tablename__}")).scalar()
                for table in COUNTED_TABLES
            }

    app.dependency_overrides[get_db] = override_get_db
    try:
        before = counts()
        client = TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"}, raise_server_exceptions=False)
        response = client.post("/v1/emails/intake", json=intake_payload(marker, sender_name="n" * 300))
        assert response.status_code == 422, response.text
        detail = response.json()["detail"]
        assert "sender_name" in detail
        for leak in ("character varying", "22001", "psycopg", "DataError"):
            assert leak not in response.text, f"internal details leaked: {detail}"
        assert counts() == before, "no PostgreSQL row may be written for a rejected request"
        with engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM emails WHERE source_email_id = :marker"), {"marker": marker}).scalar() == 0
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.skipif(not PG_URL, reason="ENGINE_PG_TEST_DATABASE_URL is not set (PostgreSQL tests need a scratch database)")
def test_postgres_schema_has_not_been_changed() -> None:
    """The phase must not move any column boundary."""
    engine = create_engine(PG_URL)
    try:
        with engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT column_name, character_maximum_length FROM information_schema.columns "
                    "WHERE table_name IN ('emails', 'documents') AND column_name IN "
                    "('source_email_id', 'sender_email', 'sender_name', 'sender_company', 'subject', "
                    "'correlation_id', 'n8n_execution_id', 'document_name', 'mime_type', 'storage_reference', "
                    "'content_hash', 'source_attachment_id')"
                    " ORDER BY column_name"
                )
            ).fetchall()
        lengths = {row[0]: row[1] for row in rows}
        assert lengths["source_email_id"] == 255
        assert lengths["sender_email"] == 320
        assert lengths["sender_name"] == 255
        assert lengths["subject"] == 1000
        assert lengths["document_name"] == 255
        assert lengths["storage_reference"] == 1000
        assert lengths["content_hash"] == 128
    finally:
        engine.dispose()
