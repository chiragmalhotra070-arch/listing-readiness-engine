from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from app.services.storage import LocalDocumentStorage, StorageError


@dataclass(frozen=True)
class OCRRequest:
    storage_reference: str
    mime_type: str
    document_name: str
    page_count: int | None = None


@dataclass(frozen=True)
class ExtractionResult:
    text: str
    extracted_data: dict[str, object]
    score: float
    fallback_used: bool = False
    provider: str | None = None
    warnings: list[str] = field(default_factory=list)
    pages_processed: int | None = None
    provider_metadata: dict[str, Any] = field(default_factory=dict)


class ExtractionError(Exception):
    def __init__(self, message: str, *, code: str, retryable: bool) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class OCRProvider(Protocol):
    name: str

    def extract(self, request: OCRRequest) -> ExtractionResult:
        ...


class MockOCRProvider:
    def __init__(self, name: str) -> None:
        self.name = name

    def extract(self, *, document_name: str, mock_profile: str | None = None) -> ExtractionResult:
        profile = mock_profile or document_name.rsplit("/", 1)[-1].split(".", 1)[0]
        if profile in {"retryable_failure", "ocr_both_timeout"}:
            raise ExtractionError("temporary extraction service failure", code="NETWORK_TIMEOUT", retryable=True)
        if profile == "ocr_failover_success" and self.name == "OCR_PROVIDER_A":
            raise ExtractionError("OCR provider A timed out", code="NETWORK_TIMEOUT", retryable=True)
        if profile == "permanent_failure":
            raise ExtractionError("unsupported document fixture", code="INVALID_FILE", retryable=False)
        if profile == "ocr_poor_both":
            score = 0.60 if self.name == "OCR_PROVIDER_A" else 0.70
            return ExtractionResult("poor quality text", {"remittance_number": "REM-POOR-001", "customer_id": "CUST-001"}, score, provider=self.name)
        if profile == "ocr_failover_success" and self.name == "OCR_PROVIDER_B":
            return ExtractionResult("remittance text", {"remittance_number": "REM-FAILOVER-001", "customer_id": "CUST-001"}, 0.92, provider=self.name)
        if profile == "low_ocr_fallback_success":
            score = 0.88 if self.name == "OCR_PROVIDER_A" else 0.92
            return ExtractionResult("remittance text", {"remittance_number": "REM-LOW-001", "customer_id": "CUST-001"}, score, self.name == "OCR_PROVIDER_B", provider=self.name)
        if profile == "low_ocr_fallback_failure":
            score = 0.88 if self.name == "OCR_PROVIDER_A" else 0.84
            return ExtractionResult("unreadable remittance text", {"remittance_number": "REM-LOW-002", "customer_id": "CUST-001"}, score, self.name == "OCR_PROVIDER_B", provider=self.name)
        if profile in {"duplicate_remittance", "valid_remittance", "valid_remittance_2", "crm_timeout_then_success", "crm_invalid_request"}:
            number = {"duplicate_remittance": "REM-001", "valid_remittance": "REM-VALID-001", "valid_remittance_2": "REM-VALID-002", "crm_timeout_then_success": "REM-CRM-001", "crm_invalid_request": "REM-CRM-002"}[profile]
            return ExtractionResult("remittance text", {"remittance_number": number, "customer_id": "CUST-001"}, 0.96, provider=self.name)
        if profile == "unknown_customer":
            return ExtractionResult("remittance text", {"remittance_number": "REM-UNKNOWN-001", "customer_id": "UNKNOWN"}, 0.96, provider=self.name)
        if profile == "conflicting_customer":
            return ExtractionResult("remittance text", {"remittance_number": "REM-CONFLICT-001", "customer_id": "CUST-001", "sender_customer_id": "CUST-002"}, 0.96, provider=self.name)
        if profile == "not_applicable":
            return ExtractionResult("marketing document", {}, 0.96, provider=self.name)
        return ExtractionResult("document text", {}, 0.96, provider=self.name)


