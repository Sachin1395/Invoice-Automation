"""Schema normalization, error classification and grounding evidence."""
from decimal import Decimal

import pytest
from pydantic import ValidationError

from invoice_pipeline.extract.grounding import compute_grounding
from invoice_pipeline.ingest.render import extract_text_layer
from invoice_pipeline.llm.errors import ErrorKind, classify_error
from invoice_pipeline.profiles.india_gst.schema import ExtractionResult, Invoice, TaxLine

from conftest import make_scanned_pdf
from fakes import (
    BACKPACK, BACKPACK_TEXT, SAMPLE_RESULT, auth_error, bad_request_error, daily_quota_error, make_sample_invoice_pdf,
    not_found_error, rate_limit_error, sample_json, server_error,
)


# --- schema ------------------------------------------------------------------
def test_sample_normalizes():
    result = ExtractionResult.model_validate_json(sample_json())
    a, b = result.invoices
    assert a.invoice_date == "2026-09-03"        # 03.09.2026 is day-first
    assert a.currency == "INR"                   # 'inr' upper-cased
    assert a.line_items[0].unit_price == "4.24"  # ₹ stripped
    assert a.line_items[0].taxes[0].rate == "18"  # % stripped
    assert a.line_items[0].quantity is None
    assert b.grand_total == "468.00"
    assert b.source_pages == [2]


def test_sample_arithmetic_is_consistent():
    """The golden data mirrors a real invoice: net + tax = total, and line taxes sum to the footer."""
    b = ExtractionResult.model_validate_json(sample_json()).invoices[1]
    net = sum(Decimal(li.net_amount) for li in b.line_items)
    tax = sum(Decimal(t.amount) for li in b.line_items for t in li.taxes)
    assert tax == Decimal(b.total_tax)
    assert net + tax == Decimal(b.grand_total)


@pytest.mark.parametrize("raw,expected", [
    ("03.09.2026", "2026-09-03"), ("3/9/2026", "2026-09-03"), ("03-09-2026", "2026-09-03"),
    ("2026-09-03", "2026-09-03"), ("31.02.2026", None), ("September third", None),
])
def test_date_normalization(raw, expected):
    assert Invoice(invoice_date=raw).invoice_date == expected


def test_bad_amount_is_a_validation_error():
    with pytest.raises(ValidationError):
        Invoice(grand_total="about five hundred")


def test_unknown_tax_type_rejected():
    with pytest.raises(ValidationError):
        TaxLine(type="VAT", rate="5", amount="1")


def test_confidence_clamped():
    inv = Invoice.model_validate({"confidence": {"grand_total": 1.7, "currency": -2}})
    assert inv.confidence.grand_total == 1.0 and inv.confidence.currency == 0.0


# --- error classification --------------------------------------------------------
@pytest.mark.parametrize("exc,kind", [
    (daily_quota_error(), ErrorKind.QUOTA_EXHAUSTED),
    (rate_limit_error(), ErrorKind.RATE_LIMIT),
    (not_found_error(), ErrorKind.MODEL_NOT_FOUND),
    (server_error(503), ErrorKind.TRANSIENT),
    (server_error(500), ErrorKind.TRANSIENT),
    (auth_error(), ErrorKind.AUTH),
    (bad_request_error(), ErrorKind.BAD_REQUEST),
    (TimeoutError("slow"), ErrorKind.TRANSIENT),
    (ValueError("a bug"), ErrorKind.UNKNOWN),
])
def test_classify(exc, kind):
    assert classify_error(exc)[0] == kind


def test_retry_delay_parsed():
    assert classify_error(rate_limit_error(37.5)) == (ErrorKind.RATE_LIMIT, 37.5)


# --- grounding -------------------------------------------------------------------
def _invoices():
    return [i.model_dump(mode="json") for i in ExtractionResult.model_validate_json(sample_json()).invoices]


def test_grounding_true_for_values_in_text(settings):
    layer = extract_text_layer(make_sample_invoice_pdf(), settings)
    first, second = _invoices()
    g1 = compute_grounding(first, layer)
    assert g1["invoice_number"] and g1["invoice_date"] and g1["seller_name"]
    assert g1["seller_gstin"] and g1["buyer_name"] and g1["total_tax"] and g1["grand_total"]
    assert g1["order_number"] and g1["transaction_id"]
    assert g1["line_items"] == [True]
    assert g1["subtotal"] is None                # null value: nothing to ground
    g2 = compute_grounding(second, layer)
    assert g2["grand_total"] is True and g2["seller_name"] is True


