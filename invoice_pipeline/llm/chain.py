"""Model fallback chain: throttling, retries, backoff, dead/exhausted tracking.

One ModelChain lives for one run, so a model found dead (404) or out of daily
quota is skipped for the remaining files instead of failing on every one.
"""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from pydantic import BaseModel

from .. import events
from ..config import Settings
from .client import LLMClient
from .errors import ErrorKind, classify_error
from .types import (
    AllModelsUnavailable,
    ExtractionFailed,
    FatalLLMError,
    NoUsableModelsError,
    Part,
    TextPart,
)

log = logging.getLogger("invoice_pipeline.llm")


@dataclass
class ChainResult:
    parsed: BaseModel
    raw_text: str
    model: str
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int
    attempts: int


class _Skip(Exception):
    """This model can't serve the request right now; try the next one."""
    def __init__(self, reason: str, cooldown: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.cooldown = cooldown  # model is struggling: rest it so later files don't pay the wait again


class _Invalid(Exception):
    """This model answered, but never with valid output."""
    def __init__(self, raw: str, error: str):
        super().__init__(error)
        self.raw = raw
        self.error = error


def _short(text: object, n: int = 300) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[: n - 1] + "…"


class ModelChain:
    def __init__(
        self,
        client: LLMClient,
        settings: Settings,
        *,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        rng: Optional[Callable[[], float]] = None,
        now: Optional[Callable[[], datetime]] = None,
    ):
        self.client = client
        self.s = settings
        # resolved at construction (not definition) so tests can patch time.sleep
        self._sleep = sleep or time.sleep
        self._monotonic = monotonic or time.monotonic
        self._rng = rng or random.random
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._last_call: Optional[float] = None
        self.dead: dict[str, str] = {}        # retired / not available
        self.exhausted: dict[str, str] = {}   # daily quota used up
        self.cooling: dict[str, float] = {}   # model -> monotonic time it may be tried again

    # --- model bookkeeping -------------------------------------------------
    def live_models(self) -> list[str]:
        now = self._monotonic()
        return [m for m in self.s.model_chain
                if m not in self.dead and m not in self.exhausted
                and self.cooling.get(m, 0.0) <= now]

    def probe(self) -> None:
        """Drop configured models this key can't use. Aborts the run if none remain."""
        try:
            available = self.client.list_models()
        except Exception as exc:
            kind, _ = classify_error(exc)
            if kind is ErrorKind.AUTH:
                raise FatalLLMError(f"API key rejected while listing models: {_short(exc)}") from exc
            log.warning("model probe failed (%s); assuming configured models exist", _short(exc))
            return
        for m in self.s.model_chain:
            if m not in available:
                self.dead[m] = "not available for this API key"
                log.warning("model %s is not available for this key; skipping it", m)
        if not self.live_models():
            hints = sorted(n for n in available if n.startswith("gemini") and "flash" in n)[:20]
            raise NoUsableModelsError(
                f"None of the configured models {list(self.s.model_chain)} is available. "
                f"Flash models your key can list: {hints}. Update model_chain in config.py."
            )

    # --- pacing --------------------------------------------------------------
    def _throttle(self) -> None:
        gap = self.s.throttle_seconds
        if gap > 0 and self._last_call is not None:
            wait = gap - (self._monotonic() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        self._last_call = self._monotonic()

    def _backoff(self, attempt: int, server_delay: Optional[float]) -> float:
        if server_delay is not None:
            return min(server_delay + 1.0, self.s.max_server_delay_s)
        base = min(self.s.backoff_cap_s, self.s.backoff_base_s * (2 ** (attempt - 1)))
        return base * (0.75 + 0.5 * self._rng())  # jitter

    def _retry_after(self) -> datetime:
        now = self._now()
        if not self.exhausted and self.cooling:
            return now + timedelta(seconds=self.s.model_cooldown_s)
        if self.exhausted:  # daily quota resets at midnight Pacific
            try:
                from zoneinfo import ZoneInfo
                pt = now.astimezone(ZoneInfo("America/Los_Angeles"))
                midnight = (pt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
                return midnight.astimezone(timezone.utc)
            except Exception:
                return now + timedelta(hours=1)
        return now + timedelta(minutes=5)

    # --- the call ------------------------------------------------------------
    def extract(self, system_prompt: str, parts: list[Part], schema: type[BaseModel]) -> ChainResult:
        live = self.live_models()
        if not live:
            raise AllModelsUnavailable(self._reasons(), self._retry_after())

        reasons: dict[str, str] = {}
        invalid: Optional[tuple[str, _Invalid]] = None
        for i, model in enumerate(live):
            is_last = i == len(live) - 1   # only the last resort gets the full retry budget
            try:
                return self._try_model(model, system_prompt, parts, schema, is_last)
            except _Skip as skip:
                reasons[model] = skip.reason
                if skip.cooldown:
                    self.cooling[model] = self._monotonic() + self.s.model_cooldown_s
                    log.warning("model %s resting for %.0fs", model, self.s.model_cooldown_s)
                log.warning("model %s skipped: %s", model, skip.reason)
                nxt = live[i + 1] if i + 1 < len(live) else None
                events.emit("EXTRACT", f"{model} is unavailable ({skip.reason.split(' (')[0]})"
                                       + (f"; switching to {nxt}" if nxt else ""), "w")
            except _Invalid as bad:
                invalid = invalid or (model, bad)
                reasons[model] = f"invalid output: {_short(bad.error, 120)}"
                log.warning("model %s returned invalid output: %s", model, _short(bad.error))

        if invalid:
            model, bad = invalid
            raise ExtractionFailed(model=model, raw_response=bad.raw, error=bad.error)
        raise AllModelsUnavailable({**self._reasons(), **reasons}, self._retry_after())

    def _reasons(self) -> dict[str, str]:
        now = self._monotonic()
        return {**{m: f"dead: {r}" for m, r in self.dead.items()},
                **{m: f"quota: {r}" for m, r in self.exhausted.items()},
                **{m: "resting after repeated errors" for m, t in self.cooling.items() if t > now}}

    def _try_model(self, model: str, system_prompt: str, parts: list[Part],
                   schema: type[BaseModel], is_last: bool = True) -> ChainResult:
        # Fail fast when a fallback is waiting; retry hard only on the last resort.
        max_retries = (self.s.max_transient_retries if is_last
                       else self.s.max_transient_retries_with_fallback)
        transient = 0
        parse_failures = 0
        attempts = 0
        note: Optional[str] = None
        while True:
            call_parts = parts + ([TextPart(note)] if note else [])
            self._throttle()
            attempts += 1
            started = self._monotonic()
            try:
                resp = self.client.generate(
                    model=model, system_prompt=system_prompt, parts=call_parts,
                    schema=schema, temperature=self.s.extraction_temperature,
                )
            except Exception as exc:
                kind, delay = classify_error(exc)
                if kind is ErrorKind.AUTH:
                    raise FatalLLMError(f"auth/permission error from {model}: {_short(exc)}") from exc
                if kind is ErrorKind.MODEL_NOT_FOUND:
                    self.dead[model] = _short(exc, 120)
                    raise _Skip("model not found (retired?)")
                if kind is ErrorKind.QUOTA_EXHAUSTED:
                    self.exhausted[model] = _short(exc, 120)
                    raise _Skip("daily quota exhausted")
                if kind in (ErrorKind.RATE_LIMIT, ErrorKind.TRANSIENT):
                    transient += 1
                    if transient > max_retries:
                        raise _Skip(f"{kind.value} persisted after {max_retries} retries "
                                    f"(last error: {_short(exc, 160)})", cooldown=True)
                    wait = self._backoff(transient, delay)
                    log.info("model %s %s; retry %d in %.1fs (%s)", model, kind.value, transient, wait,
                             _short(exc, 200))
                    events.emit("EXTRACT", f"{model} is busy ({'rate limit' if kind is ErrorKind.RATE_LIMIT else 'temporary error'})"
                                           f"; retrying in {wait:.0f}s", "w")
                    self._sleep(wait)
                    continue
                if kind is ErrorKind.BAD_REQUEST:
                    raise _Skip(f"request rejected: {_short(exc, 160)}")
                raise  # UNKNOWN: probably a bug; let it surface

            latency_ms = int((self._monotonic() - started) * 1000)
            raw = resp.text or ""
            try:
                if not raw.strip():
                    raise ValueError(f"empty response (finish_reason={resp.finish_reason})")
                parsed = schema.model_validate_json(raw)
                return ChainResult(parsed, raw, model, resp.input_tokens,
                                   resp.output_tokens, latency_ms, attempts)
            except ValueError as err:  # includes pydantic.ValidationError
                parse_failures += 1
                if parse_failures > self.s.parse_retries:
                    raise _Invalid(raw, str(err))
                note = (f"Your previous response was rejected: {_short(err, 1200)} "
                        "Return corrected JSON only, matching the schema.")
