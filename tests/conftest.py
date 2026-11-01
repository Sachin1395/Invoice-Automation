"""Fixtures: every sample invoice is generated in code, so no binary files are checked in."""
import io
from dataclasses import replace

import pillow_heif
import pymupdf
import pytest
from PIL import Image, ImageDraw

from invoice_pipeline.config import Settings

pillow_heif.register_heif_opener()

INVOICE_TEXT = "INVOICE #1001  Acme Corp  Total due: 1,250.00 USD  Thank you for your business."


@pytest.fixture
def settings(tmp_path):
    s = Settings(
        inbox_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        rejected_dir=tmp_path / "rejected",
        pages_dir=tmp_path / "pages",
        db_path=tmp_path / "data" / "invoices.db",
        output_dir=tmp_path / "output",
        export_enabled=False,      # export tests switch it on via exp_settings
        extraction_enabled=False,  # ingest tests need no API; extraction tests use ex_settings
    )
    s.ensure_dirs()
    return s


@pytest.fixture
def ex_settings(settings):
    """Extraction on, instant retries, two fake models."""
    return replace(
        settings, extraction_enabled=True, model_chain=("model-a", "model-b"),
        throttle_seconds=0.0, backoff_base_s=0.0, breaker_threshold=2,
        validation_enabled=False,   # extraction tests stop after extraction
    )


@pytest.fixture
def val_settings(ex_settings):
    """Extraction + validation on, with a fixed 'today' so dates in the samples never go stale."""
    from datetime import date
    return replace(ex_settings, validation_enabled=True, today=date(2026, 10, 27))


def _photo(size=(1200, 1600), color=(240, 240, 235)) -> Image.Image:
    img = Image.new("RGB", size, color)
    d = ImageDraw.Draw(img)
    d.text((50, 50), "INVOICE 42", fill=(0, 0, 0))
    d.rectangle((40, 40, size[0] - 40, size[1] - 40), outline=(0, 0, 0), width=4)
    return img


def make_text_pdf(pages=1, text=INVOICE_TEXT) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((72, 72), f"{text} (page {i + 1})", fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def make_scanned_pdf() -> bytes:
    """A PDF whose only content is an image: no text layer."""
    buf = io.BytesIO()
    _photo().save(buf, "PNG")
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, stream=buf.getvalue())
    data = doc.tobytes()
    doc.close()
    return data


def make_encrypted_pdf() -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), INVOICE_TEXT)
    data = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="secret", owner_pw="owner")
    doc.close()
    return data


def make_rotated_jpeg() -> bytes:
    """Landscape pixels tagged with EXIF orientation 6 (viewer must rotate 90 degrees)."""
    img = _photo(size=(1600, 1000))
    exif = Image.Exif()
    exif[0x0112] = 6
    buf = io.BytesIO()
    img.save(buf, "JPEG", exif=exif)
    return buf.getvalue()


def make_transparent_png() -> bytes:
    img = Image.new("RGBA", (400, 300), (0, 0, 0, 0))  # fully transparent
    ImageDraw.Draw(img).rectangle((50, 50, 200, 150), fill=(255, 0, 0, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def make_heic() -> bytes:
    buf = io.BytesIO()
    _photo(size=(800, 600)).save(buf, "HEIF")
    return buf.getvalue()


def make_huge_jpeg() -> bytes:
    buf = io.BytesIO()
    _photo(size=(4000, 3000)).save(buf, "JPEG")
    return buf.getvalue()


def make_truncated_jpeg() -> bytes:
    buf = io.BytesIO()
    _photo().save(buf, "JPEG")
    data = buf.getvalue()
    return data[: len(data) // 2]


def fake_exe_bytes() -> bytes:
    return b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 2000


@pytest.fixture
def drop(settings):
    """drop('name.ext', bytes) -> writes a file into the inbox and returns its path."""
    def _drop(name: str, data: bytes):
        p = settings.inbox_dir / name
        p.write_bytes(data)
        return p
    return _drop


@pytest.fixture
def small_limits(settings):
    return replace(settings, max_pages=3)


def make_ocr_pdf(pages: int = 1) -> bytes:
    """What a scanner app with OCR produces: a picture of the page with invisible text laid over it."""
    buf = io.BytesIO()
    _photo().save(buf, "PNG")
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
        page.insert_text((72, 100), INVOICE_TEXT + f" OCR page {i + 1}", fontsize=11, render_mode=3)
    data = doc.tobytes()
    doc.close()
    return data


def make_mixed_pdf() -> bytes:
    """Page 1 genuinely digital, page 2 an OCR'd scan."""
    ocr = pymupdf.open(stream=make_ocr_pdf(), filetype="pdf")
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), INVOICE_TEXT + " digital page", fontsize=11)
    doc.insert_pdf(ocr)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def exp_settings(val_settings):
    """Everything on, including the output node."""
    return replace(val_settings, export_enabled=True)
