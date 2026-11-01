"""Unit tests for the individual ingest steps."""
import pytest
from PIL import Image

from invoice_pipeline.ingest.normalize import normalize_image
from invoice_pipeline.ingest.render import extract_text_layer
from invoice_pipeline.ingest.validate import (
    check_image_integrity,
    check_pdf_integrity,
    detect_mime,
)
from invoice_pipeline.models import IngestRejection, RejectionReason

from conftest import (
    fake_exe_bytes, make_encrypted_pdf, make_heic, make_rotated_jpeg,
    make_scanned_pdf, make_text_pdf, make_transparent_png, make_truncated_jpeg,
)


def reason_of(fn, *args):
    with pytest.raises(IngestRejection) as e:
        fn(*args)
    return e.value.reason


def test_detect_mime_real_types(settings):
    assert detect_mime(make_text_pdf(), settings) == "application/pdf"
    assert detect_mime(make_rotated_jpeg(), settings) == "image/jpeg"
    assert detect_mime(make_transparent_png(), settings) == "image/png"
    assert detect_mime(make_heic(), settings) in ("image/heic", "image/heif")


def test_detect_mime_rejects_exe(settings):
    assert reason_of(detect_mime, fake_exe_bytes(), settings) == RejectionReason.UNSUPPORTED_TYPE


def test_pdf_integrity(settings, small_limits):
    assert check_pdf_integrity(make_text_pdf(pages=2), settings) == 2
    assert reason_of(check_pdf_integrity, make_encrypted_pdf(), settings) == RejectionReason.ENCRYPTED_PDF
    assert reason_of(check_pdf_integrity, make_text_pdf(pages=5), small_limits) == RejectionReason.TOO_MANY_PAGES
    assert reason_of(check_pdf_integrity, b"%PDF-1.7 garbage", settings) == RejectionReason.CORRUPT_FILE


def test_truncated_jpeg_is_corrupt():
    assert reason_of(check_image_integrity, make_truncated_jpeg()) == RejectionReason.CORRUPT_FILE


def test_text_layer_threshold(settings):
    layer = extract_text_layer(make_text_pdf(pages=2), settings)
    assert layer is not None and len(layer) == 2 and "INVOICE" in layer[0]
    assert extract_text_layer(make_scanned_pdf(), settings) is None
    assert extract_text_layer(make_text_pdf(text="hi"), settings) is None  # under 50 chars total


def test_exif_rotation_is_applied():
    import io
    img = Image.open(io.BytesIO(make_rotated_jpeg()))
    assert img.size == (1600, 1000)
    out = normalize_image(img, 2048)
    assert out.size == (1000, 1600)  # rotated to portrait


def test_transparency_flattened_to_white():
    import io
    out = normalize_image(Image.open(io.BytesIO(make_transparent_png())), 2048)
    assert out.mode == "RGB"
    assert out.getpixel((5, 5)) == (255, 255, 255)


def test_downscale_only():
    big = Image.new("RGB", (4000, 3000), "white")
    small = Image.new("RGB", (500, 400), "white")
    assert max(normalize_image(big, 2048).size) == 2048
    assert normalize_image(small, 2048).size == (500, 400)
