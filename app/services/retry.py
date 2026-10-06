from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.domain.enums import FailureType


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    exhausted: bool
    next_attempt_at: datetime | None


class RetryPolicy:
    def __init__(self, settings: Settings) -> None:
        self.max_attempts = settings.retry_max_attempts
        self.base_delay_seconds = settings.retry_base_delay_seconds
        self.max_delay_seconds = settings.retry_max_delay_seconds
        self.jitter_seconds = settings.retry_jitter_seconds

    def schedule(self, attempt_number: int) -> RetryDecision:
        if attempt_number >= self.max_attempts:
            return RetryDecision(True, True, None)
        current = datetime.now(timezone.utc)
        delay = min(self.max_delay_seconds, self.base_delay_seconds * (2 ** max(attempt_number - 1, 0)))
        delay += random.uniform(0, self.jitter_seconds) if self.jitter_seconds else 0
        return RetryDecision(True, False, current + timedelta(seconds=delay))


def classify_error(code: str) -> tuple[FailureType, bool]:
    retryable_codes = {"NETWORK_TIMEOUT", "HTTP_503", "HTTP_429", "PROVIDER_UNAVAILABLE"}
    if code in retryable_codes:
        return FailureType.RETRYABLE, True
    return FailureType.PERMANENT, False
