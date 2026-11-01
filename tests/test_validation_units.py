"""Unit tests for each validation check, using the golden invoices."""
import copy
from datetime import date
from decimal import Decimal

import pytest

from invoice_pipeline import db
from invoice_pipeline.profiles.india_gst import PROFILE, rules
from invoice_pipeline.validation.arithmetic import check_arithmetic, total_confirmed
from invoice_pipeline.validation.bank import BankMatch
from invoice_pipeline.validation.completeness import check_completeness
from invoice_pipeline.validation.duplicates import fold, make_keys, norm, order_norm, shape
from invoice_pipeline.validation.engine import Ctx, validate_empty_file, validate_invoice
from invoice_pipeline.validation.types import Route, Severity
from invoice_pipeline.validation.words import words_to_amount

from golden import (DIGITAL, amazon_fee, flipkart_bill, grounded, myntra_fees, myntra_watch,
                    retailez_backpack, scanned)

TODAY = date(2026, 10, 27)


def codes(flags, severity=None):
    return {f.code for f in flags if severity is None or f.severity == severity}


@pytest.fixture
def conn(val_settings):
    db.init_db(val_settings.db_path)
    c = db.connect(val_settings.db_path)
    yield c
    c.commit()
    c.close()


@pytest.fixture
def ctx(val_settings, conn):
    return Ctx(settings=val_settings, rules=rules, conn=conn, is_scan=False, today=TODAY)


def store(conn, inv, status="accepted", job_id=1):
    keys = make_keys(inv, rules)
    payload = {"route": "autopass", "score": 1.0, "flags": [], "field_confidence": {}}
    iid = db.insert_invoice(conn, extraction_id=job_id, job_id=job_id, sha256="x", seq=0,
                            inv=inv, validation=payload, keys=keys)
    db.set_invoice_status(conn, [iid], status)
    conn.commit()
    return iid


# ---- words / keys / GSTIN -----------------------------------------------------------------
def test_amount_in_words():
    assert words_to_amount("Four Hundred Sixty-eight only") == (Decimal(468), False)
    assert words_to_amount("Rupees One Lakh Twenty Thousand Five Hundred and Fifty Paise Only") == (Decimal("120500.50"), True)
    assert words_to_amount("blah blah") is None and words_to_amount(None) is None


def test_keys_fold_confusable_characters():
    assert fold("I1926JD000023630") == fold("11926JD000023630") == fold("l1926jd0O0023630".replace("0O0", "000"))
    assert norm(" i1926 jd ") == "I1926JD"
    assert shape("I1926JD000023630") == "A9A9" and shape("11926JD000023630") == "9A9"
    assert shape("MKT-353808321") == shape("MKT-318400606") == "A-9"
    assert order_norm("1322808-4772362-3393803") == order_norm("132280847723623393803")


@pytest.mark.parametrize("gstin", ["19AAECJ7669M1ZO", "29AAECM9636P1ZJ", "19AABCF8078M1ZY",
                                   "33AALCR3173P1ZU", "29AAICA3918J1ZE", "36AFFFS4938H1ZN"])
def test_real_gstins_pass_and_single_misreads_fail(gstin):
    assert rules.gstin_checksum_ok(gstin)
    swaps = {"O": "0", "0": "O", "I": "1", "1": "I", "S": "5", "5": "S", "8": "B", "B": "8"}
    for i, ch in enumerate(gstin[:14]):
        if ch in swaps:
            assert not rules.gstin_checksum_ok(gstin[:i] + swaps[ch] + gstin[i + 1:])


# ---- arithmetic ---------------------------------------------------------------------------
@pytest.mark.parametrize("make", DIGITAL)
def test_all_real_invoices_balance(make, val_settings):
    res = check_arithmetic(make(), val_settings)
    assert not [f for f in res.flags if f.severity == Severity.MAJOR], [f.message for f in res.flags]
    assert total_confirmed(res.confirmations)


def test_both_pricing_styles_are_accepted(val_settings):
    # Amazon style (unit x qty - discount = net) and Myntra style (gross - discount = total)
    assert "PRICING_MISMATCH" not in codes(check_arithmetic(retailez_backpack(), val_settings).flags)
    assert "PRICING_MISMATCH" not in codes(check_arithmetic(myntra_watch(), val_settings).flags)


