"""Steps: turn raw bytes into PIL page images, and pull the PDF text layer."""
from __future__ import annotations

import io
from typing import Optional

import pillow_heif
import pymupdf
from PIL import Image

from ..config import Settings
from ..models import IngestRejection, RejectionReason

pillow_heif.register_heif_opener()


def render_pdf_pages(raw: bytes, settings: Settings) -> list[Image.Image]:
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
        try:
            pages = []
            for page in doc:
                pix = page.get_pixmap(dpi=settings.pdf_dpi, alpha=False)
                pages.append(Image.frombytes("RGB", (pix.width, pix.height), pix.samples))
            return pages
        finally:
            doc.close()
    except IngestRejection:
        raise
    except Exception as exc:
        raise IngestRejection(RejectionReason.RENDER_FAILED, f"PDF render failed: {exc}")


def load_image(raw: bytes) -> Image.Image:
    """Load JPEG / PNG / HEIC. The result is fully decoded and detached from the buffer."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
            return img.copy()
    except Exception as exc:
        raise IngestRejection(RejectionReason.RENDER_FAILED, f"image load failed: {exc}")


def _is_image_backed(page) -> bool:
    """True when one picture covers most of the page: the text on top of it (if any) is an OCR layer
    made from those same pixels, so it is no more independent than the model's own reading."""
    page_area = page.rect.width * page.rect.height
    if page_area <= 0:
        return False
    for info in page.get_image_info():
        x0, y0, x1, y1 = info["bbox"]
        w = max(0.0, min(x1, page.rect.x1) - max(x0, page.rect.x0))
        h = max(0.0, min(y1, page.rect.y1) - max(y0, page.rect.y0))
        if w * h >= 0.8 * page_area:
            return True
    return False


def extract_text_layer(raw: bytes, settings: Settings) -> Optional[list[Optional[str]]]:
    """Per-page text for PDFs with a digital text layer, else None.

    The threshold applies to the document total: if all pages together have
    fewer than `min_text_chars` characters, the PDF is treated as a flat scan.
    """
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
        try:
            texts = []
            for page in doc:
                text = page.get_text("text").strip()
                texts.append("" if text and _is_image_backed(page) else text)   # OCR over a scan: ignore
        finally:
            doc.close()
    except Exception:
        return None  # text is a bonus; never fail ingest because of it
    if sum(len(t) for t in texts) < settings.min_text_chars:
        return None
    return [t or None for t in texts]
