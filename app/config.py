from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

MAX_LOGICAL_FILE_BYTES = 20 * 1024 * 1024


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://engine:engine@localhost:5432/listing_engine"
    processing_mode: str = "sync"
    work_queue_lease_seconds: int = 300
    ocr_acceptable_score: float = 0.90
    ocr_fallback_min_score: float = 0.85
    retry_max_attempts: int = 3
    retry_base_delay_seconds: float = 1.0
    retry_max_delay_seconds: float = 60.0
    retry_jitter_seconds: float = 0.0
    ocr_provider_order: str = "TESSERACT"
    ocr_mock_provider_order: str = "OCR_PROVIDER_A,OCR_PROVIDER_B"
    ocr_tesseract_command: str = "tesseract"
    ocr_pdftoppm_command: str = "pdftoppm"
    ocr_language: str = "eng"
    ocr_timeout_seconds: float = 60.0
    ocr_max_pages: int = 10
    ocr_render_dpi: int = 200
    ocr_max_rendered_image_bytes: int = 10 * 1024 * 1024
    ocr_temp_root: str = "/tmp/listing-engine-ocr"
    intake_api_key: str = "dev-intake-key"
    #: Days a requirement may sit PENDING (since file creation) before the
    #: detection stage reports it overdue.  Overdue is surfaced only --
    #: findings, readout, review queue -- never a state or verdict change.
    requirement_overdue_days: int = 7
    max_logical_file_bytes: int = MAX_LOGICAL_FILE_BYTES
    #: Maximum number of documents in a single intake request, on either
    #: transport.  Each document becomes a stored file plus Email/Document/
    #: audit rows and (in sync mode) an OCR + LLM + decision run, so the
    #: per-file limit alone does not bound the cost of one request.  Enforced
    #: by the API layer with HTTP 422 rather than by the schema, so operators
    #: can raise it through configuration.
    max_documents_per_intake: int = 25
    #: Hard cap on the request body, checked against ``Content-Length`` before
    #: the body is parsed, so an oversized payload is never read into memory
    #: and never reaches base64 decoding or storage.  ``0`` derives the cap:
    #: ``max_documents_per_intake`` files of ``max_logical_file_bytes`` each,
    #: inflated 4/3 for the base64 n8n transport, plus 1 MiB of metadata
    #: headroom.  Violations are HTTP 413, matching the existing per-file
    #: size semantics.
    max_request_body_bytes: int = 0
    document_storage_root: str = "/tmp/listing-engine-documents"
    llm_provider: str = "mock"
    llm_base_url: str = ""
    llm_model: str = "mock-v1"
    llm_api_key: str = ""
    modal_proxy_token: str = ""
    llm_timeout_seconds: float = 30.0
    llm_max_tokens: int = 2000
    llm_temperature: float = 0.0
    llm_prompt_version: str = "v2"  # v2: listing taxonomy swap (2026-10-06)
    llm_schema_version: str = "v1"
    llm_response_format: str = "json_object"
    llm_include_temperature: bool = False

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    def effective_max_request_body_bytes(self) -> int:
        """Request-body ceiling actually enforced by the intake endpoints."""
        if self.max_request_body_bytes > 0:
            return self.max_request_body_bytes
        # n8n carries base64 (4/3 expansion) of up to
        # ``max_documents_per_intake`` files, each capped by
        # ``max_logical_file_bytes``; multipart carries raw bytes for the same
        # envelope.  1 MiB covers metadata for both.
        return self.max_documents_per_intake * self.max_logical_file_bytes * 4 // 3 + 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()
