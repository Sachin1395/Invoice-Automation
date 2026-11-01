"""Arithmetic checks. Pure functions over one invoice dict; no database, no profile rules.

Invoices price rows two ways, and both appear in the wild:
  exclusive (Amazon):  unit x qty - discount = net, then tax on top
  inclusive (Myntra):  gross - discount = row total, with the tax already inside
So the pricing check passes if either style reconciles. The check that holds on every
invoice is net + taxes = row total.
"""
from __future__ import annotations

import copy
from decimal import Decimal, InvalidOperation
from typing import Optional

from ..config import Settings
from .types import ArithResult, Flag, Severity
from .words import words_to_amount

ZERO = Decimal(0)


def D(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def _close(a: Decimal, b: Decimal, tol: Decimal) -> bool:
    return abs(a - b) <= tol


def _fmt(x: Decimal) -> str:
    return f"{x:.2f}"


def _normalize_inclusive(inv: dict, s: Settings) -> tuple[dict, bool]:
    """Some invoices (Nykaa) print a 'NET AMT' that already contains the tax, so it is the row total,
    not the taxable value. Signature: the column adds up to the grand total, with real tax on top of
    it that would overshoot. Rewrite the rows into the usual shape (taxable = total - tax) so the
    same checks apply, and ignore any row total the model may have calculated itself."""
    rows = inv.get("line_items") or []
    grand = D(inv.get("grand_total"))
    if not rows or grand is None:
        return inv, False
    nets = [D(r.get("net_amount")) for r in rows]
    tax_lists = [[D(t.get("amount")) for t in (r.get("taxes") or [])] for r in rows]
    if any(n is None for n in nets) or any(a is None for ts in tax_lists for a in ts):
        return inv, False
    row_tax = [sum(ts, ZERO) for ts in tax_lists]
    printed_tax = D(inv.get("total_tax"))
    tax = printed_tax if printed_tax is not None else sum(row_tax, ZERO)
    tol = Decimal(str(s.invoice_tolerance))
    sum_net = sum(nets, ZERO)
    if tax > Decimal("0.50") and _close(sum_net, grand, tol) and not _close(sum_net + tax, grand, tol):
        adjusted = copy.deepcopy(inv)
        for r, n, t in zip(adjusted["line_items"], nets, row_tax):
            r["line_total"] = str(n)
            r["net_amount"] = f"{n - t:.2f}"
        return adjusted, True
    return inv, False


def check_arithmetic(inv: dict, s: Settings) -> ArithResult:
    inv, inclusive = _normalize_inclusive(inv, s)
    normalized = inv if inclusive else None
    row_tol = Decimal(str(s.row_tolerance))
    inv_tol = Decimal(str(s.invoice_tolerance))
    round_max = Decimal(str(s.round_off_max))
    flags: list[Flag] = []
    conf: list[str] = []

    def flag(code, sev, msg, fields=()):
        flags.append(Flag(code=code, severity=sev, message=msg, fields=list(fields)))

    if inclusive:
        flag("INCLUSIVE_NET_DETECTED", Severity.MINOR,
             "The 'net amount' column adds up to the grand total, so it was treated as tax-inclusive "
             "(taxable value = net amount minus tax)")

    rows = inv.get("line_items") or []
    row_taxes: list[Optional[Decimal]] = []
    row_totals: list[Optional[Decimal]] = []
    nets: list[Optional[Decimal]] = []

    # ---- per row ---------------------------------------------------------------
    for i, r in enumerate(rows, start=1):
        net, total = D(r.get("net_amount")), D(r.get("line_total"))
        unit, qty = D(r.get("unit_price")), D(r.get("quantity"))
        disc, gross = D(r.get("discount")), D(r.get("gross_amount"))
        other = D(r.get("other_charges")) or ZERO
        taxes = r.get("taxes") or []
        amounts = [D(t.get("amount")) for t in taxes]
        tax_known = all(a is not None for a in amounts)
        tax_sum = sum((a for a in amounts if a is not None), ZERO)

        nets.append(net)
        row_totals.append(total)
        row_taxes.append(tax_sum if tax_known else None)

        if net is not None and total is not None and tax_known:
            if not (_close(net + tax_sum, total, row_tol) or _close(net + tax_sum + other, total, row_tol)):
                flag("ROW_TOTAL_MISMATCH", Severity.MAJOR,
                     f"Row {i}: taxable {_fmt(net)} + tax {_fmt(tax_sum)} = {_fmt(net + tax_sum)}, "
                     f"but the row total is {_fmt(total)}")

        if net is not None:
            for t in taxes:
                rate, amt = D(t.get("rate")), D(t.get("amount"))
                if rate is not None and amt is not None:
                    expected = net * rate / 100
                    if not _close(expected, amt, row_tol):
                        flag("TAX_RATE_MISMATCH", Severity.MAJOR,
                             f"Row {i}: {t.get('type')} {rate}% of {_fmt(net)} is {_fmt(expected)}, "
                             f"but the invoice shows {_fmt(amt)}")

        d = abs(disc) if disc is not None else ZERO
        variants = []
        if unit is not None and net is not None:
            base = unit * (qty if qty is not None else Decimal(1))
            variants.append(_close(base - d, net, row_tol) or _close(base, net, row_tol))
        if unit is not None and total is not None:
            base_incl = unit * (qty if qty is not None else Decimal(1))
            variants.append(_close(base_incl - d, total, row_tol) or _close(base_incl, total, row_tol))
        if gross is not None and total is not None:
            variants.append(_close(gross - d, total, row_tol) or _close(gross - d + other, total, row_tol)
                            or _close(gross, total, row_tol))
        if variants and not any(variants):
            flag("PRICING_MISMATCH", Severity.MAJOR,
                 f"Row {i}: unit price, quantity, gross amount and discount don't reconcile "
                 f"with the row's taxable value / total")

    # ---- invoice level -------------------------------------------------------------
    computed_subtotal = (sum(nets, ZERO) if rows and all(n is not None for n in nets) else None)
    computed_tax = (sum(row_taxes, ZERO) if rows and all(t is not None for t in row_taxes) else None)
    rows_total = (sum(row_totals, ZERO) if rows and all(t is not None for t in row_totals) else None)

    printed_sub, printed_tax = D(inv.get("subtotal")), D(inv.get("total_tax"))
    grand = D(inv.get("grand_total"))
    other_inv = D(inv.get("other_charges")) or ZERO
    round_off_printed = D(inv.get("round_off"))
    round_off = round_off_printed or ZERO

    if printed_sub is not None and computed_subtotal is not None:
        if _close(printed_sub, computed_subtotal, inv_tol):
            conf.append("subtotal:rows")
        else:
            flag("SUBTOTAL_MISMATCH", Severity.MAJOR,
                 f"Printed subtotal {_fmt(printed_sub)} but the rows' taxable values add up to "
                 f"{_fmt(computed_subtotal)}", ["subtotal"])

    if printed_tax is not None and computed_tax is not None:
        if _close(printed_tax, computed_tax, inv_tol):
            conf.append("total_tax:rows")
        else:
            flag("TAX_TOTAL_MISMATCH", Severity.MAJOR,
                 f"Printed total tax {_fmt(printed_tax)} but the rows' taxes add up to "
                 f"{_fmt(computed_tax)}", ["total_tax"])

    if grand is not None:
        base = printed_sub if printed_sub is not None else computed_subtotal
        tax = printed_tax if printed_tax is not None else computed_tax
        if base is not None and tax is not None:
            expected = base + tax + other_inv + round_off
            diff = abs(grand - expected)
            if diff <= inv_tol:
                conf.append("grand_total:formula")
            elif round_off_printed is None and diff <= round_max:
                conf.append("grand_total:formula")
                flag("ROUND_OFF_ASSUMED", Severity.MINOR,
                     f"Grand total differs from subtotal + tax by {_fmt(diff)}; treated as a round-off")
            else:
                flag("GRAND_TOTAL_MISMATCH", Severity.MAJOR,
                     f"Subtotal {_fmt(base)} + tax {_fmt(tax)} = {_fmt(expected)}, "
                     f"but the grand total is {_fmt(grand)}",
                     ["grand_total", "total_tax", "subtotal"])
        if rows_total is not None:
            diff = abs(grand - (rows_total + round_off))
            if diff <= inv_tol:
                conf.append("grand_total:rows")
            elif round_off_printed is None and diff <= round_max:
                conf.append("grand_total:rows")
            else:
                flag("GRAND_VS_ROWS_MISMATCH", Severity.MAJOR,
                     f"The row totals add up to {_fmt(rows_total)}, but the grand total is {_fmt(grand)}",
                     ["grand_total"])

        paid = D((inv.get("payment") or {}).get("amount_paid"))
        if paid is not None:
            if _close(paid, grand, inv_tol):
                conf.append("payment:amount")
            else:
                flag("PAYMENT_AMOUNT_MISMATCH", Severity.MAJOR,
                     f"Payment box shows {_fmt(paid)} but the grand total is {_fmt(grand)}")

        parsed = words_to_amount(inv.get("amount_in_words"))
        if parsed is not None:
            words_amt, has_paise = parsed
            ok = _close(words_amt, grand, Decimal("0.01")) if has_paise else abs(words_amt - grand) < 1
            if ok:
                conf.append("grand_total:words")
            else:
                flag("AMOUNT_WORDS_MISMATCH", Severity.MAJOR,
                     f"Amount in words ({inv.get('amount_in_words')}) is {words_amt}, "
                     f"but the grand total is {_fmt(grand)}", ["grand_total"])

    return ArithResult(
        flags=flags,
        confirmations=conf,
        computed_subtotal=_fmt(computed_subtotal) if computed_subtotal is not None else None,
        normalized_invoice=normalized,
    )


def total_confirmed(confirmations: list[str]) -> bool:
    """'Their math balances': something other than the model's say-so supports the grand total."""
    return any(c in confirmations for c in ("grand_total:formula", "grand_total:rows", "grand_total:words"))
