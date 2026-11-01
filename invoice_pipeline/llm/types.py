"""Provider-neutral types and exceptions for the LLM layer."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Union


@dataclass(frozen=True)
class ImagePart:
    data: bytes
    mime_type: str = "image/jpeg"


@dataclass(frozen=True)
class TextPart:
    text: str


Part = Union[ImagePart, TextPart]


@dataclass
class LLMResponse:
    text: Optional[str]
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    finish_reason: Optional[str] = None


class LLMError(Exception):
    """Base class for LLM-layer failures."""


class FatalLLMError(LLMError):
    """Configuration or auth problem. Retrying can't help: abort the whole run."""


class NoUsableModelsError(FatalLLMError):
    """None of the configured models exist for this API key."""


@dataclass
class AllModelsUnavailable(LLMError):
    """Every model in the chain is dead, out of quota, or kept failing transiently.
    The file is fine; try again later."""
    reasons: dict = field(default_factory=dict)
    retry_after: Optional[datetime] = None

    def __str__(self) -> str:
        detail = "; ".join(f"{m}: {r}" for m, r in self.reasons.items()) or "no models left"
        return f"all models unavailable ({detail})"


@dataclass
class ExtractionFailed(LLMError):
    """A model answered, but never with output that validates against the schema."""
    model: str = ""
    raw_response: str = ""
    error: str = ""

    def __str__(self) -> str:
        return f"extraction failed on {self.model}: {self.error[:300]}"