def test_computed_subtotal_when_none_printed(val_settings):
    res = check_arithmetic(retailez_backpack(), val_settings)
    assert res.computed_subtotal == "600.84"


def test_amount_words_mismatch_is_caught(val_settings):
    inv = amazon_fee()
    inv["amount_in_words"] = "Fifty only"
    assert "AMOUNT_WORDS_MISMATCH" in codes(check_arithmetic(inv, val_settings).flags, Severity.MAJOR)


@pytest.mark.parametrize("mutate,expected", [
    (lambda i: i.update(grand_total="719.00"), "GRAND_TOTAL_MISMATCH"),
    (lambda i: i.update(total_tax="118.16"), "TAX_TOTAL_MISMATCH"),
    (lambda i: i["line_items"][0].update(line_total="790.00"), "ROW_TOTAL_MISMATCH"),
    (lambda i: i["line_items"][0]["taxes"][0].update(amount="45.00"), "TAX_RATE_MISMATCH"),
    (lambda i: i["line_items"][0].update(unit_price="700.84"), "PRICING_MISMATCH"),
    (lambda i: i["payment"].update(amount_paid="700.00"), "PAYMENT_AMOUNT_MISMATCH"),
])
def test_broken_numbers_are_flagged(mutate, expected, val_settings):
    inv = retailez_backpack()
    mutate(inv)
    assert expected in codes(check_arithmetic(inv, val_settings).flags, Severity.MAJOR)


def test_small_rounding_is_tolerated_but_a_rupee_is_not(val_settings):
    inv = myntra_fees()
    inv["grand_total"] = "33.03"
    assert not codes(check_arithmetic(inv, val_settings).flags, Severity.MAJOR)   # a few paise: a round-off
    inv["grand_total"] = "34.00"
    assert "GRAND_TOTAL_MISMATCH" in codes(check_arithmetic(inv, val_settings).flags, Severity.MAJOR)


# ---- completeness -------------------------------------------------------------------------
def test_missing_required_fields():
    inv = amazon_fee()
    inv["invoice_number"] = None
    inv["seller"]["gstin"] = None
    inv["line_items"] = []
    assert codes(check_completeness(inv, rules.REQUIRED), Severity.MAJOR) == {"MISSING_REQUIRED", "NO_LINE_ITEMS"}


# ---- GST sanity ---------------------------------------------------------------------------
@pytest.mark.parametrize("make", DIGITAL)
def test_real_invoices_pass_sanity(make, val_settings):
    flags, conf = rules.sanity_checks(make(), val_settings, TODAY)
    assert not codes(flags, Severity.MAJOR), [f.message for f in flags]
    assert "seller_gstin:checksum" in conf


def test_tax_type_must_match_the_supply(val_settings):
    inv = amazon_fee()                       # Karnataka seller, Tamil Nadu supply: IGST is right
    inv["line_items"][0]["taxes"] = [{"type": "CGST", "rate": "9", "amount": "0.38"},
                                     {"type": "SGST", "rate": "9", "amount": "0.38"}]
    assert "TAX_TYPE_MISMATCH" in codes(rules.sanity_checks(inv, val_settings, TODAY)[0], Severity.MAJOR)
    inv = retailez_backpack()                # Tamil Nadu to Tamil Nadu: IGST would be wrong
    for t in inv["line_items"][0]["taxes"]:
        t["type"] = "IGST"
    assert "TAX_TYPE_MISMATCH" in codes(rules.sanity_checks(inv, val_settings, TODAY)[0], Severity.MAJOR)


def test_misread_gstin_is_flagged(val_settings):
    inv = amazon_fee()
    inv["seller"]["gstin"] = "29AAICA39I8J1ZE"        # 1 -> I
    flags, conf = rules.sanity_checks(inv, val_settings, TODAY)
    assert "SELLER_GSTIN_CHECKSUM" in codes(flags, Severity.MAJOR) or "SELLER_GSTIN_FORMAT" in codes(flags, Severity.MAJOR)
    assert "seller_gstin:checksum" not in conf


def test_rates_depend_on_the_invoice_date(val_settings):
    inv = amazon_fee()
    inv["line_items"][0]["taxes"][0].update(rate="12")
    assert "TAX_RATE_INVALID" in codes(rules.sanity_checks(inv, val_settings, TODAY)[0], Severity.MAJOR)
    inv["invoice_date"] = "2025-03-01"                 # 12% existed before the 22 Sep 2025 reform
    assert "TAX_RATE_INVALID" not in codes(rules.sanity_checks(inv, val_settings, TODAY)[0])


