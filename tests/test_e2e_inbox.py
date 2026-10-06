from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db.models import Document, DocumentWorkItem, Email
from app.db.session import Base
from app.domain.enums import WorkItemStatus
from devtools.e2e_inbox.run import MAX_LOGICAL_FILE_BYTES, prepare_fixture, submit_fixture


FIXTURE_PDF = Path(__file__).parent / "fixtures" / "valid_remittance.pdf"


def write_fixture(root: Path, *, source_email_id: str = "fixture-test") -> Path:
    root.mkdir()
    (root / "email.json").write_text(
        json.dumps(
            {
                "source_email_id": source_email_id,
                "sender_email": "fixture@example.com",
                "received_at": datetime.now(timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    (root / "z.pdf").write_bytes(FIXTURE_PDF.read_bytes())
    return root


def session_factory(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fixture.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def test_fixture_metadata_and_deterministic_document_order(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixture")
    (fixture / "a.pdf").write_bytes(FIXTURE_PDF.read_bytes())
    prepared = prepare_fixture(fixture, run_id="run-1")
    assert [document.name for document in prepared.documents] == ["a.pdf", "z.pdf"]
    assert prepared.source_email_id == "fixture-test"
    assert prepared.correlation_id == "run-1"
    assert prepared.documents[0].content_hash == hashlib.sha256(FIXTURE_PDF.read_bytes()).hexdigest()
    assert prepared.documents[0].mime_type == "application/pdf"


def test_fixture_rejects_missing_metadata_and_unsupported_files(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    missing.mkdir()
    (missing / "invoice.pdf").write_bytes(FIXTURE_PDF.read_bytes())
    with pytest.raises(ValueError, match="missing fixture metadata"):
        prepare_fixture(missing)

    unsupported = tmp_path / "unsupported"
    unsupported.mkdir()
    (unsupported / "email.json").write_text(json.dumps({"source_email_id": "x", "sender_email": "x@example.com"}))
    (unsupported / "image.png").write_bytes(b"not an image")
    with pytest.raises(ValueError, match="unsupported document type"):
        prepare_fixture(unsupported)


def test_fixture_rejects_oversized_documents(tmp_path: Path) -> None:
    fixture = tmp_path / "oversized"
    fixture.mkdir()
    (fixture / "email.json").write_text(json.dumps({"source_email_id": "oversized", "sender_email": "x@example.com"}))
    (fixture / "large.pdf").write_bytes(b"%PDF-" + b"x" * (MAX_LOGICAL_FILE_BYTES + 1 - 5))
    with pytest.raises(ValueError, match="exceeds"):
        prepare_fixture(fixture)


def test_fixture_runner_uses_existing_pipeline_and_queue(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixture")
    factory = session_factory(tmp_path)
    settings = Settings(
        processing_mode="sync",
        document_storage_root=str(tmp_path / "documents"),
        llm_provider="mock",
    )
    prepared, response = submit_fixture(fixture, session_factory=factory, settings=settings, run_id="run-2")
    assert prepared.source_email_id == "fixture-test"
    assert response.idempotent is False
    with factory() as db:
        email = db.scalar(select(Email).where(Email.source_email_id == "fixture-test"))
        documents = list(db.scalars(select(Document).where(Document.email_id == email.id)))
        work_items = list(db.scalars(select(DocumentWorkItem).where(DocumentWorkItem.document_id == documents[0].id)))
        assert email is not None
        assert len(documents) == 1
        assert len(work_items) == 0
        assert documents[0].content_hash == hashlib.sha256(FIXTURE_PDF.read_bytes()).hexdigest()


def test_fixture_runner_creates_one_email_and_multiple_work_items(tmp_path: Path) -> None:
    fixture = write_fixture(tmp_path / "fixture")
    (fixture / "a.pdf").write_bytes(FIXTURE_PDF.read_bytes())
    factory = session_factory(tmp_path)
    settings = Settings(
        processing_mode="sync",
        document_storage_root=str(tmp_path / "documents"),
        llm_provider="mock",
    )
    _, response = submit_fixture(fixture, session_factory=factory, settings=settings, run_id="run-3")
    with factory() as db:
        email = db.get(Email, response.email_id)
        documents = list(db.scalars(select(Document).where(Document.email_id == email.id).order_by(Document.id)))
        work_items = list(
            db.scalars(
                select(DocumentWorkItem).where(
                    DocumentWorkItem.document_id.in_([document.id for document in documents])
                )
            )
        )
        assert email is not None
        assert len(documents) == 2
        assert len(work_items) == 0
