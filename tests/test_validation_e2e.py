"""ingest -> extract -> validate -> route, through the runner, with a scripted fake Gemini."""
import copy
import json
import time

import pytest

from invoice_pipeline import db, review
from invoice_pipeline.models import JobStatus, PipelineState
from invoice_pipeline.nodes.validate import validate_node
from invoice_pipeline.profiles.india_gst import PROFILE
from invoice_pipeline.runner import run_once
from invoice_pipeline.validation.bank import BankMatch

from conftest import make_scanned_pdf
from fakes import FakeLLM
from golden import (DIGITAL, amazon_fee, grounded, myntra_fees, myntra_watch, pdf_for, retailez_backpack,
                    scanned)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def reply(invoices: list[dict]) -> str:
    return json.dumps({"invoices": invoices})


def digital_five() -> list[dict]:
    out = []
    for n, make in enumerate(DIGITAL, start=1):
        inv = make()
        inv["source_pages"] = [n]            # one page per invoice in the test PDF
        out.append(inv)
    return out


def rows(settings, sql, *args):
    with db.session(settings.db_path) as conn:
        return conn.execute(sql, args).fetchall()


def statuses(settings):
    return {r["invoice_number"]: r["status"] for r in rows(settings, "select invoice_number, status from invoices")}


def test_five_real_digital_invoices_all_autopass(val_settings, drop):
    invs = digital_five()
    drop("five.pdf", pdf_for(invs))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    assert res.status == JobStatus.DONE and res.detail == "5 invoice(s): 5 autopass"
    assert set(statuses(val_settings).values()) == {"accepted"} and len(statuses(val_settings)) == 5
    assert rows(val_settings, "select * from review_queue") == []
    # the numbers made it into the line table, and order numbers are stored without dashes
    assert rows(val_settings, "select count(*) n from invoice_lines")[0]["n"] == 7
    assert {r["order_number_norm"] for r in rows(val_settings, "select order_number_norm from invoices")} == {
        "40590784846760355", "132280847723623393803"}


def test_scanned_copies_of_the_same_invoices(val_settings, drop):
    """The real scenario: the digital file first, then a scan of it with I misread as 1."""
    invs = digital_five()[2:]                                    # JW watch, Myntra fees, Flipkart
    for n, inv in enumerate(invs, start=1):
        inv["source_pages"] = [n]                                # this PDF has only three pages
    drop("digital.pdf", pdf_for(invs))
    run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))

    scan = [scanned(myntra_watch(), "11926JD000023630"),         # I -> 1
            scanned(myntra_fees()),                              # read correctly
            scanned(DIGITAL[4](), "11926FI015395867")]           # I -> 1
    for s in scan:
        s["source_pages"] = [1]
    drop("scan.pdf", make_scanned_pdf())
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([{k: v for k, v in s.items() if k != "grounding"} for s in scan])]}))

    assert res.status == JobStatus.DONE
    assert res.detail == "3 invoice(s): 2 human review, 1 hard reject"
    by_number = {r["invoice_number"]: r for r in rows(val_settings, "select * from invoices where seq is not null and extraction_id=2")}
    assert by_number["11926JD000023630"]["status"] == "review"       # look-alike: a person confirms
    assert by_number["11926FI015395867"]["status"] == "review"
    assert by_number["I2926MY227475971"]["status"] == "rejected"     # exact duplicate, even from a scan
    assert by_number["I2926MY227475971"]["duplicate_of"] is not None
    open_items = rows(val_settings, "select * from review_queue where status='open'")
    assert len(open_items) == 2
    assert any("Looks like invoice" in r for r in json.loads(open_items[0]["reasons_json"]))
    import io, contextlib
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        review.main(["show", str(open_items[0]["review_id"])], val_settings)
    assert "REJECT this item" in out.getvalue()


def test_whole_file_resubmitted_is_recognised_as_duplicate(val_settings, drop):
    invs = digital_five()
    drop("a.pdf", pdf_for(invs))
    [first] = run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    drop("b.pdf", pdf_for(invs, footer="re-sent copy"))          # different bytes, same invoices
    [second] = run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    assert second.status == JobStatus.DUPLICATE
    with db.session(val_settings.db_path) as conn:
        assert db.get_job(conn, second.job_id)["duplicate_of"] == first.job_id
    assert [p.name for p in val_settings.processed_dir.iterdir()].count("b.pdf") == 1


def test_a_repeated_invoice_inside_one_file_is_rejected(val_settings, drop):
    invs = [amazon_fee(), amazon_fee()]
    invs[1]["source_pages"] = [2]
    drop("twice.pdf", pdf_for(invs))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    assert res.detail == "2 invoice(s): 1 autopass, 1 hard reject"
    assert sorted(r["status"] for r in rows(val_settings, "select status from invoices")) == ["accepted", "rejected"]


def test_broken_totals_and_misread_gstin_go_to_review(val_settings, drop):
    bad_total = retailez_backpack()
    bad_total["grand_total"] = "719.00"
    bad_gstin = amazon_fee()
    bad_gstin["seller"]["gstin"] = "29AAICA39I8J1ZE"
    bad_gstin["source_pages"] = [2]
    invs = [bad_total, bad_gstin]
    drop("bad.pdf", pdf_for(invs))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    assert res.detail == "2 invoice(s): 2 human review"
    reasons = " ".join(r["reasons_json"] for r in rows(val_settings, "select reasons_json from review_queue"))
    assert "grand total is 719.00" in reasons
    assert "check digit" in reasons or "not a valid GSTIN" in reasons