def test_future_date_and_foreign_currency(val_settings):
    inv = amazon_fee()
    inv["invoice_date"], inv["currency"] = "2027-01-01", "USD"
    found = codes(rules.sanity_checks(inv, val_settings, TODAY)[0], Severity.MAJOR)
    assert {"DATE_IN_FUTURE", "CURRENCY_UNEXPECTED"} <= found


# ---- duplicates ---------------------------------------------------------------------------
def run(inv, ctx):
    return validate_invoice(inv, 0, ctx)[0]


def test_exact_duplicate_is_a_blocker(ctx, conn):
    first = store(conn, grounded(myntra_fees()))
    v = run(grounded(myntra_fees()), ctx)
    assert v.route == Route.HARD_REJECT and v.duplicate_of == first
    assert "DUPLICATE_EXACT" in codes(v.flags, Severity.BLOCKER)


def test_lookalike_always_goes_to_a_person_never_rejected(ctx, conn):
    first = store(conn, grounded(myntra_watch()))
    scan = scanned(myntra_watch(), number="11926JD000023630")     # I read as 1; same date and total
    v = validate_invoice(scan, 0, ctx)[0]
    assert v.route == Route.HUMAN_REVIEW
    assert "DUPLICATE_LOOKALIKE" in codes(v.flags, Severity.MAJOR) and v.duplicate_of == first
    assert not codes(v.flags, Severity.BLOCKER)
    assert "NUMBER_SHAPE_MISMATCH" in codes(v.flags)               # also built differently from the seller's history


def test_same_number_different_total_goes_to_review(ctx, conn):
    store(conn, grounded(myntra_fees()))
    inv = grounded(myntra_fees())
    inv["grand_total"] = "34.00"
    v = run(inv, ctx)
    assert v.route == Route.HUMAN_REVIEW and "DUPLICATE_NUMBER_DIFFERENT_TOTAL" in codes(v.flags)


def test_same_seller_date_total_different_number_is_probable(ctx, conn):
    store(conn, grounded(amazon_fee()))
    other = grounded(amazon_fee())
    other["invoice_number"] = "MKT-999999999"
    v = run(other, ctx)
    assert "DUPLICATE_PROBABLE" in codes(v.flags, Severity.MAJOR) and v.route == Route.HUMAN_REVIEW


def test_rejected_and_unrelated_invoices_do_not_count(ctx, conn):
    store(conn, grounded(myntra_fees()), status="rejected")
    assert run(grounded(myntra_fees()), ctx).route == Route.AUTOPASS
    store(conn, grounded(amazon_fee()))
    assert run(grounded(retailez_backpack()), ctx).route == Route.AUTOPASS


def test_matching_seller_history_confirms_an_unverifiable_scan(ctx, conn, val_settings):
    store(conn, grounded(amazon_fee()))
    ctx.is_scan = True
    new = scanned(amazon_fee())
    new["invoice_number"], new["invoice_date"] = "MKT-111111111", "2026-09-20"   # same pattern, new invoice
    v = run(new, ctx)
    assert "invoice_number:shape" in v.confirmations
    assert v.field_confidence["invoice_number"] >= 0.9


# ---- confidence & routing -----------------------------------------------------------------
@pytest.mark.parametrize("make", DIGITAL)
def test_real_digital_invoices_autopass(make, ctx):
    v = run(grounded(make()), ctx)
    assert v.route == Route.AUTOPASS, v.reasons
    assert v.score >= 0.95 and v.low_risk


def test_scan_from_a_new_seller_caps_the_invoice_number_but_low_value_still_autopasses(ctx):
    ctx.is_scan = True
    v = run(scanned(amazon_fee()), ctx)
    assert v.field_confidence["invoice_number"] == 0.75        # capped: nothing can confirm it
    assert v.field_confidence["seller_gstin"] >= 0.95          # but the checksum confirms the GSTIN
    assert v.route == Route.AUTOPASS and v.score == 0.75


