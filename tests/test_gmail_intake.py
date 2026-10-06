from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.db.models import Document, DocumentWorkItem, Email
from app.db.session import Base, get_db
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures"


def make_session_factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'gmail-intake.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def configure_http(tmp_path, monkeypatch):
    monkeypatch.setenv("PROCESSING_MODE", "sync")
    monkeypatch.setenv("DOCUMENT_STORAGE_ROOT", str(tmp_path / "documents"))
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


def multipart_request(client, source_email_id, files):
    metadata = {
        "source_email_id": source_email_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "gmail-synthetic@example.test",
        "applicability": True,
        "documents": [{"document_name": name, "mime_type": mime, "source_attachment_id": f"attachment-{i}"} for i, (name, _, mime) in enumerate(files, 1)],
    }
    return client.post("/v1/emails/intake", data={"metadata": json.dumps(metadata)}, files=[("files", f) for f in files])


def test_gmail_style_intake_returns_terminal_results_and_no_work_items(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    try:
        pdf = (FIXTURES / "valid_remittance.pdf").read_bytes()
        with TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"}) as client:
            response = multipart_request(client, "gmail-sync-001", [("remittance.pdf", pdf, "application/pdf")])
        assert response.status_code == 201
        result = response.json()
        assert result["documents"][0]["decision"] is not None
        assert result["documents"][0]["processing_status"] == "COMPLETED"
        with factory() as db:
            assert db.query(DocumentWorkItem).count() == 0
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()


def test_gmail_duplicate_delivery_remains_idempotent(tmp_path, monkeypatch):
    factory = configure_http(tmp_path, monkeypatch)
    try:
        pdf = (FIXTURES / "valid_remittance.pdf").read_bytes()
        with TestClient(app, headers={"X-Intake-API-Key": "dev-intake-key"}) as client:
            first = multipart_request(client, "gmail-sync-idempotent", [("remittance.pdf", pdf, "application/pdf")])
            second = multipart_request(client, "gmail-sync-idempotent", [("remittance.pdf", pdf, "application/pdf")])
        assert first.status_code == second.status_code == 201
        assert second.json()["idempotent"] is True
        assert second.json()["email_id"] == first.json()["email_id"]
        with factory() as db:
            assert db.query(Email).count() == 1
            assert db.query(Document).count() == 1
            assert db.query(DocumentWorkItem).count() == 0
    finally:
        app.dependency_overrides.clear()
        get_settings.cache_clear()