class TesseractOCRProvider:
    name = "tesseract"

    def __init__(
        self,
        storage: LocalDocumentStorage,
        *,
        tesseract_command: str = "tesseract",
        pdftoppm_command: str = "pdftoppm",
        language: str = "eng",
        timeout_seconds: float = 60.0,
        max_pages: int = 10,
        render_dpi: int = 200,
        max_rendered_image_bytes: int = 10 * 1024 * 1024,
        temp_root: str = "/tmp/listing-engine-ocr",
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        which: Callable[[str], str | None] = shutil.which,
    ) -> None:
        self.storage = storage
        self.tesseract_command = self._validate_executable(tesseract_command, which)
        self.pdftoppm_command = self._validate_executable(pdftoppm_command, which)
        self.language = self._validate_language(language)
        if timeout_seconds <= 0:
            raise ValueError("OCR timeout must be positive")
        if not 1 <= max_pages <= 100:
            raise ValueError("OCR maximum page count must be between 1 and 100")
        if not 72 <= render_dpi <= 300:
            raise ValueError("OCR render DPI must be between 72 and 300")
        if max_rendered_image_bytes <= 0:
            raise ValueError("OCR rendered image limit must be positive")
        self.timeout_seconds = timeout_seconds
        self.max_pages = max_pages
        self.render_dpi = render_dpi
        self.max_rendered_image_bytes = max_rendered_image_bytes
        self.temp_root = Path(temp_root)
        self.runner = runner

    @staticmethod
    def _validate_executable(command: str, which: Callable[[str], str | None]) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", command):
            raise ValueError("OCR executable must be a whitelisted executable name")
        if which(command) is None:
            raise ExtractionError(f"OCR executable is unavailable: {command}", code="PROVIDER_UNAVAILABLE", retryable=False)
        return command

    @staticmethod
    def _validate_language(language: str) -> str:
        if not re.fullmatch(r"[a-z]{3}(?:\+[a-z0-9_]+)*", language):
            raise ValueError("OCR language must use a whitelisted language code")
        return language

    def extract(self, request: OCRRequest) -> ExtractionResult:
        if request.mime_type != "application/pdf" and not request.document_name.lower().endswith(".pdf"):
            raise ExtractionError("Tesseract OCR currently supports PDF documents only", code="UNSUPPORTED_FILE", retryable=False)
        try:
            pdf_path = self.storage.resolve(request.storage_reference)
        except StorageError as error:
            raise ExtractionError(str(error), code="INVALID_FILE", retryable=False) from error
        page_count = request.page_count or 0
        if page_count <= 0:
            raise ExtractionError("OCR requires a known PDF page count", code="INVALID_FILE", retryable=False)
        if page_count > self.max_pages:
            raise ExtractionError("PDF exceeds OCR maximum page count", code="OCR_PAGE_LIMIT", retryable=False)

        self.temp_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ocr-", dir=self.temp_root) as temporary_directory:
            prefix = str(Path(temporary_directory) / "page")
            render_args = [self.pdftoppm_command, "-f", "1", "-l", str(page_count), "-r", str(self.render_dpi), "-png", str(pdf_path), prefix]
            try:
                render_result = self.runner(render_args, check=False, capture_output=True, text=True, timeout=self.timeout_seconds, shell=False)
            except subprocess.TimeoutExpired as error:
                raise ExtractionError("PDF rendering timed out", code="OCR_TIMEOUT", retryable=True) from error
            if render_result.returncode != 0:
                raise ExtractionError("PDF rendering failed", code="INVALID_FILE", retryable=False)

            images = sorted(Path(temporary_directory).glob("page-*.png"))
            if not images:
                raise ExtractionError("PDF rendering produced no pages", code="INVALID_FILE", retryable=False)
            text_parts: list[str] = []
            page_metadata: list[dict[str, Any]] = []
            confidence_values: list[float] = []
            for page_number, image_path in enumerate(images, start=1):
                if image_path.stat().st_size > self.max_rendered_image_bytes:
                    raise ExtractionError("rendered OCR image exceeds resource limit", code="OCR_RESOURCE_LIMIT", retryable=False)
                tesseract_args = [self.tesseract_command, str(image_path), "stdout", "-l", self.language, "--psm", "6", "tsv"]
                try:
                    result = self.runner(tesseract_args, check=False, capture_output=True, text=True, timeout=self.timeout_seconds, shell=False)
                except subprocess.TimeoutExpired as error:
                    raise ExtractionError("Tesseract OCR timed out", code="OCR_TIMEOUT", retryable=True) from error
                if result.returncode != 0:
                    raise ExtractionError("Tesseract OCR failed", code="PROVIDER_ERROR", retryable=True)
                page_text, page_confidences = self._parse_tsv(result.stdout)
                text_parts.append(page_text)
                confidence_values.extend(page_confidences)
                page_metadata.append({"page": page_number, "word_count": len(page_confidences), "raw_confidence_average": sum(page_confidences) / len(page_confidences) if page_confidences else 0.0})

        score = sum(confidence_values) / len(confidence_values) / 100 if confidence_values else 0.0
        return ExtractionResult(
            "\n".join(part for part in text_parts if part).strip(),
            {},
            score,
            provider=self.name,
            warnings=["OCR returned no recognized words"] if not confidence_values else [],
            pages_processed=len(page_metadata),
            provider_metadata={"language": self.language, "render_dpi": self.render_dpi, "page_results": page_metadata},
        )

    @staticmethod
    def _parse_tsv(tsv: str) -> tuple[str, list[float]]:
        lines = tsv.splitlines()
        if not lines:
            return "", []
        text_parts: list[str] = []
        confidences: list[float] = []
        for line in lines[1:]:
            columns = line.split("\t")
            if len(columns) < 12:
                continue
            text = columns[11].strip()
            try:
                confidence = float(columns[10])
            except ValueError:
                continue
            if text and confidence >= 0:
                text_parts.append(text)
                confidences.append(confidence)
        return " ".join(text_parts), confidences


