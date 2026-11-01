"""Turn provider exceptions into a small set of kinds the retry policy understands."""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional


class ErrorKind(str, Enum):
    RATE_LIMIT = "rate_limit"            # per-minute limit: wait and retry
    QUOTA_EXHAUSTED = "quota_exhausted"  # daily limit: stop using this model for now
    TRANSIENT = "transient"              # 5xx, timeouts, network: retry with backoff
    MODEL_NOT_FOUND = "model_not_found"  # retired or unavailable model
    AUTH = "auth"                        # bad key / permissions: abort the run
    BAD_REQUEST = "bad_request"          # deterministic 4xx: retrying the same call won't help
    UNKNOWN = "unknown"


_DAILY = re.compile(r"per\s?day|daily", re.IGNORECASE)
_RETRY_IN = re.compile(r"retry\s+in\s+(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)\s*s", re.IGNORECASE)
_NETWORK_NAMES = ("timeout", "connect", "network", "protocol", "remoteprotocol", "readerror")


def parse_retry_delay(text: str) -> Optional[float]:
    for pattern in (_RETRY_DELAY, _RETRY_IN):
        m = pattern.search(text)
        if m:
            return float(m.group(1))
    return None


def classify_error(exc: BaseException) -> tuple[ErrorKind, Optional[float]]:
    """Returns (kind, server_suggested_delay_seconds_or_None)."""
    text = str(exc)
    code = getattr(exc, "code", None)
    status = str(getattr(exc, "status", "") or "")

    if isinstance(code, int):
        if code in (401, 403) or status in ("UNAUTHENTICATED", "PERMISSION_DENIED"):
            return ErrorKind.AUTH, None
        if code == 404:
            return ErrorKind.MODEL_NOT_FOUND, None
        if code == 429:
            if _DAILY.search(text):
                return ErrorKind.QUOTA_EXHAUSTED, None
            return ErrorKind.RATE_LIMIT, parse_retry_delay(text)
        if code == 400:
            if re.search(r"api key", text, re.IGNORECASE):
                return ErrorKind.AUTH, None
            return ErrorKind.BAD_REQUEST, None
        if 500 <= code < 600:
            return ErrorKind.TRANSIENT, parse_retry_delay(text)
        if 400 <= code < 500:
            return ErrorKind.BAD_REQUEST, None

    name = type(exc).__name__.lower()
    if isinstance(exc, (TimeoutError, ConnectionError)) or any(n in name for n in _NETWORK_NAMES):
        return ErrorKind.TRANSIENT, None
    return ErrorKind.UNKNOWN, None
