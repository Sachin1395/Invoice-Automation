"""India GST extraction schema (v1).

This is the schema Gemini is asked to fill. Money values are plain decimal
strings, dates are ISO (YYYY-MM-DD), anything missing is null. The validation
node (later) is responsible for judging correctness; here we only normalize
formats so downstream code can rely on them.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field

_MONEY_JUNK = re.compile(r"[₹$€£,\s]|Rs\.?|INR|USD|EUR", re.IGNORECASE)


def _clean_money(v):
    """'₹1,250.50' -> '1250.50'. Raises ValueError when not a number, which makes
    the extraction layer re-prompt the model with the error."""
    if v is None:
        return None
    if isinstance(v, (int, float, Decimal)):
        v = str(v)
    s = _MONEY_JUNK.sub("", str(v)).strip()
    if s == "":
        return None
    try:
        Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a valid amount: {v!r}")
    return s


def _clean_discount(v):
    """A discount is always a reduction, but invoices (and model runs) disagree on printing it as 52.18
    or -52.18. Store the magnitude so the same invoice reads the same from every copy."""
    m = _clean_money(v)
    return None if m is None else m.lstrip("-+")


def _clean_rate(v):
    """'18%' -> '18'."""
    if v is None:
        return None
    s = str(v).replace("%", "").strip()
    if s == "":
        return None
    try:
        Decimal(s)
    except InvalidOperation:
        raise ValueError(f"not a valid tax rate: {v!r}")
    return s


_DATE_FORMATS = [
    re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T].*)?$"),  # ISO, optional time
    re.compile(r"^(\d{1,2})[./-](\d{1,2})[./-](\d{4})$"),  # Indian DD.MM.YYYY
]
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE_NAMED = re.compile(r"^(\d{1,2})[\s.-]+([A-Za-z]{3,9})[\s.,-]+(\d{4})")  # 13 Feb 2026


def _clean_date(v):
    """Normalize to ISO. Indian invoices are day-first. Unparseable -> None
    (the raw string is kept separately, so nothing is lost)."""
    if v is None:
        return None
    s = str(v).strip()
    try:
        m = _DATE_FORMATS[0].match(s)
        if m:
            y, mo, d = map(int, m.groups())
        else:
            m = _DATE_FORMATS[1].match(s)
            if m:
                d, mo, y = map(int, m.groups())
            else:
                m = _DATE_NAMED.match(s)  # "13 Feb 2026", "13-February-2026", and a trailing time is ignored
                if not m or m.group(2)[:3].lower() not in _MONTHS:
                    return None
                d, y = int(m.group(1)), int(m.group(3))
                mo = _MONTHS[m.group(2)[:3].lower()]
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _clamp01(v):
    if v is None:
        return None
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        raise ValueError(f"confidence must be a number, got {v!r}")


def _strip_upper(v):
    return None if v is None else (str(v).strip().upper() or None)


def _strip(v):
    return None if v is None else (str(v).strip() or None)


Money = Annotated[Optional[str], BeforeValidator(_clean_money)]
Discount = Annotated[Optional[str], BeforeValidator(_clean_discount)]
Rate = Annotated[Optional[str], BeforeValidator(_clean_rate)]
IsoDate = Annotated[Optional[str], BeforeValidator(_clean_date)]
Score = Annotated[Optional[float], BeforeValidator(_clamp01)]
IdCode = Annotated[Optional[str], BeforeValidator(_strip_upper)]  # GSTIN, PAN
Text = Annotated[Optional[str], BeforeValidator(_strip)]


class TaxLine(BaseModel):
    type: Literal["IGST", "CGST", "SGST", "UTGST", "CESS", "OTHER"]
    rate: Rate = Field(None, description="Percentage without % sign, e.g. '18'")
    amount: Money = None


class LineItem(BaseModel):
    line_no: Optional[int] = None
    description: Text = None
    hsn_sac: Text = Field(None, description="HSN or SAC code if printed")
    quantity: Money = Field(None, description="null when the row has no quantity")
    unit_price: Money = None
    gross_amount: Money = Field(None, description="'Gross Amount' column as printed (may already include tax)")
    other_charges: Money = Field(None, description="'Other Charges' column as printed")
    discount: Discount = Field(None, description="Discount column as printed, keep a minus sign if shown; null if the invoice has no discount column")
    net_amount: Money = Field(None, description="Taxable value before tax")
    taxes: list[TaxLine] = Field(default_factory=list)
    line_total: Money = Field(None, description="Net amount plus tax, as printed")
    confidence: Score = Field(None, description="0-1 confidence for this whole row")


class Party(BaseModel):
    name: Text = None
    address: Text = None
    gstin: IdCode = None
    pan: IdCode = None
    state_code: Text = Field(None, description="Two-digit GST state code if printed")


class Payment(BaseModel):
    transaction_id: Text = None
    mode: Text = None
    paid_at: Text = Field(None, description="As printed")
    amount_paid: Money = Field(None, description="Often labelled 'Invoice Value'")


class FieldConfidence(BaseModel):
    """Model-reported confidence (0-1) for the key header fields."""
    invoice_number: Score = None
    invoice_date: Score = None
    seller_name: Score = None
    seller_gstin: Score = None
    buyer_name: Score = None
    subtotal: Score = None
    total_tax: Score = None
    grand_total: Score = None
    currency: Score = None


class Invoice(BaseModel):
    source_pages: list[int] = Field(
        default_factory=list, description="1-based page numbers this invoice spans"
    )
    document_type: Text = Field(None, description="e.g. Tax Invoice, Credit Note, Bill of Supply")
    invoice_number: Text = None
    invoice_date: IsoDate = Field(None, description="ISO date YYYY-MM-DD")
    invoice_date_raw: Text = Field(None, description="Date exactly as printed")
    due_date: IsoDate = None
    order_number: Text = None
    reference_numbers: list[str] = Field(
        default_factory=list, description="Other printed reference ids, e.g. 'Invoice Details' codes"
    )
    place_of_supply: Text = None
    reverse_charge: Optional[bool] = Field(None, description="Is tax payable under reverse charge")
    currency: IdCode = Field(None, description="ISO 4217 code, e.g. INR")

    seller: Party = Field(default_factory=Party)
    buyer: Party = Field(default_factory=Party, description="Billing party")

    line_items: list[LineItem] = Field(default_factory=list)

    subtotal: Money = Field(
        None, description="ONLY if the invoice prints a subtotal / total taxable value line; never computed")
    total_tax: Money = None
    grand_total: Money = None
    other_charges: Money = Field(None, description="Invoice-level other charges (packing, freight) if printed")
    round_off: Money = Field(None, description="Round-off line as printed, keeping its sign")
    amount_in_words: Text = None

    payment: Payment = Field(default_factory=Payment)
    confidence: FieldConfidence = Field(default_factory=FieldConfidence)


class ExtractionResult(BaseModel):
    """What the model returns for one file: every invoice found in it."""
    invoices: list[Invoice] = Field(default_factory=list)
