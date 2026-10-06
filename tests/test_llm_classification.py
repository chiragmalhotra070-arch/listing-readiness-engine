from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.adapters.llm import LLMError, LLMRequest, ModalLLMProvider, MockLLMProvider, build_llm_provider
from app.db.models import AuditEvent, ProcessingAttempt
from app.domain.enums import DocumentType
from app.schemas import LLMAnalysisResult, PaymentAdviceFields
from app.services.classification import LLMDocumentClassifier
from app.services.validation import validate_required_fields


def request(profile: str, *, extracted_data: dict[str, object] | None = None) -> LLMRequest:
    return LLMRequest(
        document_name=f"{profile}.pdf",
        mime_type="application/pdf",
        extracted_text="synthetic financial document",
        extracted_data=extracted_data or {},
        metadata={"mock_profile": profile},
    )


def test_mock_provider_returns_valid_remittance() -> None:
    result = MockLLMProvider().analyze(request("valid_remittance", extracted_data={"remittance_number": "REM-001"}))
    assert isinstance(result, LLMAnalysisResult)
    assert result.document_type == DocumentType.REMITTANCE
    assert result.extracted_fields["remittance_number"] == "REM-001"
    assert result.provider == "mock"
    assert result.model == "mock-v1"


def test_modal_provider_uses_modal_token_and_exact_model() -> None:
    settings = type(
        "Settings",
        (),
        {
            "llm_provider": "modal",
            "llm_base_url": "https://example.modal.direct/v1",
            "llm_model": "deepseek-ai/DeepSeek-V4.1-Flash",
            "llm_api_key": "",
            "modal_proxy_token": "test-modal-token",
            "llm_timeout_seconds": 30,
            "llm_max_tokens": 2000,
            "llm_temperature": 0,
            "llm_prompt_version": "v1",
            "llm_schema_version": "v1",
            "llm_response_format": "json_object",
            "llm_include_temperature": False,
        },
    )

    provider = build_llm_provider(settings)

    assert isinstance(provider, ModalLLMProvider)
    assert provider.name == "modal"
    assert provider.api_key == "test-modal-token"
    assert provider.model == "deepseek-ai/DeepSeek-V4.1-Flash"


@pytest.mark.parametrize(
    ("profile", "document_type"),
    [("valid_invoice", DocumentType.INVOICE), ("payment_advice", DocumentType.PAYMENT_ADVICE)],
)
def test_mock_provider_supports_additional_document_types(profile: str, document_type: DocumentType) -> None:
    assert MockLLMProvider().analyze(request(profile)).document_type == document_type


@pytest.mark.parametrize("profile", ["unknown_document", "ambiguous_classification"])
def test_unknown_and_ambiguous_classification_are_safe(profile: str) -> None:
    result = MockLLMProvider().analyze(request(profile))
    assert result.document_type == DocumentType.UNKNOWN
    assert result.ambiguities or result.warnings


def test_missing_required_field_is_reported_without_inventing_value() -> None:
    result = MockLLMProvider().analyze(request("missing_required_field"))
    assert result.document_type == DocumentType.REMITTANCE
    assert "remittance_number" not in result.extracted_fields
    assert result.warnings


def test_conflicting_customer_candidates_are_preserved() -> None:
    result = MockLLMProvider().analyze(request("conflicting_customer_candidates"))
    assert result.customer_candidates.customer_id_candidates == ["CUST-001", "CUST-002"]


@pytest.mark.parametrize(
    ("profile", "code", "retryable"),
    [("provider_timeout", "LLM_TIMEOUT", True), ("provider_rate_limit", "LLM_RATE_LIMIT", True), ("provider_unavailable", "LLM_UNAVAILABLE", True)],
)
def test_provider_failures_are_classified(profile: str, code: str, retryable: bool) -> None:
    with pytest.raises(LLMError) as error:
        MockLLMProvider().analyze(request(profile))
    assert error.value.code == code
    assert error.value.retryable is retryable


def test_malformed_and_empty_provider_results_require_review() -> None:
    for profile in ("malformed_provider_response", "empty_provider_response"):
        with pytest.raises(LLMError) as error:
            MockLLMProvider().analyze(request(profile))
        assert error.value.needs_review is True
        assert error.value.retryable is False


