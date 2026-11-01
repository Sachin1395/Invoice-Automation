"""India GST validation rules. The generic engine calls these through the profile, so another
country or customer gets its own rules module instead of editing the engine."""
from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Optional

from ...validation.arithmetic import D
from ...validation.types import Flag, Severity

# Required field path -> name used in the confidence dict.
REQUIRED = {
    "invoice_number": "invoice_number",
    "invoice_date": "invoice_date",
    "seller.name": "seller_name",
    "seller.gstin": "seller_gstin",
    "grand_total": "grand_total",
    "currency": "currency",
}
EXPECTED_CURRENCY = "INR"

# GST state / UT codes (the first two digits of a GSTIN).
STATES = {
    "01": "JAMMU AND KASHMIR", "02": "HIMACHAL PRADESH", "03": "PUNJAB", "04": "CHANDIGARH",
    "05": "UTTARAKHAND", "06": "HARYANA", "07": "DELHI", "08": "RAJASTHAN", "09": "UTTAR PRADESH",
    "10": "BIHAR", "11": "SIKKIM", "12": "ARUNACHAL PRADESH", "13": "NAGALAND", "14": "MANIPUR",
    "15": "MIZORAM", "16": "TRIPURA", "17": "MEGHALAYA", "18": "ASSAM", "19": "WEST BENGAL",
    "20": "JHARKHAND", "21": "ODISHA", "22": "CHHATTISGARH", "23": "MADHYA PRADESH", "24": "GUJARAT",
    "25": "DAMAN AND DIU", "26": "DADRA AND NAGAR HAVELI AND DAMAN AND DIU", "27": "MAHARASHTRA",
    "28": "ANDHRA PRADESH", "29": "KARNATAKA", "30": "GOA", "31": "LAKSHADWEEP", "32": "KERALA",
    "33": "TAMIL NADU", "34": "PUDUCHERRY", "35": "ANDAMAN AND NICOBAR ISLANDS", "36": "TELANGANA",
    "37": "ANDHRA PRADESH", "38": "LADAKH", "97": "OTHER TERRITORY", "99": "CENTRE JURISDICTION",
}
_ALIASES = {"ORISSA": "21", "PONDICHERRY": "34", "JAMMU & KASHMIR": "01", "NCT OF DELHI": "07",
            "NEW DELHI": "07", "UTTARANCHAL": "05", "DADRA & NAGAR HAVELI": "26", "ANDAMAN & NICOBAR": "35"}

GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9/\-]{1,16}$")  # GST rule: max 16 chars, letters/digits/-/ /

GST_REFORM_DATE = date(2025, 9, 22)
RATES_AFTER_REFORM = {Decimal(x) for x in ("0", "0.25", "1.5", "3", "5", "18", "40")}
RATES_BEFORE_REFORM = {Decimal(x) for x in ("0", "0.1", "0.25", "1", "1.5", "2", "3", "5", "12", "18", "28")}


def vendor_id(inv: dict) -> Optional[str]:
    g = (inv.get("seller") or {}).get("gstin")
    return re.sub(r"\s", "", g).upper() if g else None


def gstin_checksum_ok(gstin: str) -> bool:
    if len(gstin) != 15 or any(c not in _CHARS for c in gstin):
        return False
    total = 0
    for i, c in enumerate(gstin[:14]):
        v = _CHARS.index(c) * (1 if i % 2 == 0 else 2)
        total += v // 36 + v % 36
    return _CHARS[(36 - total % 36) % 36] == gstin[14]


def state_code_from_text(text: Optional[str]) -> Optional[str]:
    """'TAMIL NADU', '33-Tamil Nadu', 'Tamil Nadu (33)' -> '33'."""
    if not text:
        return None
    up = re.sub(r"\s+", " ", text.upper().strip())
    for name, code in _ALIASES.items():
        if name in up:
            return code
    for m in re.finditer(r"\b(\d{2})\b", up):
        if m.group(1) in STATES:
            return m.group(1)
    for code, name in sorted(STATES.items(), key=lambda kv: -len(kv[1])):
        if name in up.replace("&", "AND"):
            return code
    return None


def _check_gstin(g: Optional[str], who: str, conf_field: str, flags: list, conf: list) -> None:
    if not g:
        return
    if not GSTIN_RE.match(g):
        flags.append(Flag(code=f"{who.upper()}_GSTIN_FORMAT", severity=Severity.MAJOR,
                          message=f"{who.capitalize()} GSTIN {g} is not a valid GSTIN format",
                          fields=[conf_field]))
    elif not gstin_checksum_ok(g):
        flags.append(Flag(code=f"{who.upper()}_GSTIN_CHECKSUM", severity=Severity.MAJOR,
                          message=f"{who.capitalize()} GSTIN {g} fails its check digit "
                                  f"(a character was probably misread, e.g. O/0 or I/1)",
                          fields=[conf_field]))
    elif g[:2] not in STATES:
        flags.append(Flag(code=f"{who.upper()}_GSTIN_STATE", severity=Severity.MAJOR,
                          message=f"{who.capitalize()} GSTIN {g} starts with unknown state code {g[:2]}",
                          fields=[conf_field]))
    else:
        conf.append(f"{conf_field}:checksum")


