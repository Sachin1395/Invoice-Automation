"""LLM client interface and the Gemini implementation."""
from __future__ import annotations

from typing import Optional, Protocol

from pydantic import BaseModel

from .types import ImagePart, LLMResponse, Part, TextPart


class LLMClient(Protocol):
    def list_models(self) -> set[str]:
        """Model ids this key can use, without the 'models/' prefix."""

    def generate(
        self,
        *,
        model: str,
        system_prompt: str,
        parts: list[Part],
        schema: type[BaseModel],
        temperature: Optional[float],
    ) -> LLMResponse:
        """One structured-output call. Raises provider exceptions as-is."""


class GeminiClient:
    """Thin wrapper over the google-genai SDK."""

    def __init__(self, api_key: str, timeout_s: float = 90.0):
        from google import genai
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=int(timeout_s * 1000)),
        )

    def list_models(self) -> set[str]:
        names = set()
        for m in self._client.models.list():
            if m.name:
                names.add(m.name.removeprefix("models/"))
        return names

    def generate(self, *, model, system_prompt, parts, schema, temperature=None) -> LLMResponse:
        t = self._types
        contents = []
        for p in parts:
            if isinstance(p, ImagePart):
                contents.append(t.Part.from_bytes(data=p.data, mime_type=p.mime_type))
            elif isinstance(p, TextPart):
                contents.append(t.Part.from_text(text=p.text))
            else:  # pragma: no cover
                raise TypeError(f"unsupported part: {type(p)!r}")

        config_kwargs = dict(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=schema,
        )
        if temperature is not None:
            config_kwargs["temperature"] = temperature

        resp = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=t.GenerateContentConfig(**config_kwargs),
        )

        try:
            text = resp.text
        except Exception:  # no usable candidate (e.g. blocked)
            text = None
        usage = resp.usage_metadata
        finish = None
        if resp.candidates:
            fr = resp.candidates[0].finish_reason
            finish = getattr(fr, "name", None) or (str(fr) if fr is not None else None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            finish_reason=finish,
        )