def test_classifier_normalizes_customer_candidates() -> None:
    classifier = LLMDocumentClassifier(type("Settings", (), {"llm_provider": "mock", "llm_model": "mock-v1", "llm_prompt_version": "v1", "llm_schema_version": "v1"})())
    result = classifier.classify(document_id=1, document_name="conflicting_customer_candidates.pdf", mime_type="application/pdf", extracted_text="", extracted_data={}, mock_profile="conflicting_customer_candidates")
    assert "customer_id" not in result.extracted_data
    assert "sender_customer_id" not in result.extracted_data
    assert result.customer_candidates.customer_id_candidates == ["CUST-001", "CUST-002"]
    assert result.prompt_version == "v1"
    assert result.schema_version == "v1"


def test_credit_note_accepts_canonical_note_fields() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"
        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.CREDIT_NOTE, extracted_fields={"note_number": "CN-001", "note_date": "2026-09-19", "adjustment_amount": 450.0, "currency": "USD"}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")
    result = LLMDocumentClassifier(type("Settings", (), {"llm_schema_version": "v1"})(), provider=Provider()).classify(document_id=1, document_name="credit-note.pdf", mime_type="application/pdf", extracted_text="Credit Amount: 450", extracted_data={})
    assert result.document_type == DocumentType.CREDIT_NOTE
    assert result.extracted_data["note_number"] == "CN-001"
    assert result.extracted_data["adjustment_amount"] == 450.0


def test_debit_note_uses_same_canonical_note_fields() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"
        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.DEBIT_NOTE, extracted_fields={"note_number": "DN-001", "note_date": "2026-09-19", "adjustment_amount": 125.0, "currency": "USD"}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")
    result = LLMDocumentClassifier(type("Settings", (), {"llm_schema_version": "v1"})(), provider=Provider()).classify(document_id=1, document_name="debit-note.pdf", mime_type="application/pdf", extracted_text="Debit Amount: 125", extracted_data={})
    assert result.document_type == DocumentType.DEBIT_NOTE
    assert set(result.extracted_data) <= {"note_number", "note_date", "reference_invoice", "adjustment_amount", "tax_amount", "total_amount", "currency"}


def test_noncanonical_note_fields_are_rejected() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"
        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.CREDIT_NOTE, extracted_fields={"credit_note_number": "CN-001", "credit_note_date": "2026-09-19", "amount": 450.0}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")
    with pytest.raises(LLMError) as error:
        LLMDocumentClassifier(type("Settings", (), {"llm_schema_version": "v1"})(), provider=Provider()).classify(document_id=1, document_name="credit-note.pdf", mime_type="application/pdf", extracted_text="", extracted_data={})
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"


def test_bank_statement_accepts_period_fields_and_supplements_statement_number() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"
        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.BANK_STATEMENT, extracted_fields={"statement_start_date": "2026-08-01", "statement_end_date": "2026-08-31", "opening_balance": 10000.0, "closing_balance": 12500.0, "currency": "EUR"}, supplemental_information=[{"name": "statement_number", "value": "STMT-001", "source": "document"}], confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")
    result = LLMDocumentClassifier(type("Settings", (), {"llm_schema_version": "v1"})(), provider=Provider()).classify(document_id=1, document_name="bank-statement.pdf", mime_type="application/pdf", extracted_text="", extracted_data={})
    assert result.extracted_data["statement_start_date"] == "2026-08-01"
    assert result.extracted_data["statement_end_date"] == "2026-08-31"
    assert {item["name"] for item in result.supplemental_information} == {"statement_number"}


def test_bank_statement_does_not_require_statement_number() -> None:
    assert validate_required_fields(DocumentType.BANK_STATEMENT, {"statement_start_date": "2026-08-01", "statement_end_date": "2026-08-31"}).valid


def test_llm_metadata_and_fields_enter_business_pipeline(client) -> None:
    response = client.post(
        "/v1/emails/intake",
        json={
            "source_email_id": "llm-pipeline-valid-invoice",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "sender_email": "sender@example.com",
            "applicability": True,
            "documents": [{"document_name": "valid_invoice.pdf", "mime_type": "application/pdf", "mock_profile": "valid_invoice"}],
        },
    )
    assert response.status_code == 201
    document = response.json()["documents"][0]
    assert document["decision"] == "READY_FOR_PROCESSING"
    assert document["business_document_type"] == "INVOICE"
    assert document["business_reference"] == "INV-LLM-001"
    assert document["evidence"]["classification"]["provider"] == "mock"
    assert document["evidence"]["classification"]["model"] == "mock-v1"
    assert document["evidence"]["classification"]["prompt_version"] == "v2"
    assert document["evidence"]["classification"]["schema_version"] == "v1"
    assert "customer_id" not in document["evidence"]["classification"]["extracted_fields"]
    assert document["evidence"]["classification"]["customer_candidates"] == {
        "customer_id_candidates": ["CUST-001"],
        "customer_name_candidates": [],
        "other_identifier_candidates": [],
    }

    attempts = client.get(f"/v1/documents/{document['id']}/attempts").json()
    classification = next(attempt for attempt in attempts if attempt["stage"] == "CLASSIFICATION")
    assert classification["provider"] == "mock"
    assert classification["result"]["schema_version"] == "v1"

    audit = client.get(f"/v1/documents/{document['id']}/audit").json()
    assert any(event["message"] == "classification completed" for event in audit)