class OCRExtractionAdapter:
    """Provider-ordered OCR facade with deterministic mock compatibility."""

    def __init__(
        self,
        provider_order: list[str] | None = None,
        *,
        mock_provider_order: list[str] | None = None,
        providers: dict[str, OCRProvider] | None = None,
        storage: LocalDocumentStorage | None = None,
        tesseract_options: dict[str, Any] | None = None,
    ) -> None:
        self.provider_order = provider_order or ["TESSERACT"]
        self.mock_provider_order = mock_provider_order or ["OCR_PROVIDER_A", "OCR_PROVIDER_B"]
        self.providers = providers or {}
        self.storage = storage
        self.tesseract_options = tesseract_options or {}

    def _provider(self, provider_name: str) -> OCRProvider:
        if provider_name in self.providers:
            return self.providers[provider_name]
        if provider_name == "TESSERACT":
            if self.storage is None:
                raise ExtractionError("OCR storage adapter is unavailable", code="PROVIDER_UNAVAILABLE", retryable=False)
            return TesseractOCRProvider(self.storage, **self.tesseract_options)
        raise ExtractionError(f"unknown OCR provider: {provider_name}", code="PROVIDER_UNAVAILABLE", retryable=False)

    def extract_with_failover(
        self,
        *,
        document_name: str,
        mock_profile: str | None = None,
        storage_reference: str | None = None,
        mime_type: str | None = None,
        page_count: int | None = None,
    ) -> tuple[ExtractionResult | None, list[tuple[str, ExtractionError]], list[tuple[str, ExtractionResult]]]:
        errors: list[tuple[str, ExtractionError]] = []
        results: list[tuple[str, ExtractionResult]] = []
        use_mock = mock_profile is not None or storage_reference is None
        provider_names = self.mock_provider_order if use_mock else self.provider_order
        request = OCRRequest(storage_reference=storage_reference or "", mime_type=mime_type or "application/pdf", document_name=document_name, page_count=page_count)
        for provider_name in provider_names:
            try:
                if use_mock:
                    result = MockOCRProvider(provider_name).extract(document_name=document_name, mock_profile=mock_profile)
                else:
                    result = self._provider(provider_name).extract(request)
            except ExtractionError as error:
                errors.append((provider_name, error))
                continue
            results.append((provider_name, result))
            if result.score >= 0.90:
                return result, errors, results
        if results:
            return results[-1][1], errors, results
        return None, errors, results