def test_high_value_unverified_goes_to_review_and_a_bank_match_unlocks_it(ctx, val_settings):
    big = grounded(myntra_watch())
    big["line_items"][0].update(gross_amount="103799.00", discount="3135.00", net_amount="85308.47",
                                taxes=[{"type": "IGST", "rate": "18", "amount": "15355.53"}], line_total="100664.00")
    big.update(subtotal="85308.47", total_tax="15355.53", grand_total="100664.00")
    v = run(big, ctx)
    assert v.route == Route.HUMAN_REVIEW and any("no matching cleared bank charge" in r for r in v.reasons)

    class Bank:
        def find_match(self, invoice):
            return BankMatch(reference="UTR123")
    ctx.bank = Bank()
    v = run(big, ctx)
    assert v.route == Route.AUTOPASS and v.bank_match == "UTR123"


def test_failed_check_lowers_the_involved_fields_and_blocks_autopass(ctx):
    inv = grounded(retailez_backpack())
    inv["grand_total"] = "719.00"
    v = run(inv, ctx)
    assert v.route == Route.HUMAN_REVIEW
    assert v.field_confidence["grand_total"] <= 0.3 and v.score <= 0.3


def test_ungrounded_value_is_distrusted(ctx):
    inv = grounded(amazon_fee())
    inv["grounding"]["invoice_number"] = False
    v = run(inv, ctx)
    assert v.field_confidence["invoice_number"] <= 0.3 and v.route == Route.HUMAN_REVIEW


def test_unverifiable_total_cannot_autopass_on_the_models_word_alone(ctx):
    inv = grounded(amazon_fee())
    inv["line_items"], inv["total_tax"], inv["amount_in_words"] = [], None, None
    v = run(inv, ctx)
    assert v.route == Route.HUMAN_REVIEW and any("independently verified" in r for r in v.reasons)


def test_empty_file_routes():
    assert validate_empty_file(is_scan=False).route == Route.HARD_REJECT
    assert validate_empty_file(is_scan=True).route == Route.HUMAN_REVIEW


# ---- findings from the real Nykaa run -------------------------------------------------------------
from golden import nykaa_as_returned, nykaa_correct


def test_inclusive_net_column_is_detected_and_repaired(val_settings):
    res = check_arithmetic(nykaa_as_returned(), val_settings)
    assert not codes(res.flags, Severity.MAJOR), [f.message for f in res.flags]
    assert "INCLUSIVE_NET_DETECTED" in codes(res.flags, Severity.MINOR)
    assert total_confirmed(res.confirmations) and res.computed_subtotal == "1380.71"
    row2 = res.normalized_invoice["line_items"][1]
    assert row2["line_total"] == "414.82"       # the printed NET AMT, not the 478.10 the model calculated
    assert row2["net_amount"] == "351.54"       # taxable value = total - tax


def test_correct_inclusive_extraction_passes_without_repair(val_settings):
    res = check_arithmetic(nykaa_correct(), val_settings)
    assert not codes(res.flags, Severity.MAJOR) and res.normalized_invoice is None
    assert "grand_total:rows" in res.confirmations


@pytest.mark.parametrize("make", DIGITAL)
def test_ordinary_invoices_are_not_mistaken_for_inclusive(make, val_settings):
    assert check_arithmetic(make(), val_settings).normalized_invoice is None


def test_zero_amount_igst_columns_are_not_a_tax_type_mismatch(val_settings):
    flags, _ = rules.sanity_checks(nykaa_as_returned(), val_settings, TODAY)
    assert "TAX_TYPE_MISMATCH" not in codes(flags)
    flags, _ = rules.sanity_checks(flipkart_bill(), val_settings, TODAY)      # CGST/SGST both 0%, intra-state
    assert not codes(flags, Severity.MAJOR)


def test_row_amount_missing_from_the_text_goes_to_review(ctx):
    inv = grounded(amazon_fee())
    inv["grounding"]["line_items"] = [False]
    v = run(inv, ctx)
    assert v.route == Route.HUMAN_REVIEW and "ROW_VALUE_NOT_IN_TEXT" in codes(v.flags, Severity.MAJOR)


def test_model_output_as_returned_for_nykaa_now_autopasses(ctx):
    inv = grounded(nykaa_as_returned())
    inv["grounding"]["line_items"] = [True] + [False] * 6 + [True] * 3     # the real run's grounding
    v, _, stored = validate_invoice(inv, 0, ctx)
    assert v.route == Route.AUTOPASS and v.score >= 0.95
    assert stored["line_items"][1]["line_total"] == "414.82"