def test_llm_cannot_bypass_duplicate_detection(client) -> None:
    payload = {
        "received_at": datetime.now(timezone.utc).isoformat(),
        "sender_email": "sender@example.com",
        "applicability": True,
        "documents": [{"document_name": "valid_invoice.pdf", "mime_type": "application/pdf", "mock_profile": "valid_invoice"}],
    }
    first = client.post("/v1/emails/intake", json={**payload, "source_email_id": "llm-duplicate-one"}).json()["documents"][0]
    second = client.post("/v1/emails/intake", json={**payload, "source_email_id": "llm-duplicate-two"}).json()["documents"][0]
    assert first["decision"] == "READY_FOR_PROCESSING"
    assert second["decision"] == "DUPLICATE"
    assert second["decision"] != "PROCESSED"


def test_llm_customer_conflict_is_review(client) -> None:
    response = client.post(
        "/v1/emails/intake",
        json={
            "source_email_id": "llm-conflict",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "sender_email": "sender@example.com",
            "applicability": True,
            "documents": [{"document_name": "conflicting_customer_candidates.pdf", "mime_type": "application/pdf", "mock_profile": "conflicting_customer_candidates"}],
        },
    )
    assert response.json()["documents"][0]["decision"] == "NEEDS_REVIEW"


def test_invoice_canonical_and_supplemental_information_are_accepted_and_separated() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(
                document_type=DocumentType.INVOICE,
                extracted_fields={"invoice_number": "INV-001", "sales_tax": 10.0, "total_amount": 125.0, "currency": "USD"},
                supplemental_information=[
                    {"name": "payment_terms", "value": "Net 30", "source": "document"},
                    {"name": "order_date", "value": "2021-01-01", "source": "document"},
                ],
                confidence=0.95,
                provider=self.name,
                model=self.model,
                prompt_version="v1",
                schema_version="v1",
            )

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    result = LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert result.extracted_data == {"invoice_number": "INV-001", "tax_amount": 10.0, "total_amount": 125.0, "currency": "USD"}
    assert result.supplemental_information == [
        {"name": "payment_terms", "value": "Net 30", "source": "document"},
        {"name": "order_date", "value": "2021-01-01", "source": "document"},
    ]


def test_invoice_tax_aliases_normalize_to_canonical_field() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def __init__(self, alias):
            self.alias = alias

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields={self.alias: 10.0}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    for alias in ("tax", "sales_tax"):
        result = LLMDocumentClassifier(settings, provider=Provider(alias)).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
        assert result.extracted_data == {"tax_amount": 10.0}
        assert result.supplemental_information == []


def test_invoice_conflicting_canonical_and_alias_values_require_review() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields={"tax_amount": 10.0, "tax": 11.0}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    with pytest.raises(LLMError) as error:
        LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    assert error.value.needs_review is True
    assert error.value.metadata["canonical_alias_conflict"] == {"alias": "tax", "canonical": "tax_amount"}


def test_invoice_canonical_invalid_type_and_unknown_extracted_field_still_fail() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def __init__(self, fields):
            self.fields = fields

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields=self.fields, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    for fields in ({"invoice_number": 123}, {"invoice_number": "INV-001", "unknown": "x"}):
        with pytest.raises(LLMError) as error:
            LLMDocumentClassifier(settings, provider=Provider(fields)).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
        assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"