def test_high_value_new_vendor_needs_review_unless_a_bank_charge_matches(val_settings, drop):
    big = myntra_watch()
    big["line_items"][0].update(gross_amount="103799.00", net_amount="85308.47", line_total="100664.00",
                                taxes=[{"type": "IGST", "rate": "18", "amount": "15355.53"}])
    big.update(subtotal="85308.47", total_tax="15355.53", grand_total="100664.00")
    drop("big.pdf", pdf_for([big]))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([big])]}))
    assert res.detail == "1 invoice(s): 1 human review"

    class Bank:
        def find_match(self, invoice):
            return BankMatch("UTR-1")
    with db.session(val_settings.db_path) as conn:               # start over for the second scenario
        conn.execute("DELETE FROM invoices"); conn.execute("DELETE FROM review_queue")
        conn.execute("UPDATE jobs SET status='failed'")
    drop("big2.pdf", pdf_for([big], footer="again"))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([big])]}), bank=Bank())
    assert res.detail == "1 invoice(s): 1 autopass"


def test_no_invoice_in_a_text_file_is_rejected_but_in_an_image_goes_to_a_person(val_settings, drop):
    drop("memo.pdf", pdf_for([amazon_fee()]))
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([])]}))
    assert res.status == JobStatus.REJECTED
    assert [p.name for p in val_settings.rejected_dir.iterdir()] == ["memo.pdf"]

    drop("blurry.pdf", make_scanned_pdf())
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([])]}))
    assert res.status == JobStatus.DONE and res.detail.startswith("no invoice found")
    [item] = rows(val_settings, "select * from review_queue")
    assert item["invoice_id"] is None and item["status"] == "open"


def test_validation_is_idempotent(val_settings):
    db.init_db(val_settings.db_path)
    state = PipelineState(job_id=1, source_path="x", file_id="abc", extraction_id=7,
                          invoices=[grounded(amazon_fee())], text_layer=["text"])
    for _ in range(2):
        validate_node(state, val_settings, PROFILE)
    assert rows(val_settings, "select count(*) n from invoices")[0]["n"] == 1
    assert rows(val_settings, "select count(*) n from invoice_lines")[0]["n"] == 1


# ---- review commands ------------------------------------------------------------------------
def test_review_commands(val_settings, drop, capsys):
    bad = retailez_backpack()
    bad["grand_total"] = "719.00"
    other = amazon_fee()
    other["grand_total"] = "6.00"
    other["source_pages"] = [2]
    invs = [bad, other]
    drop("r.pdf", pdf_for(invs))
    run_once(val_settings, llm=FakeLLM({"*": [reply(invs)]}))
    capsys.readouterr()

    assert review.main(["list"], val_settings) == 0
    listing = capsys.readouterr().out
    assert "CJB1-1303297" in listing and "MKT-353808321" in listing

    assert review.main(["show", "1"], val_settings) == 0
    shown = capsys.readouterr().out
    assert "GRAND_TOTAL_MISMATCH" in shown and "page_001.jpg" in shown

    assert review.main(["approve", "1", "--note", "checked the paper copy"], val_settings) == 0
    assert review.main(["reject", "2"], val_settings) == 0
    assert review.main(["approve", "1"], val_settings) == 1          # already resolved
    assert statuses(val_settings) == {"CJB1-1303297": "accepted", "MKT-353808321": "rejected"}
    assert review.main(["list"], val_settings) == 0
    assert "nothing to review" in capsys.readouterr().out


def test_nykaa_tax_inclusive_invoice_end_to_end(val_settings, drop):
    """The real failure: 15 false alarms and a human-review route. Now it autopasses, the stored rows
    carry the printed numbers, and the model's original answer stays in the audit table."""
    from golden import nykaa_as_returned, nykaa_pdf
    drop("nykaa.pdf", nykaa_pdf())
    [res] = run_once(val_settings, llm=FakeLLM({"*": [reply([nykaa_as_returned()])]}))
    assert res.detail == "1 invoice(s): 1 autopass"
    [stored] = rows(val_settings, "select * from invoices")
    assert stored["status"] == "accepted" and stored["computed_subtotal"] == "1380.71"
    assert "INCLUSIVE_NET_DETECTED" in stored["flags_json"]
    line2 = rows(val_settings, "select * from invoice_lines where line_no=2")[0]
    assert (line2["line_total"], line2["net_amount"], line2["tax_total"]) == ("414.82", "351.54", "63.28")
    original = json.loads(rows(val_settings, "select parsed_json from extractions")[0]["parsed_json"])
    assert original["invoices"][0]["line_items"][1]["line_total"] == "478.10"      # untouched for audit
    assert original["invoices"][0]["grounding"]["line_items"][1] is False         # grounding saw the invention


def test_ocr_scan_gets_the_scan_treatment_end_to_end(val_settings, drop):
    """A scanner app's OCR text must not lift trust: the invoice number stays capped, nothing is 'grounded'."""
    from conftest import make_ocr_pdf
    inv = amazon_fee()
    inv["source_pages"] = [1]
    drop("ocr.pdf", make_ocr_pdf())
    llm = FakeLLM({"*": [reply([inv])]})
    [res] = run_once(val_settings, llm=llm)
    assert res.detail == "1 invoice(s): 1 autopass"
    [row] = rows(val_settings, "select * from invoices")
    assert json.loads(row["field_confidence_json"])["invoice_number"] == 0.75
    parsed = json.loads(rows(val_settings, "select parsed_json from extractions")[0]["parsed_json"])
    assert set(parsed["invoices"][0]["grounding"].values()) <= {None}
    assert not any("Text layer" in getattr(p, "text", "") for p in llm.calls[0][1])      # no OCR hint sent to the model
