from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CRMActionResult:
    success: bool
    message: str
    code: str | None = None
    external_reference: str | None = None


class CRMActionAdapter:
    def process(self, *, document_id: int, mock_profile: str | None = None, attempt_number: int = 1, idempotency_key: str) -> CRMActionResult:
        if mock_profile in {"crm_timeout_then_success"} and attempt_number == 1:
            return CRMActionResult(False, "temporary CRM action failure", "NETWORK_TIMEOUT")
        if mock_profile in {"crm_invalid_request"}:
            return CRMActionResult(False, "invalid CRM request", "HTTP_400")
        return CRMActionResult(True, f"mock CRM action completed for {idempotency_key}", external_reference=f"CRM-{document_id}")