def test_pipeline_persists_supplemental_information_without_using_it_for_decisions(tmp_path) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.config import Settings
    from app.db.models import Document
    from app.db.session import Base
    from app.schemas import DocumentInput, EmailIntakeRequest
    from app.services.pipeline import DocumentPipeline

    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(
                document_type=DocumentType.INVOICE,
                extracted_fields={"invoice_number": "INV-001", "total_amount": 125.0, "currency": "USD"},
                supplemental_information=[{"name": "order_number", "value": "ORD-001", "source": "document"}],
                customer_candidates={"customer_id_candidates": ["CUST-001"]},
                confidence=0.95,
                provider=self.name,
                model=self.model,
                prompt_version="v1",
                schema_version="v1",
            )

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        settings = Settings(llm_provider="mock", llm_model="mock-v1", document_storage_root=str(tmp_path / "documents"))
        pipeline = DocumentPipeline(settings, classifier=LLMDocumentClassifier(settings, provider=Provider()))
        payload = EmailIntakeRequest(source_email_id="supplemental-persist", received_at=datetime.now(timezone.utc), sender_email="synthetic@example.com", applicability=True, documents=[DocumentInput(document_name="invoice.pdf", mime_type="application/pdf", mock_profile="valid_invoice")])
        response = pipeline.process_email(payload, db)
        document = db.get(Document, response.documents[0].id)
        assert response.documents[0].decision == "READY_FOR_PROCESSING"
        assert document is not None
        assert document.evidence["classification"]["supplemental_information"] == [{"name": "order_number", "value": "ORD-001", "source": "document"}]
        assert document.evidence["classification"]["assessment"] is None
        assert "assessment" not in document.extracted_data
        assert document.extracted_data["invoice_number"] == "INV-001"
        assert "order_number" not in document.extracted_data
        assert document.customer_id is not None
        assert document.duplicate is False
        assert document.duplicate_of_document_id is None


def test_multiple_transactions_assessment_is_valid() -> None:
    assessment = {"category": "MULTIPLE_TRANSACTIONS_FOUND", "summary": "The document contains multiple remittance transactions.", "evidence": ["Two payment references are present", "Two payment amounts are present", "No single canonical remittance record is established"]}
    body = valid_provider_body(document_type="REMITTANCE", assessment=assessment)
    result = real_provider(lambda request, timeout: FakeHTTPResponse(body)).analyze(request("compound-remittance"))
    assert result.assessment is not None
    assert result.assessment.category == "MULTIPLE_TRANSACTIONS_FOUND"
    assert len(result.assessment.evidence) == 3


def test_existing_customer_assessment_categories_remain_valid() -> None:
    for category in ("CLEAR_SINGLE_CUSTOMER", "CUSTOMER_ID_REQUIRES_VERIFICATION", "MULTIPLE_CUSTOMERS_FOUND", "NO_CUSTOMER_IDENTIFIER", "CONFLICTING_CUSTOMER_IDENTIFIERS", "CUSTOMER_NOT_IDENTIFIED"):
        body = valid_provider_body(assessment={"category": category, "summary": "Observed document condition.", "evidence": ["Document evidence"]})
        result = real_provider(lambda request, timeout, body=body: FakeHTTPResponse(body)).analyze(request("assessment-category"))
        assert result.assessment is not None
        assert result.assessment.category == category


def test_assessment_omitted_is_backward_compatible() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields={"invoice_number": "INV-001"}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    result = LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert result.assessment is None


def test_supplemental_information_omitted_is_backward_compatible() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields={"invoice_number": "INV-001"}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    result = LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert result.supplemental_information == []


def test_valid_assessment_is_accepted_and_persisted_in_classification_result() -> None:
    class Provider:
        name = "openai_compatible"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(
                document_type=DocumentType.INVOICE,
                extracted_fields={"invoice_number": "INV-001"},
                assessment={"category": "NO_CUSTOMER_IDENTIFIER", "summary": "The invoice contains no customer identifier.", "evidence": ["No customer ID is present"]},
                confidence=0.9,
                provider=self.name,
                model=self.model,
                prompt_version="v1",
                schema_version="v1",
            )

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    result = LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="invoice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert result.assessment == {"category": "NO_CUSTOMER_IDENTIFIER", "summary": "The invoice contains no customer identifier.", "evidence": ["No customer ID is present"]}
    assert "assessment" not in result.extracted_data


@pytest.mark.parametrize(
    "assessment",
    [
        {"category": "UNSUPPORTED", "summary": "Observed", "evidence": []},
        {"category": "NO_CUSTOMER_IDENTIFIER", "summary": "", "evidence": []},
        {"category": "NO_CUSTOMER_IDENTIFIER", "summary": "Observed", "evidence": [123]},
        {"category": "NO_CUSTOMER_IDENTIFIER", "summary": "Observed", "evidence": [], "extra": "forbidden"},
    ],
)
def test_invalid_assessment_is_rejected(assessment) -> None:
    import json
    body = valid_provider_body(assessment=assessment)
    with pytest.raises(LLMError) as error:
        real_provider(lambda request, timeout: FakeHTTPResponse(body)).analyze(request("assessment-invalid"))
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    assert error.value.needs_review is True