def test_grounding_uses_the_invoices_own_pages(settings):
    """Invoice 1's number is on page 1 only; claiming it for page 2's invoice must not ground."""
    layer = extract_text_layer(make_sample_invoice_pdf(), settings)
    _, second = _invoices()
    second["invoice_number"] = "MKT-318400606"
    assert compute_grounding(second, layer)["invoice_number"] is False


def test_grounding_false_for_wrong_value(settings):
    layer = extract_text_layer(make_sample_invoice_pdf(), settings)
    first, _ = _invoices()
    first["grand_total"] = "6.00"
    first["seller"]["gstin"] = "29AAICA3918J1ZX"
    g = compute_grounding(first, layer)
    assert g["grand_total"] is False and g["seller_gstin"] is False


def test_grounding_none_without_text_layer(settings):
    assert extract_text_layer(make_scanned_pdf(), settings) is None
    assert set(compute_grounding(_invoices()[0], None).values()) == {None}


# --- CGST+SGST on one row, negative discount ------------------------------------
def test_backpack_invoice_schema_and_arithmetic():
    inv = Invoice.model_validate(BACKPACK)
    backpack, shipping = inv.line_items
    assert [t.type for t in backpack.taxes] == ["CGST", "SGST"]      # two taxes, ONE row
    assert shipping.discount == "33.90"                               # ₹ stripped; a discount is stored as a magnitude
    assert backpack.discount == "0.00"
    tax = sum(Decimal(t.amount) for li in inv.line_items for t in li.taxes)
    assert tax == Decimal(inv.total_tax) == Decimal("108.16")
    assert sum(Decimal(li.net_amount) for li in inv.line_items) + tax == Decimal(inv.grand_total)
    assert Decimal(shipping.unit_price) - Decimal(shipping.discount) == Decimal(shipping.net_amount)


def test_derived_subtotal_is_not_grounded():
    """No subtotal is printed. A model that copies the row's net amount into `subtotal` must be caught,
    even though 600.84 does appear on the page."""
    inv = Invoice.model_validate(BACKPACK).model_dump(mode="json")
    inv["subtotal"] = "600.84"
    g = compute_grounding(inv, [BACKPACK_TEXT])
    assert g["subtotal"] is False
    assert g["total_tax"] is True and g["grand_total"] is True       # printed in TOTAL row / payment box
    assert g["line_items"] == [True, True]
    assert g["transaction_id"] is True and g["order_number"] is True


def test_printed_subtotal_is_grounded():
    inv = Invoice.model_validate(BACKPACK).model_dump(mode="json")
    inv["subtotal"] = "600.84"
    with_subtotal = BACKPACK_TEXT + "\nSubtotal 600.84"
    assert compute_grounding(inv, [with_subtotal])["subtotal"] is True


def test_invented_row_amount_is_flagged():
    inv = Invoice.model_validate(BACKPACK).model_dump(mode="json")
    inv["line_items"][0]["net_amount"] = "650.84"
    assert compute_grounding(inv, [BACKPACK_TEXT])["line_items"] == [False, True]


def test_discount_sign_is_normalised():
    from invoice_pipeline.profiles.india_gst.schema import LineItem
    assert LineItem(discount="-₹33.90").discount == "33.90"
    assert LineItem(discount="52.18").discount == "52.18"
    assert LineItem(discount="-0.00").discount == "0.00"
    assert LineItem(discount=None).discount is None


# ---- OCR layers are not independent evidence ----------------------------------------------------
def test_ocr_layer_over_a_scan_is_not_treated_as_a_text_layer(settings):
    from conftest import make_mixed_pdf, make_ocr_pdf
    import pymupdf
    assert pymupdf.open(stream=make_ocr_pdf(), filetype="pdf")[0].get_text().strip()     # the text really is there
    assert extract_text_layer(make_ocr_pdf(pages=2), settings) is None
    mixed = extract_text_layer(make_mixed_pdf(), settings)
    assert mixed is not None and mixed[0] and mixed[1] is None                          # only the digital page counts


def test_scan_status_is_decided_per_invoice():
    from invoice_pipeline.nodes.validate import invoice_is_scan
    layer = ["digital text", None]
    assert invoice_is_scan({"source_pages": [2]}, layer) is True
    assert invoice_is_scan({"source_pages": [1]}, layer) is False
    assert invoice_is_scan({"source_pages": [1, 2]}, layer) is False
    assert invoice_is_scan({"source_pages": [1]}, None) is True
