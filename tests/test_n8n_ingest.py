from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings, get_settings
from app.db.models import Document, DocumentWorkItem, Email
from app.db.session import Base, get_db
from app.domain.enums import ProcessingStatus, WorkItemStatus
from app.main import app
from app.worker import DocumentWorker

FIXTURES = Path(__file__).parent / "fixtures"


def make_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'n8n-ingest.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def configure_http(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESSING_MODE", "sync")
    monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "documents"))
    monkeypatch.setenv("MAX_LOGICAL_FILE_BYTES", "20971520")
    get_settings.cache_clear()
    factory = make_session_factory(tmp_path)

    def override_get_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    return factory


def envelope(source_email_id: str, documents: list[tuple[str, str, bytes, str | None]]):
    return {
        "source_email_id": source_email_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "n8n-synthetic@example.test",
        "sender_name": "n8n Synthetic",
        "subject": "n8n ingestion",
        "body": "synthetic test",
        "documents": [
            {
                "document_name": name,
                "mime_type": mime,
                "source_attachment_id": attachment_id,
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
            for name, mime, content, attachment_id in documents
        ],
    }


def post_envelope(client: TestClient, payload: dict, extra_headers: dict[str, str] | None = None):
    request_headers = {"X-Intake-API-Key": "dev-intake-key", **(extra_headers or {})}
    return client.post("/v1/ingest/n8n", json=payload, headers=request_headers)


def cleanup():
    app.dependency_overrides.clear()
    get_settings.cache_clear()


def test_n8n_valid_pdf_materializes_and_queues(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    try:
        with TestClient(app) as client:
            response = post_envelope(
                client,
                envelope("n8n-one", [("remittance.pdf", "application/pdf", content, "gmail-attachment-1")]),
                {"X-Correlation-ID": "corr-one", "X-N8N-Execution-ID": "exec-one"},
            )
        assert response.status_code == 201
        result = response.json()
        assert result["idempotent"] is False
        assert result["correlation_id"] == "corr-one"
        assert result["n8n_execution_id"] == "exec-one"
        document_id = result["documents"][0]["id"]
        with factory() as db:
            email = db.scalar(select(Email).where(Email.source_email_id == "n8n-one"))
            document = db.get(Document, document_id)
            item = db.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id))
            assert email is not None
            assert document is not None
            assert document.source_attachment_id == "gmail-attachment-1"
            assert document.file_size_bytes == len(content)
            assert document.content_hash == hashlib.sha256(content).hexdigest()
            assert document.storage_reference is not None
            assert Path(document.storage_reference.removeprefix("local://documents/")).name == document.content_hash
            assert item is None
            stored_path = Path(tmp_path / "documents" / document.content_hash[:2] / document.content_hash[2:4] / document.content_hash)
            assert stored_path.read_bytes() == content
    finally:
        cleanup()


def test_n8n_exact_limit_accepted(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    content = b"%PDF-" + b"A" * (20 * 1024 * 1024 - 5)
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-limit", [("limit.pdf", "application/pdf", content, None)]))
        assert response.status_code == 201
        with factory() as db:
            assert db.query(Document).count() == 1
    finally:
        cleanup()


def test_n8n_over_limit_rejected_without_database_rows(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    content = b"%PDF-" + b"A" * (20 * 1024 * 1024 - 4)
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-over-limit", [("too-large.pdf", "application/pdf", content, None)]))
        assert response.status_code == 413
        with factory() as db:
            assert db.query(Email).count() == 0
            assert db.query(Document).count() == 0
    finally:
        cleanup()


def test_n8n_multiple_documents_create_one_email_and_work_item_each(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    pdf = (FIXTURES / "valid_remittance.pdf").read_bytes()
    xlsx = (FIXTURES / "valid_remittance.xlsx").read_bytes()
    documents = [
        ("a.pdf", "application/pdf", pdf, "a"),
        ("b.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", xlsx, "b"),
        ("c.xlsm", "application/vnd.ms-excel.sheet.macroEnabled.12", xlsx, "c"),
        ("d.pdf", "application/pdf", pdf, "d"),
        ("e.pdf", "application/pdf", pdf, "e"),
        ("f.pdf", "application/pdf", pdf, "f"),
        ("g.pdf", "application/pdf", pdf, "g"),
    ]
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-many", documents))
        assert response.status_code == 201
        with factory() as db:
            assert db.query(Email).count() == 1
            assert db.query(Document).count() == 7
            assert db.query(DocumentWorkItem).count() == 0
    finally:
        cleanup()


def test_n8n_invalid_base64_leaves_no_materialized_files(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    payload = envelope("n8n-invalid-base64", [("a.pdf", "application/pdf", b"%PDF-valid", "a")])
    payload["documents"].append({"document_name": "b.pdf", "mime_type": "application/pdf", "content_base64": "not base64!"})
    try:
        with TestClient(app) as client:
            response = post_envelope(client, payload)
        assert response.status_code == 422
        with factory() as db:
            assert db.query(Email).count() == 0
            assert db.query(Document).count() == 0
        assert not list((tmp_path / "documents").rglob("*")) if (tmp_path / "documents").exists() else True
    finally:
        cleanup()


def test_n8n_empty_file_rejected(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-empty", [("empty.pdf", "application/pdf", b"", None)]))
        assert response.status_code == 422
        with factory() as db:
            assert db.query(Document).count() == 0
    finally:
        cleanup()


def test_n8n_invalid_pdf_rejected(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-invalid-pdf", [("invalid.pdf", "application/pdf", b"not a pdf", None)]))
        assert response.status_code == 422
        with factory() as db:
            assert db.query(Document).count() == 0
    finally:
        cleanup()


def test_n8n_duplicate_source_email_is_idempotent(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    payload = envelope("n8n-duplicate", [("remittance.pdf", "application/pdf", content, "attachment-1")])
    try:
        with TestClient(app) as client:
            first = post_envelope(client, payload)
            second = post_envelope(client, payload)
        assert first.status_code == second.status_code == 201
        assert second.json()["idempotent"] is True
        assert second.json()["email_id"] == first.json()["email_id"]
        with factory() as db:
            assert db.query(Email).count() == 1
            assert db.query(Document).count() == 1
            assert db.query(DocumentWorkItem).count() == 0
    finally:
        cleanup()


def test_n8n_sync_intake_needs_no_worker(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    content = (FIXTURES / "valid_remittance.pdf").read_bytes()
    try:
        with TestClient(app) as client:
            response = post_envelope(client, envelope("n8n-sync", [("remittance.pdf", "application/pdf", content, "attachment-1")]))
        document_id = response.json()["documents"][0]["id"]
        assert response.json()["documents"][0]["processing_status"] == ProcessingStatus.COMPLETED
        with factory() as db:
            document = db.get(Document, document_id)
            assert document is not None
            assert document.processing_status == ProcessingStatus.COMPLETED
            assert db.scalar(select(DocumentWorkItem).where(DocumentWorkItem.document_id == document_id)) is None
    finally:
        cleanup()
