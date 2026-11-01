"""Duplicate detection against invoices already stored.

Keys, from strictest to loosest:
  exact     same seller id + same invoice number (case and spacing ignored)
  look-alike same after folding easily confused characters (I/L/1, O/0): the scan-vs-digital case
  probable  same seller + same date + same total, different number
A look-alike can be a genuine duplicate or a genuinely different invoice, so it always goes to a person.
Only an exact match with the same total is rejected automatically.
"""
from __future__ import annotations

import re
from decimal import Decimal
from typing import Optional

from .. import db
from .arithmetic import D
from .types import Flag, Severity

_CONFUSABLE = str.maketrans({"I": "1", "L": "1", "O": "0"})


def norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", "", s).upper() if s else ""


def fold(s: Optional[str]) -> str:
    return norm(s).translate(_CONFUSABLE)


def shape(s: Optional[str]) -> str:
    """'I1926JD000023630' -> 'A9A9', 'MKT-353808321' -> 'A-9'. Run lengths are ignored on purpose."""
    out: list[str] = []
    for ch in norm(s):
        c = "A" if ch.isalpha() else "9" if ch.isdigit() else ch
        if not out or out[-1] != c or c not in ("A", "9"):
            out.append(c)
    return "".join(out)


def order_norm(s: Optional[str]) -> str:
    """'1322808-4772362-3393803' and '132280847723623393803' are the same order."""
    return re.sub(r"[^0-9A-Za-z]", "", s).upper() if s else ""


def make_keys(inv: dict, rules) -> dict:
    vendor = rules.vendor_id(inv)
    number = inv.get("invoice_number")
    return {
        "vendor_id": vendor,
        "vendor_fold": fold(vendor) if vendor else None,
        "number_norm": norm(number) or None,
        "number_fold": fold(number) or None,
        "number_shape": shape(number) or None,
        "order_norm": order_norm(inv.get("order_number")) or None,
    }


def check_duplicates(inv: dict, keys: dict, conn) -> tuple[list, list, Optional[int]]:
    """Returns (flags, confirmations, duplicate_of)."""
    flags: list[Flag] = []
    conf: list[str] = []
    vendor_fold, number_fold = keys.get("vendor_fold"), keys.get("number_fold")
    if not vendor_fold:
        return flags, conf, None

    grand = D(inv.get("grand_total"))
    tol = Decimal("0.01")
    blocker_id: Optional[int] = None
    other_id: Optional[int] = None
    seen: set[int] = set()

    def same_total(row) -> bool:
        other = D(row["grand_total"])
        return grand is not None and other is not None and abs(other - grand) <= tol

    if number_fold:
        for row in db.find_invoices_by_folded_key(conn, vendor_fold, number_fold):
            seen.add(row["invoice_id"])
            exact = row["vendor_id"] == keys["vendor_id"] and row["number_norm"] == keys["number_norm"]
            where = f"invoice #{row['invoice_id']} ({row['invoice_number']}, {row['status']})"
            if exact and same_total(row):
                flags.append(Flag(code="DUPLICATE_EXACT", severity=Severity.BLOCKER,
                                  message=f"Exact duplicate of {where}: same seller, number and total",
                                  related_invoice_id=row["invoice_id"]))
                blocker_id = blocker_id or row["invoice_id"]
            elif exact:
                flags.append(Flag(code="DUPLICATE_NUMBER_DIFFERENT_TOTAL", severity=Severity.MAJOR,
                                  message=f"Same seller and invoice number as {where}, but the total differs "
                                          f"({row['grand_total']} vs {inv.get('grand_total')}): a revision or a misread?",
                                  related_invoice_id=row["invoice_id"]))
                other_id = other_id or row["invoice_id"]
            else:
                flags.append(Flag(code="DUPLICATE_LOOKALIKE", severity=Severity.MAJOR,
                                  message=f"Looks like {where}: the seller and invoice number match once I/1/L and O/0 "
                                          f"are treated as the same character. Probably the same invoice read "
                                          f"differently; please confirm",
                                  related_invoice_id=row["invoice_id"]))
                other_id = other_id or row["invoice_id"]

    date = inv.get("invoice_date")
    if date and grand is not None:
        for row in db.find_invoices_same_day(conn, vendor_fold, date):
            if row["invoice_id"] in seen or not same_total(row):
                continue
            flags.append(Flag(code="DUPLICATE_PROBABLE", severity=Severity.MAJOR,
                              message=f"Same seller, date and total as invoice #{row['invoice_id']} "
                                      f"({row['invoice_number']}) but a different number",
                              related_invoice_id=row["invoice_id"]))
            other_id = other_id or row["invoice_id"]

    # Seller history: does this number look like the ones this seller normally issues?
    if keys.get("number_shape") and blocker_id is None:
        shapes = db.accepted_shapes(conn, vendor_fold)
        if shapes:
            if keys["number_shape"] in shapes:
                conf.append("invoice_number:shape")
            else:
                flags.append(Flag(code="NUMBER_SHAPE_MISMATCH", severity=Severity.MAJOR,
                                  message=f"Invoice number {inv.get('invoice_number')!r} is built differently from this "
                                          f"seller's earlier invoices (pattern {keys['number_shape']} vs "
                                          f"{', '.join(sorted(shapes))})", fields=["invoice_number"]))
    if not any(f.code.startswith("DUPLICATE") for f in flags):
        conf.append("no_duplicate")
    return flags, conf, blocker_id or other_id
