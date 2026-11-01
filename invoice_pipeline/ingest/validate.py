"""Steps: size check, magic-byte type detection, integrity checks."""
from __future__ import annotations

import io
from pathlib import Path

import filetype
import pillow_heif
import pymupdf
from PIL import Image

from ..config import Settings
from ..models import IngestRejection, RejectionReason

pillow_heif.register_heif_opener()

PDF_MIME = "application/pdf"


def check_size(path: Path, settings: Settings) -> int:
    size = path.stat().st_size
    if size == 0:
        raise IngestRejection(RejectionReason.CORRUPT_FILE, "empty file")
    if size > settings.max_file_bytes:
        raise IngestRejection(
            RejectionReason.FILE_TOO_LARGE,
            f"{size / 1024 / 1024:.1f} MB exceeds {settings.max_file_mb} MB",
        )
    return size


def detect_mime(raw: bytes, settings: Settings) -> str:
    """Detect type from magic bytes; the file extension is never consulted."""
    kind = filetype.guess(raw)
    mime = kind.mime if kind else None
    if mime not in settings.allowed_mimes:
        raise IngestRejection(
            RejectionReason.UNSUPPORTED_TYPE, f"detected type: {mime or 'unknown'}"
        )
    return mime


def check_pdf_integrity(raw: bytes, settings: Settings) -> int:
    """Open the PDF for real. Returns the page count."""
    try:
        doc = pymupdf.open(stream=raw, filetype="pdf")
    except Exception as exc:
        raise IngestRejection(RejectionReason.CORRUPT_FILE, f"PDF won't open: {exc}")
    try:
        if doc.needs_pass or doc.is_encrypted:
            raise IngestRejection(RejectionReason.ENCRYPTED_PDF, "password protected")
        n = doc.page_count
        if n == 0:
            raise IngestRejection(RejectionReason.CORRUPT_FILE, "PDF has no pages")
        if n > settings.max_pages:
            raise IngestRejection(
                RejectionReason.TOO_MANY_PAGES, f"{n} pages exceeds {settings.max_pages}"
            )
        return n
    finally:
        doc.close()


def check_image_integrity(raw: bytes) -> None:
    """Fully decode the image; truncated or corrupt data raises here."""
    try:
        with Image.open(io.BytesIO(raw)) as img:
            img.load()
    except Exception as exc:
        raise IngestRejection(RejectionReason.CORRUPT_FILE, f"image won't decode: {exc}")
