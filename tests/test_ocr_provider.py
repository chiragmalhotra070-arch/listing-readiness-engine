from __future__ import annotations

import hashlib
import inspect
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.adapters.ocr import ExtractionError, ExtractionResult, OCRExtractionAdapter, OCRRequest, TesseractOCRProvider
from app.config import Settings
from app.db.models import AuditEvent, Document, ProcessingAttempt
from app.db.session import Base
from app.schemas import DocumentInput, EmailIntakeRequest
from app.services.pipeline import DocumentPipeline
from app.services.storage import LocalDocumentStorage


FIXTURE = Path(__file__).parent / "fixtures" / "scanned_remittance.pdf"


class FakeRunner:
    def __init__(self, *, stdout: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.tesseract_stdout = stdout

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "fake-pdftoppm":
            Path(f"{args[-1]}-1.png").write_bytes(b"synthetic-rendered-page")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout=self.tesseract_stdout, stderr="")


class TimeoutRunner(FakeRunner):
    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == "fake-pdftoppm":
            Path(f"{args[-1]}-1.png").write_bytes(b"synthetic-rendered-page")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])


def tsv(*words: tuple[str, float]) -> str:
    lines = ["level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"]
    for index, (word, confidence) in enumerate(words, start=1):
        lines.append(f"5\t1\t1\t1\t1\t{index}\t0\t0\t10\t10\t{confidence}\t{word}")
    return "\n".join(lines) + "\n"


def provider(tmp_path: Path, runner) -> tuple[TesseractOCRProvider, LocalDocumentStorage, str]:
    storage = LocalDocumentStorage(str(tmp_path / "documents"))
    stored = storage.store(FIXTURE.read_bytes())
    instance = TesseractOCRProvider(
        storage,
        tesseract_command="fake-tesseract",
        pdftoppm_command="fake-pdftoppm",
        temp_root=str(tmp_path / "ocr-temp"),
        runner=runner,
        which=lambda name: f"/fake/{name}",
    )
    return instance, storage, stored.storage_reference


def test_scanned_pdf_invokes_provider_with_explicit_subprocess_arguments(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=tsv(("REMITTANCE:", 96.0), ("REM-REAL-001", 94.0), ("CUSTOMER:", 95.0), ("CUST-001", 93.0)))
    ocr, _, storage_reference = provider(tmp_path, runner)
    result = ocr.extract(OCRRequest(storage_reference, "application/pdf", "scanned_remittance.pdf", page_count=1))
    assert result.provider == "tesseract"
    assert result.score == pytest.approx(0.945)
    assert result.pages_processed == 1
    assert result.provider_metadata["page_results"][0]["word_count"] == 4
    assert all(call_kwargs["shell"] is False for _, call_kwargs in runner.calls)
    assert all(isinstance(args, list) for args, _ in runner.calls)
    assert all("REMITTANCE" not in str(call_kwargs) for _, call_kwargs in runner.calls)
    assert not list((tmp_path / "ocr-temp").glob("ocr-*/*"))


def test_empty_output_is_low_quality_and_keeps_provider_metadata(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=tsv())
    ocr, _, storage_reference = provider(tmp_path, runner)
    result = ocr.extract(OCRRequest(storage_reference, "application/pdf", "scanned_remittance.pdf", page_count=1))
    assert result.text == ""
    assert result.score == 0.0
    assert result.warnings == ["OCR returned no recognized words"]
    assert result.provider_metadata["page_results"][0]["word_count"] == 0


def test_provider_timeout_is_retryable(tmp_path: Path) -> None:
    ocr, _, storage_reference = provider(tmp_path, TimeoutRunner())
    with pytest.raises(ExtractionError) as error:
        ocr.extract(OCRRequest(storage_reference, "application/pdf", "scanned_remittance.pdf", page_count=1))
    assert error.value.code == "OCR_TIMEOUT"
    assert error.value.retryable is True


def test_maximum_page_limit_is_enforced(tmp_path: Path) -> None:
    ocr, _, storage_reference = provider(tmp_path, FakeRunner())
    with pytest.raises(ExtractionError) as error:
        ocr.extract(OCRRequest(storage_reference, "application/pdf", "scanned_remittance.pdf", page_count=11))
    assert error.value.code == "OCR_PAGE_LIMIT"
    assert error.value.retryable is False


def test_provider_uses_storage_reference_and_not_uploadfile(tmp_path: Path) -> None:
    runner = FakeRunner(stdout=tsv(("TEXT", 95.0)))
    ocr, storage, storage_reference = provider(tmp_path, runner)
    assert "UploadFile" not in inspect.signature(TesseractOCRProvider.extract).return_annotation.__class__.__name__
    result = ocr.extract(OCRRequest(storage_reference, "application/pdf", "scanned_remittance.pdf", page_count=1))
    assert result.provider == "tesseract"
    assert storage.resolve(storage_reference).exists()
    assert storage_reference.startswith("local://documents/")
    assert all("/Users/" not in str(args) for args, _ in runner.calls)


def test_pipeline_persists_real_ocr_metadata_attempt_and_audit(tmp_path: Path) -> None:
    storage = LocalDocumentStorage(str(tmp_path / "documents"))
    stored = storage.store(FIXTURE.read_bytes())

    class FakeProvider:
        name = "fake-tesseract"

        def extract(self, request: OCRRequest) -> ExtractionResult:
            return ExtractionResult("Remittance: REM-REAL-001 Customer ID: CUST-001", {"remittance_number": "REM-REAL-001", "customer_id": "CUST-001"}, 0.93, provider=self.name, pages_processed=1, provider_metadata={"raw_confidence_average": 93.0})

    extractor = OCRExtractionAdapter(provider_order=["FAKE"], providers={"FAKE": FakeProvider()}, storage=storage)
    settings = Settings(document_storage_root=str(tmp_path / "documents"), ocr_provider_order="FAKE")
    pipeline = DocumentPipeline(settings, extractor=extractor)
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    payload = EmailIntakeRequest(source_email_id="ocr-pipeline", received_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), sender_email="synthetic@example.com", documents=[DocumentInput(document_name="scanned_remittance.pdf", mime_type="application/pdf", storage_reference=stored.storage_reference, content_hash=stored.content_hash, file_size_bytes=stored.size_bytes)])
    with session_factory() as db:
        result = pipeline.process_email(payload, db)
        document_id = result.documents[0].id
        attempt = db.scalar(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id, ProcessingAttempt.stage == "OCR"))
        events = list(db.scalars(select(AuditEvent).where(AuditEvent.document_id == document_id, AuditEvent.event_type == "STAGE_SUCCEEDED")))
        assert attempt is not None
        assert attempt.provider == "FAKE"
        assert attempt.quality_score == pytest.approx(0.93)
        assert attempt.result["provider_metadata"]["raw_confidence_average"] == 93.0
        assert any(event.message == "OCR completed" for event in events)


def test_application_has_no_apple_vision_dependency() -> None:
    source = "\n".join(path.read_text() for path in Path("app").rglob("*.py"))
    assert "Vision.framework" not in source
    assert "import Vision" not in source