def test_provider_switch_boundary_keeps_classifier_contract() -> None:
    class AlternateProvider:
        name = "alternate"
        model = "alternate-v1"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(document_type=DocumentType.INVOICE, extracted_fields={"invoice_number": "ALT-001"}, confidence=0.9, provider=self.name, model=self.model, prompt_version="v1", schema_version="v1")

    settings = type("Settings", (), {"llm_provider": "mock", "llm_model": "mock-v1", "llm_prompt_version": "v1", "llm_schema_version": "v1"})()
    classifier = LLMDocumentClassifier(settings, provider=AlternateProvider())
    result = classifier.classify(document_id=1, document_name="any.pdf", mime_type="application/pdf", extracted_text="", extracted_data={})
    assert result.document_type == DocumentType.INVOICE
    assert result.extracted_data["invoice_number"] == "ALT-001"
    assert result.provider == "alternate"

class FakeHTTPResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self) -> bytes:
        return self.body


def valid_provider_body(**overrides: object) -> bytes:
    result = {
        "document_type": "REMITTANCE",
        "extracted_fields": {"remittance_number": "REM-REAL-001", "total_amount": 100.0, "currency": "USD"},
        "customer_candidates": {"customer_id_candidates": ["CUST-001"]},
        "evidence": {"source": "synthetic"},
        "confidence": 0.97,
        "ambiguities": [],
        "warnings": [],
        "assessment": {"category": "CLEAR_SINGLE_CUSTOMER", "summary": "The document contains one clear customer candidate.", "evidence": ["Customer information is consistent"]},
    }
    result.update(overrides)
    return __import__("json").dumps({"id": "req-test-001", "choices": [{"message": {"content": __import__("json").dumps(result)}}], "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}}).encode()


def real_provider(opener):
    from app.adapters.llm import OpenAICompatibleLLMProvider

    return OpenAICompatibleLLMProvider(base_url="https://api.experientiallabs.ai/v1", api_key="not-printed-test-key", model="gpt-5.6-luna", timeout_seconds=2, max_tokens=2000, temperature=0, prompt_version="v1", schema_version="v1", opener=opener)


def test_openai_compatible_request_uses_system_user_boundary_and_json_mode() -> None:
    captured = {}

    def opener(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeHTTPResponse(valid_provider_body())

    result = real_provider(opener).analyze(request("real-remittance", extracted_data={"remittance_number": "REM-PARSER-001"}))
    import json

    payload = json.loads(captured["request"].data)
    assert payload["model"] == "gpt-5.6-luna"
    assert payload["response_format"] == {"type": "json_object"}
    assert [message["role"] for message in payload["messages"]] == ["system", "user"]
    assert "untrusted data" in payload["messages"][0]["content"]
    assert "BEGIN_UNTRUSTED_DOCUMENT_DATA" in payload["messages"][1]["content"]
    assert "Authorization" in captured["request"].headers
    assert result.provider_request_id == "req-test-001"
    assert result.raw_metadata["usage"]["total_tokens"] == 20
    assert result.raw_metadata["normalized_result_size_bytes"] > 0
    assert result.raw_metadata["latency_ms"] >= 0


def test_prompt_explicitly_requires_complete_llm_analysis_contract() -> None:
    provider = real_provider(lambda request, timeout: FakeHTTPResponse(valid_provider_body()))
    system_prompt, _ = provider._prompts(request("prompt-contract"))

    required_fields = [
        "document_type (string enum from allowed_document_types)",
        "extracted_fields (object)",
        "customer_candidates (object)",
        "evidence (object)",
        "confidence (number from 0.0 through 1.0, always present)",
        "ambiguities (array of strings)",
        "assessment (object or null)",
        "warnings (array of strings)",
        "supplemental_information (array of objects)",
    ]
    for field in required_fields:
        assert field in system_prompt

    assert "customer_id_candidates (array of strings)" in system_prompt
    assert "customer_name_candidates (array of strings)" in system_prompt
    assert "other_identifier_candidates (array of strings)" in system_prompt
    assert "Every listed field is mandatory and must never be omitted" in system_prompt
    assert "Use an empty object or empty array when there is no information" in system_prompt
    assert "Do not invent values, identifiers, evidence, or confidence" in system_prompt
    assert "CLEAR_SINGLE_CUSTOMER" in system_prompt
    assert "CUSTOMER_ID_REQUIRES_VERIFICATION" in system_prompt
    assert "MULTIPLE_CUSTOMERS_FOUND" in system_prompt
    assert "NO_CUSTOMER_IDENTIFIER" in system_prompt
    assert "CONFLICTING_CUSTOMER_IDENTIFIERS" in system_prompt
    assert "CUSTOMER_NOT_IDENTIFIED" in system_prompt
    assert "up to five concise strings" in system_prompt
    assert "do not provide hidden chain-of-thought" in system_prompt
    assert "extracted_fields MUST contain only canonical fields belonging to the classified document type" in system_prompt
    assert "Assessor's Parcel Number to apn" in system_prompt
    assert "MUST go into supplemental_information as objects with exactly name, value, and source=document" in system_prompt
    assert "Do not duplicate canonical fields or canonical aliases into supplemental_information" in system_prompt
    assert "Never place customer_id, customer_name, sender identity, account identity, or any customer identifier in extracted_fields or supplemental_information" in system_prompt
    assert "Do not duplicate canonical fields or canonical aliases into supplemental_information" in system_prompt
    assert "All customer identifiers belong only in customer_candidates" in system_prompt
    assert "use UNKNOWN" in system_prompt
    assert '"document_type":"LISTING_AGREEMENT"' in system_prompt
    assert '"confidence":0.94' in system_prompt
    assert '"document_type":"UNKNOWN"' in system_prompt
    assert "The application supplies provider, model, prompt_version, schema_version" in system_prompt
    assert "Each listing document type adds only its own delta fields" in system_prompt
    assert "LISTING_AGREEMENT (residential listing agreement, CAR RLA)" in system_prompt
    assert "listing_price (number or null)" in system_prompt
    assert "CA_TDS (transfer disclosure statement, CAR TDS)" in system_prompt
    assert "disclosure_signed (boolean or null)" in system_prompt
    assert "CA_SPQ (seller property questionnaire, CAR SPQ)" in system_prompt
    assert "questionnaire_complete (boolean or null)" in system_prompt
    assert "CA_NHD (natural hazard disclosure report)" in system_prompt
    assert "hazards_disclosed (boolean or null)" in system_prompt
    assert "HOA_PACKAGE (HOA resale package: CC&Rs, budget, minutes)" in system_prompt
    assert "hoa_name (string or null)" in system_prompt
    assert "PRELIM_TITLE_REPORT (preliminary title report)" in system_prompt
    assert "title_company (string or null)" in system_prompt
    assert "SOLAR_AGREEMENT (solar ownership or financing agreement)" in system_prompt
    assert "ownership_type (string or null; owned, leased, or PPA)" in system_prompt
    assert "Do not emit keys outside the classified type's vocabulary" in system_prompt


def test_payment_advice_canonical_fields_validate_and_drive_required_field_check() -> None:
    fields = PaymentAdviceFields.model_validate({"payment_reference": "PAY-001", "payment_date": None, "amount": 125.0, "currency": "USD"})
    assert fields.payment_reference == "PAY-001"
    assert validate_required_fields(DocumentType.PAYMENT_ADVICE, fields.model_dump()).valid is True


@pytest.mark.parametrize(
    "provider_fields",
    [
        {"payment_advice_number": "PAY-001", "payment_amount": 125.0, "payment_date": None, "currency": "USD"},
        {"payment_reference": "PAY-001", "payment_advice_number": "PAY-002", "amount": 125.0, "currency": "USD"},
        {"payment_reference": "PAY-001", "amount": 125.0, "currency": "USD", "payment_method": "WIRE"},
    ],
)
def test_payment_advice_provider_style_or_ambiguous_fields_are_rejected(provider_fields: dict[str, object]) -> None:
    class Provider:
        name = "experiential-labs"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(
                document_type=DocumentType.PAYMENT_ADVICE,
                extracted_fields=provider_fields,
                customer_candidates={"customer_id_candidates": [], "customer_name_candidates": [], "other_identifier_candidates": []},
                evidence={},
                confidence=0.9,
                ambiguities=[],
                warnings=[],
                provider=self.name,
                model=self.model,
                prompt_version="v1",
                schema_version="v1",
            )

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    with pytest.raises(LLMError) as error:
        LLMDocumentClassifier(settings, provider=Provider()).classify(document_id=1, document_name="payment-advice.pdf", mime_type="application/pdf", extracted_text="synthetic", extracted_data={})
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    assert error.value.needs_review is True


def test_payment_advice_missing_canonical_reference_is_not_normalized_or_accepted() -> None:
    fields = PaymentAdviceFields.model_validate({"payment_reference": None, "payment_date": None, "amount": 125.0, "currency": "USD"})
    result = validate_required_fields(DocumentType.PAYMENT_ADVICE, fields.model_dump())
    assert result.valid is False
    assert result.missing_fields == ("payment_reference",)


def test_document_field_validation_retains_safe_provider_metadata_only() -> None:
    class ProviderAfterLlmValidation:
        name = "experiential-labs"
        model = "gpt-5.6-luna"

        def analyze(self, request: LLMRequest) -> LLMAnalysisResult:
            return LLMAnalysisResult(
                document_type=DocumentType.REMITTANCE,
                extracted_fields={"total_amount": ["sensitive-value"], "unexpected_field": "secret-value"},
                confidence=0.94,
                provider=self.name,
                model=self.model,
                prompt_version="v1",
                schema_version="v1",
                provider_request_id="req-field-diagnostics-001",
                raw_metadata={
                    "http_status": 200,
                    "latency_ms": 123.4,
                    "usage": {"total_tokens": 42},
                    "normalized_result_size_bytes": 88,
                },
            )

    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    with pytest.raises(LLMError) as error:
        LLMDocumentClassifier(settings, provider=ProviderAfterLlmValidation()).classify(
            document_id=1,
            document_name="clear-remittance.pdf",
            mime_type="application/pdf",
            extracted_text="synthetic",
            extracted_data={},
        )

    metadata = error.value.metadata
    assert metadata["provider"] == "experiential-labs"
    assert metadata["model"] == "gpt-5.6-luna"
    assert metadata["provider_request_id"] == "req-field-diagnostics-001"
    assert metadata["http_status"] == 200
    assert metadata["usage"] == {"total_tokens": 42}
    diagnostics = metadata["document_field_validation"]
    assert diagnostics["schema"] == "RemittanceFields"
    assert diagnostics["received_top_level_fields"] == ["total_amount", "unexpected_field"]
    assert diagnostics["received_field_types"] == {"total_amount": "list", "unexpected_field": "str"}
    assert any(item["field_path"] == ["total_amount"] and item["received_type"] == "list" for item in diagnostics["validation_errors"])
    assert any(item["field_path"] == ["unexpected_field"] and item["error_type"] == "extra_forbidden" for item in diagnostics["validation_errors"])
    serialized = __import__("json").dumps(metadata)
    assert "sensitive-value" not in serialized
    assert "secret-value" not in serialized


@pytest.mark.parametrize(
    ("status", "code", "retryable", "needs_review"),
    [(401, "LLM_AUTHENTICATION_ERROR", False, False), (400, "LLM_INVALID_REQUEST", False, False), (413, "LLM_CONTEXT_LIMIT", False, True), (429, "LLM_RATE_LIMIT", True, False), (503, "LLM_UNAVAILABLE", True, False)],
)
def test_openai_compatible_http_errors_are_mapped(status: int, code: str, retryable: bool, needs_review: bool) -> None:
    from urllib.error import HTTPError

    def opener(request, timeout):
        raise HTTPError(request.full_url, status, "synthetic", {}, None)

    with pytest.raises(LLMError) as error:
        real_provider(opener).analyze(request("real-remittance"))
    assert error.value.code == code
    assert error.value.retryable is retryable
    assert error.value.needs_review is needs_review


def test_openai_compatible_timeout_and_network_failures_are_retryable() -> None:
    import urllib.error

    for failure in (TimeoutError("timeout"), urllib.error.URLError("offline")):
        def opener(request, timeout, failure=failure):
            raise failure

        with pytest.raises(LLMError) as error:
            real_provider(opener).analyze(request("real-remittance"))
        assert error.value.code == "LLM_TIMEOUT" if isinstance(failure, TimeoutError) else error.value.code == "LLM_UNAVAILABLE"
        assert error.value.retryable is True


def test_openai_compatible_rejects_malformed_http_json_empty_and_fenced_content() -> None:
    import json

    bodies = [b"not-json", b"", json.dumps({"choices": [{"message": {"content": "```json\\n{}\\n```"}}]}).encode()]
    for body in bodies:
        with pytest.raises(LLMError) as error:
            real_provider(lambda request, timeout, body=body: FakeHTTPResponse(body)).analyze(request("real-remittance"))
        assert error.value.code in {"LLM_MALFORMED_RESPONSE", "LLM_EMPTY_RESPONSE"}
        assert error.value.needs_review is True


def test_openai_compatible_rejects_schema_invalid_and_unsupported_document_type() -> None:
    import json

    unsupported_body = json.dumps({"choices": [{"message": {"content": json.dumps({"document_type": "UNSUPPORTED", "extracted_fields": {}, "confidence": 0.9})}}]}).encode()
    with pytest.raises(LLMError) as error:
        real_provider(lambda request, timeout: FakeHTTPResponse(unsupported_body)).analyze(request("real-remittance"))
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    assert error.value.needs_review is True

    invented_body = json.dumps({"choices": [{"message": {"content": json.dumps({"document_type": "REMITTANCE", "extracted_fields": {"not_allowed": "invented"}, "confidence": 0.9})}}]}).encode()
    provider = real_provider(lambda request, timeout: FakeHTTPResponse(invented_body))
    settings = type("Settings", (), {"llm_schema_version": "v1"})()
    with pytest.raises(LLMError) as error:
        LLMDocumentClassifier(settings, provider=provider).classify(document_id=1, document_name="real-remittance.pdf", mime_type="application/pdf", extracted_text="", extracted_data={})
    assert error.value.code == "LLM_SCHEMA_VALIDATION_ERROR"
    assert error.value.needs_review is True


def test_validation_failure_retains_safe_provider_metadata_without_response_content() -> None:
    import json

    class StatusResponse(FakeHTTPResponse):
        status = 200

    invalid_result = {"document_type": "REMITTANCE", "extracted_fields": {}, "confidence": 2.0, "unexpected": "secret-value"}
    body = json.dumps({"id": "req-diagnostic-001", "choices": [{"message": {"content": json.dumps(invalid_result)}}], "usage": {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}}).encode()
    with pytest.raises(LLMError) as error:
        real_provider(lambda request, timeout: StatusResponse(body)).analyze(request("diagnostic-remittance"))

    metadata = error.value.metadata
    assert metadata["http_status"] == 200
    assert metadata["provider_request_id"] == "req-diagnostic-001"
    assert metadata["usage"] == {"prompt_tokens": 21, "completion_tokens": 9, "total_tokens": 30}
    assert metadata["normalized_result_size_bytes"] > 0
    assert metadata["response_top_level_type"] == "dict"
    assert metadata["response_top_level_fields"] == ["choices", "id", "usage"]
    assert metadata["response_content_top_level_fields"] == ["confidence", "document_type", "extracted_fields", "unexpected"]
    diagnostics = metadata["validation_diagnostics"]["errors"]
    assert any(item["field_path"] == ["confidence"] and item["error_type"] == "less_than_equal" and item["received_type"] == "float" for item in diagnostics)
    assert any(item["field_path"] == ["unexpected"] and item["error_type"] == "extra_forbidden" for item in diagnostics)
    serialized = json.dumps(metadata)
    assert "secret-value" not in serialized
    assert "diagnostic-remittance" not in serialized
    assert "BEGIN_UNTRUSTED_DOCUMENT_DATA" not in serialized
    assert "Authorization" not in serialized


def test_pipeline_persists_safe_validation_diagnostics_only(tmp_path) -> None:
    import json

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.config import Settings
    from app.db.session import Base
    from app.schemas import DocumentInput, EmailIntakeRequest
    from app.services.pipeline import DocumentPipeline

    class FailingClassifier:
        def classify(self, **kwargs):
            raise LLMError("structured response mismatch", code="LLM_SCHEMA_VALIDATION_ERROR", retryable=False, needs_review=True, metadata={"http_status": 200, "provider_request_id": "req-safe-001", "latency_ms": 123.4, "usage": {"total_tokens": 10}, "normalized_result_size_bytes": 321, "response_top_level_type": "dict", "response_top_level_fields": ["confidence", "document_type"], "validation_diagnostics": {"error_count": 1, "errors": [{"field_path": ["confidence"], "error_type": "float_type", "expected": "number", "received_type": "str"}]}})

    settings = Settings(llm_provider="mock", llm_model="mock-v1", document_storage_root=str(tmp_path / "documents"))
    pipeline = DocumentPipeline(settings, classifier=FailingClassifier())
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    payload = EmailIntakeRequest(source_email_id="safe-diagnostic-persistence", received_at=datetime.now(timezone.utc), sender_email="synthetic@example.com", applicability=True, documents=[DocumentInput(document_name="valid_remittance.pdf", mime_type="application/pdf", mock_profile="valid_remittance")])
    with session_factory() as db:
        response = pipeline.process_email(payload, db)
        document_id = response.documents[0].id
        attempt = db.scalar(select(ProcessingAttempt).where(ProcessingAttempt.document_id == document_id, ProcessingAttempt.stage == "CLASSIFICATION"))
        event = db.scalar(select(AuditEvent).where(AuditEvent.document_id == document_id, AuditEvent.event_type == "NEEDS_REVIEW"))
        assert attempt is not None
        assert attempt.result["provider_request_id"] == "req-safe-001"
        assert attempt.result["validation_diagnostics"]["errors"][0]["field_path"] == ["confidence"]
        assert event is not None
        assert event.details["provider_metadata"]["normalized_result_size_bytes"] == 321
        assert "secret" not in json.dumps(attempt.result)
