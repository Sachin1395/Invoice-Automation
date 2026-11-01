"""Runs every check on one invoice, updates confidence, and decides the route."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from ..config import Settings
from .arithmetic import check_arithmetic
from .bank import BankVerifier, NoBankData
from .completeness import check_completeness
from .confidence import update_confidence
from .duplicates import check_duplicates, make_keys
from .routing import decide_route
from .types import Flag, InvoiceValidation, Route, Severity


@dataclass
class Ctx:
    settings: Settings
    rules: Any                       # the profile's rules module
    conn: Any                        # open sqlite connection
    is_scan: bool                    # no text layer: values were read from pixels
    today: date
    bank: BankVerifier = None

    def __post_init__(self):
        self.bank = self.bank or NoBankData()


def validate_invoice(inv: dict, index: int, ctx: Ctx) -> tuple[InvoiceValidation, dict, dict]:
    """Returns (validation, duplicate keys, the invoice as it should be stored). The stored invoice
    differs from the input only when tax-inclusive rows were repaired."""
    s, rules = ctx.settings, ctx.rules
    arith = check_arithmetic(inv, s)
    repaired = arith.normalized_invoice is not None
    if repaired:
        inv = arith.normalized_invoice
    flags: list[Flag] = list(arith.flags)
    confirmations = list(arith.confirmations)

    # A row amount that does not appear anywhere in the file's own text was misread or calculated by
    # the model (the row-total case on Nykaa invoices). Rows we just repaired are excluded: the
    # numbers the evidence objected to have been replaced by printed ones.
    if not repaired:
        for i, grounded_row in enumerate((inv.get("grounding") or {}).get("line_items") or [], start=1):
            if grounded_row is False:
                flags.append(Flag(code="ROW_VALUE_NOT_IN_TEXT", severity=Severity.MAJOR,
                                  message=f"Row {i}: an amount on this row does not appear in the document's text; "
                                          f"it may have been misread or calculated rather than printed"))

    flags += check_completeness(inv, rules.REQUIRED)

    sanity_flags, sanity_conf = rules.sanity_checks(inv, s, ctx.today)
    flags += sanity_flags
    confirmations += sanity_conf

    keys = make_keys(inv, rules)
    dup_flags, dup_conf, duplicate_of = check_duplicates(inv, keys, ctx.conn)
    flags += dup_flags
    confirmations += dup_conf

    field_conf, score = update_confidence(inv, flags, confirmations, ctx.is_scan, s,
                                          list(rules.REQUIRED.values()))
    bank_match = ctx.bank.find_match(inv)
    route, reasons, low_risk = decide_route(inv, flags, confirmations, score, bank_match, s,
                                            rules.EXPECTED_CURRENCY)
    return InvoiceValidation(
        index=index, route=route, score=score, field_confidence=field_conf, flags=flags,
        confirmations=sorted(set(confirmations)), reasons=reasons,
        computed_subtotal=arith.computed_subtotal, duplicate_of=duplicate_of,
        is_scan=ctx.is_scan, low_risk=low_risk,
        bank_match=bank_match.reference if bank_match else None,
    ), keys, inv


def validate_empty_file(is_scan: bool) -> InvoiceValidation:
    """The model found no invoice. In a file with real text that means it isn't an invoice
    (nothing for a person to fix); in an image it may just be unreadable, so a person looks."""
    if is_scan:
        flag = Flag(code="NO_INVOICE_IN_IMAGE", severity=Severity.MAJOR,
                    message="No invoice could be read from this image; it may be unreadable or not an invoice")
        route = Route.HUMAN_REVIEW
    else:
        flag = Flag(code="NO_INVOICE_FOUND", severity=Severity.BLOCKER,
                    message="The file has text but no invoice was found in it")
        route = Route.HARD_REJECT
    return InvoiceValidation(index=-1, route=route, score=0.0, flags=[flag],
                             reasons=[flag.message], is_scan=is_scan)
