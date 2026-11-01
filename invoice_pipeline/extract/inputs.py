"""Build the model request from ingest output: page images (+ optional text layer)."""
from __future__ import annotations

from pathlib import Path

from ..llm.types import ImagePart, Part, TextPart
from ..models import PipelineState


def build_parts(state: PipelineState) -> list[Part]:
    """Pages are read from disk here; this is where JPEG bytes become the API payload."""
    total = len(state.pages)
    parts: list[Part] = [TextPart(f"This file has {total} page(s). Extract every invoice it contains.")]
    for page in state.pages:
        parts.append(TextPart(f"Page {page.page_no} of {total}:"))
        parts.append(ImagePart(Path(page.path).read_bytes(), page.mime))
        layer = state.text_layer
        if layer and page.page_no - 1 < len(layer) and layer[page.page_no - 1]:
            parts.append(TextPart(
                f"Text layer for page {page.page_no} (reading order may be scrambled; "
                f"the image is authoritative):\n{layer[page.page_no - 1]}"
            ))
    return parts
