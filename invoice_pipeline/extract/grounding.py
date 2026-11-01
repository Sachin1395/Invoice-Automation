"""Grounding evidence: does a value the model returned actually appear in the PDF text layer?

This is raw evidence only. Extraction never changes confidence from it; the
validation node decides how much weight it gets. Scans have no text layer, so
every field is None there ("no evidence either way").

Two guards against "appears somewhere on the page" being too weak:
  * identifiers are compared case-sensitively (I vs l vs 1 matters);
  * a subtotal only counts as grounded if the page has a subtotal-style label,
    because a row's own Net Amount would otherwise confirm a made-up subtotal.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Optional

GROUNDED_FIELDS = (
    "invoice_number", "invoice_date", "seller_name", "seller_gstin", "buyer_name",
    "order_number", "transaction_id", "subtotal", "total_tax", "grand_total",
)
# plus "line_items": a list with one entry per row (True = every amount printed on that row is
# in the text, False = at least one is not, None = nothing checkable), or None without a text layer.

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SUBTOTAL_LABEL = re.compile(
    r"sub\s*-?\s*total|taxable\s+(?:value|amount)|total\s+(?:taxable|net)|net\s+total", re.IGNORECASE)


def _flat(s: str, lower: bool = True) -> str:
    """Keep letters and digits only, so wrapping, spacing and punctuation don't matter."""
    s = re.sub(r"[^0-9A-Za-z]", "", s)
    return s.lower() if lower else s


def _numbers(text: str) -> set:
    out = set()
    for tok in _NUMBER.findall(text):
        try:
            out.add(Decimal(tok.replace(",", "")))
        except InvalidOperation:
            pass
    return out


def _page_text(invoice: dict, text_layer: list) -> Optional[str]:
    pages = [p for p in (invoice.get("source_pages") or []) if 1 <= p <= len(text_layer)]
    if pages:
        texts = [text_layer[p - 1] for p in pages if text_layer[p - 1]]
    else:  # model gave no usable page numbers: search the whole document
        texts = [t for t in text_layer if t]
    return "\n".join(texts) if texts else None


class _Evidence:
    """Searchable view of the text that belongs to one invoice."""

    def __init__(self, blob: str):
        self.blob = blob
        self.flat = _flat(blob)
        self.flat_cs = _flat(blob, lower=False)
        self.numbers = _numbers(blob)

    def has_text(self, value, case_sensitive: bool = False) -> Optional[bool]:
        if not value:
            return None
        needle = _flat(str(value), lower=not case_sensitive)
        if not needle:
            return None
        return needle in (self.flat_cs if case_sensitive else self.flat)

    def has_amount(self, value) -> Optional[bool]:
        """Compares absolute values: the text layer shows '-33.90' as the number 33.90."""
        if value in (None, ""):
            return None
        try:
            return abs(Decimal(str(value))) in self.numbers
        except InvalidOperation:
            return False


def _evidence(invoice: dict, text_layer: Optional[list]) -> Optional[_Evidence]:
    if not text_layer:
        return None
    blob = _page_text(invoice, text_layer)
    return _Evidence(blob) if blob else None


def compute_grounding(invoice: dict, text_layer: Optional[list]) -> dict:
    result: dict = {f: None for f in GROUNDED_FIELDS}
    result["line_items"] = None
    ev = _evidence(invoice, text_layer)
    if ev is None:
        return result

    seller = invoice.get("seller") or {}
    buyer = invoice.get("buyer") or {}
    payment = invoice.get("payment") or {}
    result["invoice_number"] = ev.has_text(invoice.get("invoice_number"), case_sensitive=True)
    result["invoice_date"] = ev.has_text(invoice.get("invoice_date_raw") or _ddmmyyyy(invoice.get("invoice_date")))
    result["seller_name"] = ev.has_text(seller.get("name"))
    result["seller_gstin"] = ev.has_text(seller.get("gstin"), case_sensitive=True)
    result["buyer_name"] = ev.has_text(buyer.get("name"))
    result["order_number"] = ev.has_text(invoice.get("order_number"), case_sensitive=True)
    result["transaction_id"] = ev.has_text(payment.get("transaction_id"), case_sensitive=True)
    result["total_tax"] = ev.has_amount(invoice.get("total_tax"))
    result["grand_total"] = ev.has_amount(invoice.get("grand_total"))

    subtotal = ev.has_amount(invoice.get("subtotal"))
    if subtotal is True and not _SUBTOTAL_LABEL.search(ev.blob):
        subtotal = False  # the number exists, but nothing on the page is labelled as a subtotal
    result["subtotal"] = subtotal
    result["line_items"] = [_row_grounded(ev, li) for li in invoice.get("line_items") or []]
    return result


def _row_grounded(ev: _Evidence, row: dict) -> Optional[bool]:
    """True only if every amount printed on the row (net, discount, total, each tax) is in the text."""
    amounts = [row.get("net_amount"), row.get("discount"), row.get("line_total")]
    amounts += [t.get("amount") for t in row.get("taxes") or []]
    checks = [ev.has_amount(a) for a in amounts if a not in (None, "")]
    if not checks:
        return None
    return all(c is True for c in checks)


def _ddmmyyyy(iso: Optional[str]) -> Optional[str]:
    if not iso or len(iso) != 10:
        return None
    y, m, d = iso.split("-")
    return f"{d}{m}{y}"