def sanity_checks(inv: dict, settings, today: date) -> tuple[list, list]:
    """Returns (flags, confirmations)."""
    flags: list[Flag] = []
    conf: list[str] = []
    seller, buyer = inv.get("seller") or {}, inv.get("buyer") or {}
    s_gstin, b_gstin = vendor_id(inv), (buyer.get("gstin") or "").upper() or None

    _check_gstin(s_gstin, "seller", "seller_gstin", flags, conf)
    _check_gstin(b_gstin, "buyer", "buyer_gstin", flags, conf)

    if s_gstin and b_gstin and s_gstin == b_gstin:
        flags.append(Flag(code="SELF_INVOICE", severity=Severity.MAJOR,
                          message="Seller and buyer GSTIN are the same"))

    if s_gstin and GSTIN_RE.match(s_gstin):
        printed_pan = (seller.get("pan") or "").upper() or None
        if printed_pan and printed_pan != s_gstin[2:12]:
            flags.append(Flag(code="PAN_MISMATCH", severity=Severity.MINOR,
                              message=f"Printed PAN {printed_pan} differs from the PAN inside the GSTIN ({s_gstin[2:12]})"))
        printed_state = (seller.get("state_code") or "").strip()
        if printed_state and printed_state.isdigit() and printed_state.zfill(2) != s_gstin[:2]:
            flags.append(Flag(code="STATE_CODE_MISMATCH", severity=Severity.MINOR,
                              message=f"Printed seller state code {printed_state} differs from the GSTIN's ({s_gstin[:2]})"))

    # Tax type must fit the supply: other state -> IGST; same state -> CGST + SGST/UTGST.
    pos = state_code_from_text(inv.get("place_of_supply"))
    types = {t.get("type") for r in inv.get("line_items") or [] for t in r.get("taxes") or []
             if _charged(t)}   # a "0.00" IGST column on an intra-state invoice is not IGST being charged
    if s_gstin and GSTIN_RE.match(s_gstin) and pos:
        inter_state = s_gstin[:2] != pos
        if inter_state and types & {"CGST", "SGST", "UTGST"}:
            flags.append(Flag(code="TAX_TYPE_MISMATCH", severity=Severity.MAJOR,
                              message=f"Seller state {s_gstin[:2]} and place of supply {pos} differ, "
                                      f"so IGST is expected, but CGST/SGST was charged"))
        elif not inter_state and "IGST" in types:
            flags.append(Flag(code="TAX_TYPE_MISMATCH", severity=Severity.MAJOR,
                              message=f"Seller and place of supply are both in state {pos}, "
                                      f"so CGST+SGST is expected, but IGST was charged"))

    # Rates: the sum of a row's component rates must be a real GST rate for the invoice date.
    inv_date = _parse_iso(inv.get("invoice_date"))
    allowed = RATES_AFTER_REFORM if (inv_date and inv_date >= GST_REFORM_DATE) else RATES_BEFORE_REFORM
    for i, r in enumerate(inv.get("line_items") or [], start=1):
        rates = [D(t.get("rate")) for t in r.get("taxes") or [] if t.get("type") not in ("CESS", "OTHER")]
        if rates and all(x is not None for x in rates):
            total_rate = sum(rates, Decimal(0))
            if total_rate not in allowed:
                flags.append(Flag(code="TAX_RATE_INVALID", severity=Severity.MAJOR,
                                  message=f"Row {i}: combined tax rate {total_rate}% is not a GST rate "
                                          f"in force on {inv_date or 'the invoice date'}"))

    number = inv.get("invoice_number")
    if number and not INVOICE_NO_RE.match(number.strip()):
        flags.append(Flag(code="INVOICE_NUMBER_FORMAT", severity=Severity.MINOR,
                          message=f"Invoice number {number!r} is longer than 16 characters or has unusual characters"))

    if inv_date:
        if inv_date > today + timedelta(days=1):
            flags.append(Flag(code="DATE_IN_FUTURE", severity=Severity.MAJOR,
                              message=f"Invoice date {inv_date} is in the future", fields=["invoice_date"]))
        elif inv_date < today - timedelta(days=6 * 365):
            flags.append(Flag(code="DATE_VERY_OLD", severity=Severity.MINOR,
                              message=f"Invoice date {inv_date} is more than 6 years old"))
        due = _parse_iso(inv.get("due_date"))
        if due and due < inv_date:
            flags.append(Flag(code="DUE_BEFORE_INVOICE", severity=Severity.MINOR,
                              message="Due date is before the invoice date"))

    cur = inv.get("currency")
    if cur and cur != EXPECTED_CURRENCY:
        flags.append(Flag(code="CURRENCY_UNEXPECTED", severity=Severity.MAJOR,
                          message=f"Currency is {cur}; this profile expects {EXPECTED_CURRENCY}"))

    is_credit = "credit" in (inv.get("document_type") or "").lower()
    if not is_credit:
        for i, r in enumerate(inv.get("line_items") or [], start=1):
            for field in ("quantity", "net_amount", "line_total"):
                v = D(r.get(field))
                if v is not None and v < 0:
                    flags.append(Flag(code="NEGATIVE_AMOUNT", severity=Severity.MAJOR,
                                      message=f"Row {i} has a negative {field.replace('_', ' ')}"))
        g = D(inv.get("grand_total"))
        if g is not None and g < 0:
            flags.append(Flag(code="NEGATIVE_AMOUNT", severity=Severity.MAJOR,
                              message="Grand total is negative on a document that is not a credit note"))

    for i, r in enumerate(inv.get("line_items") or [], start=1):
        h = (r.get("hsn_sac") or "").strip()
        if h and not (h.isdigit() and len(h) in (4, 6, 8)):
            flags.append(Flag(code="HSN_FORMAT", severity=Severity.MINOR,
                              message=f"Row {i}: HSN/SAC {h!r} is not 4, 6 or 8 digits"))
    return flags, conf


def _charged(tax: dict) -> bool:
    amount, rate = D(tax.get("amount")), D(tax.get("rate"))
    if amount is not None:
        return amount != 0
    return rate is not None and rate != 0


def _parse_iso(v) -> Optional[date]:
    try:
        return date.fromisoformat(v) if v else None
    except ValueError:
        return None
