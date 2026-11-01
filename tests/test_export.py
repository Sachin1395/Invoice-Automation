"""The output node and export files (Excel, CSV, JSON), driven through the real runner."""
import csv
import json
import os
import time
from datetime import date
from pathlib import Path

import pytest
from openpyxl import load_workbook

from invoice_pipeline import db, review
from invoice_pipeline.export import FILES, export_all
from invoice_pipeline.export import __main__ as export_cli
from invoice_pipeline.runner import run_once

from conftest import fake_exe_bytes
from fakes import FakeLLM
from golden import (DIGITAL, amazon_fee, myntra_fees, myntra_watch, nykaa_as_returned, nykaa_pdf, pdf_for,
                    retailez_backpack, scanned)
from conftest import make_scanned_pdf


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def reply(invoices):
    return json.dumps({"invoices": invoices})


def five():
    out = []
    for n, make in enumerate(DIGITAL, start=1):
        inv = make()
        inv["source_pages"] = [n]
        out.append(inv)
    return out


def out(settings, key):
    return settings.output_dir / FILES[key]


def sheet(settings, name):
    ws = load_workbook(out(settings, "xlsx"), data_only=True)[name]
    rows = list(ws.iter_rows(values_only=True))
    header = rows[0]
    return [dict(zip(header, r)) for r in rows[1:] if any(v is not None for v in r)]


def csv_rows(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def process_five(settings, drop):
    invs = five()
    drop("five.pdf", pdf_for(invs))
    return run_once(settings, llm=FakeLLM({"*": [reply(invs)]}))


# ---- the three formats agree -----------------------------------------------------------------------
def test_all_formats_are_written_and_agree(exp_settings, drop):
    process_five(exp_settings, drop)
    for key in FILES:
        assert out(exp_settings, key).exists()

    wb = load_workbook(out(exp_settings, "xlsx"))
    assert wb.sheetnames == ["Invoices", "Line items", "Needs review", "Rejected", "Summary"]

    xlsx = sheet(exp_settings, "Invoices")
    csv_inv = csv_rows(out(exp_settings, "invoices_csv"))
    doc = json.loads(out(exp_settings, "json").read_text(encoding="utf-8"))
    assert len(xlsx) == len(csv_inv) == len(doc["accepted"]) == 5
    assert {r["Invoice number"] for r in xlsx} == {r["Invoice number"] for r in csv_inv} == \
           {i["invoice_number"] for i in doc["accepted"]}

    total_x = round(sum(r["Invoice total"] for r in xlsx), 2)
    total_c = round(sum(float(r["Invoice total"]) for r in csv_inv), 2)
    total_j = round(sum(float(i["totals"]["invoice_total"]) for i in doc["accepted"]), 2)
    assert total_x == total_c == total_j == 709.00 + 5.00 + 664.00 + 33.00 + 94.00
    assert doc["summary"]["accepted_totals"]["INR"]["invoice_total"] == "1505.00"


def test_purchase_register_columns_for_a_cgst_sgst_and_an_igst_invoice(exp_settings, drop):
    process_five(exp_settings, drop)
    rows = {r["Invoice number"]: r for r in sheet(exp_settings, "Invoices")}

    r = rows["CJB1-1303297"]                                    # Tamil Nadu to Tamil Nadu: CGST + SGST
    assert (r["Taxable value"], r["CGST"], r["SGST/UTGST"], r["Total tax"], r["Invoice total"]) == \
           (600.84, 54.08, 54.08, 108.16, 709.00)
    assert r["IGST"] is None and r["Approval"] == "Autopass"
    assert r["Invoice date"].date() == date(2026, 9, 19)         # a real Excel date, not text
    assert r["Seller GSTIN"] == "33AALCR3173P1ZU" and r["Source file"] == "five.pdf"

    r = rows["I1926JD000023630"]                                # inter-state: IGST only
    assert (r["Taxable value"], r["IGST"], r["Total tax"], r["Invoice total"]) == (562.71, 101.29, 101.29, 664.00)
    assert r["CGST"] is None


def test_line_items_sheet_and_csv(exp_settings, drop):
    process_five(exp_settings, drop)
    lines = sheet(exp_settings, "Line items")
    assert len(lines) == 7 == len(csv_rows(out(exp_settings, "lines_csv")))
    ship = next(r for r in lines if r["Description"] == "Shipping Charges")
    assert (ship["Discount"], ship["Taxable value"], ship["Line total"]) == (33.90, 0.00, 0.00)
    watch = next(r for r in lines if r["Invoice number"] == "I1926JD000023630")
    assert (watch["HSN/SAC"], watch["Gross amount"], watch["Tax rate %"], watch["IGST"]) == ("91021900", 3799.00, 18, 101.29)


def test_csv_is_excel_friendly_and_exact(exp_settings, drop):
    process_five(exp_settings, drop)
    raw = out(exp_settings, "invoices_csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # BOM: Excel on Windows reads UTF-8 correctly
    row = next(r for r in csv_rows(out(exp_settings, "invoices_csv")) if r["Invoice number"] == "CJB1-1303297")
    assert row["Invoice total"] == "709.00" and row["Invoice date"] == "2026-09-19" and row["IGST"] == ""


def test_json_keeps_full_detail(exp_settings, drop):
    process_five(exp_settings, drop)
    doc = json.loads(out(exp_settings, "json").read_text(encoding="utf-8"))
    inv = next(i for i in doc["accepted"] if i["invoice_number"] == "CJB1-1303297")
    assert inv["seller"] == {"name": "RETAILEZ PRIVATE LIMITED", "gstin": "33AALCR3173P1ZU"}
    assert inv["totals"]["invoice_total"] == "709.00" and len(inv["lines"]) == 2
    assert inv["approval"]["method"] == "Autopass" and inv["confidence"]["score"] >= 0.95
    assert inv["source"]["file"] == "five.pdf" and inv["payment"]["reference"].startswith("i93r4es3ij")


def test_tax_inclusive_invoice_exports_the_repaired_values(exp_settings, drop):
    drop("nykaa.pdf", nykaa_pdf())
    run_once(exp_settings, llm=FakeLLM({"*": [reply([nykaa_as_returned()])]}))
    [r] = sheet(exp_settings, "Invoices")
    assert (r["Taxable value"], r["CGST"], r["SGST/UTGST"], r["Total tax"], r["Invoice total"]) == \
           (1380.71, 114.42, 114.42, 228.84, 1609.55)
    lines = sheet(exp_settings, "Line items")
    maybelline = next(x for x in lines if x["Description"].startswith("Maybelline"))
    assert (maybelline["Taxable value"], maybelline["Line total"]) == (351.54, 414.82)   # not the model's 478.10


# ---- what is NOT in the accepted files -------------------------------------------------------------
def test_review_and_rejected_items_are_listed_separately(exp_settings, drop):
    digital = five()[2:]
    for n, inv in enumerate(digital, start=1):
        inv["source_pages"] = [n]
    drop("digital.pdf", pdf_for(digital))
    run_once(exp_settings, llm=FakeLLM({"*": [reply(digital)]}))

    scan = [scanned(myntra_watch(), "11926JD000023630"), scanned(myntra_fees()), scanned(DIGITAL[4](), None)]
    for s in scan:
        s["source_pages"] = [1]
        s.pop("grounding")
    drop("scan.pdf", make_scanned_pdf())
    run_once(exp_settings, llm=FakeLLM({"*": [reply(scan)]}))

    assert {r["Invoice number"] for r in sheet(exp_settings, "Invoices")} == \
           {"I1926JD000023630", "I2926MY227475971", "I1926FI015395867"}       # the scan added nothing
    [pending] = sheet(exp_settings, "Needs review")
    assert pending["Invoice number"] == "11926JD000023630" and "Looks like invoice" in pending["Why it needs review"]
    rejected = sheet(exp_settings, "Rejected")
    assert {r["Invoice number / file"] for r in rejected if r["Type"] == "Invoice"} == \
           {"I2926MY227475971", "I1926FI015395867"}
    assert all(r["Decided by"] == "Pipeline" and r["Duplicate of"].startswith("#") for r in rejected)


def test_files_that_never_became_invoices_show_up_as_rejected(exp_settings, drop):
    drop("not_a_pdf.pdf", fake_exe_bytes())
    run_once(exp_settings, llm=FakeLLM({"*": ["{}"]}))
    rows = sheet(exp_settings, "Rejected")
    assert [(r["Type"], r["Invoice number / file"], r["Reason"]) for r in rows] == \
           [("File", "not_a_pdf.pdf", "unsupported_type")]


def test_approving_a_reviewed_invoice_adds_it_to_the_next_export(exp_settings, drop, capsys):
    bad = retailez_backpack()
    bad["grand_total"] = "719.00"                                # arithmetic mismatch: goes to review
    ok = amazon_fee()
    ok["source_pages"] = [2]
    drop("mixed.pdf", pdf_for([bad, ok]))
    run_once(exp_settings, llm=FakeLLM({"*": [reply([bad, ok])]}))

    assert [r["Invoice number"] for r in sheet(exp_settings, "Invoices")] == ["MKT-353808321"]   # autopass only
    assert [r["Invoice number"] for r in sheet(exp_settings, "Needs review")] == ["CJB1-1303297"]

    assert review.main(["approve", "1", "--note", "checked against the paper copy"], exp_settings) == 0
    assert "exports refreshed" in capsys.readouterr().out
    rows = {r["Invoice number"]: r for r in sheet(exp_settings, "Invoices")}
    assert set(rows) == {"CJB1-1303297", "MKT-353808321"}
    assert rows["CJB1-1303297"]["Approval"] == "Approved by reviewer"
    assert rows["CJB1-1303297"]["Reviewer note"] == "checked against the paper copy"
    assert sheet(exp_settings, "Needs review") == []
    assert json.loads(out(exp_settings, "json").read_text(encoding="utf-8"))["summary"]["invoices"]["approved_by_reviewer"] == 1


def test_rejecting_a_reviewed_invoice_lists_it_as_rejected(exp_settings, drop):
    bad = retailez_backpack()
    bad["grand_total"] = "719.00"
    drop("bad.pdf", pdf_for([bad]))
    run_once(exp_settings, llm=FakeLLM({"*": [reply([bad])]}))
    review.main(["reject", "1", "--note", "wrong total"], exp_settings)
    assert sheet(exp_settings, "Invoices") == []
    [r] = sheet(exp_settings, "Rejected")
    assert r["Decided by"] == "Reviewer" and r["Reason"] == "Rejected by reviewer: wrong total"


# ---- the node, the command, and the edge cases ---------------------------------------------------------
def test_export_node_runs_once_per_file_even_when_all_three_routes_fire(exp_settings, drop, monkeypatch):
    from invoice_pipeline.export import ExportResult
    calls = []
    monkeypatch.setattr("invoice_pipeline.nodes.export.export_all",
                        lambda settings: calls.append(1) or ExportResult(paths=[Path("x")]))
    ok, bad, dup = amazon_fee(), retailez_backpack(), amazon_fee()
    bad["grand_total"] = "719.00"
    ok["source_pages"], bad["source_pages"], dup["source_pages"] = [1], [2], [3]
    drop("all_routes.pdf", pdf_for([ok, bad, dup]))
    [res] = run_once(exp_settings, llm=FakeLLM({"*": [reply([ok, bad, dup])]}))
    assert res.detail == "3 invoice(s): 1 autopass, 1 human review, 1 hard reject"
    assert calls == [1]


def test_a_locked_excel_file_does_not_break_the_run(exp_settings, drop, monkeypatch):
    real_replace = os.replace

    def locked(src, dst):
        if Path(dst).name == "invoices.xlsx":
            raise PermissionError("file is open in Excel")
        return real_replace(src, dst)
    monkeypatch.setattr("invoice_pipeline.export.exporter.os.replace", locked)
    result = export_all(exp_settings)
    assert result.used_fallback_names
    assert not out(exp_settings, "xlsx").exists()
    assert len(list(exp_settings.output_dir.glob("invoices_*.xlsx"))) == 1
    assert out(exp_settings, "json").exists() and not list(exp_settings.output_dir.glob("*.tmp"))


def test_empty_database_still_produces_valid_files(exp_settings):
    result = export_all(exp_settings)
    assert (result.accepted, result.needs_review, result.rejected) == (0, 0, 0)
    assert sheet(exp_settings, "Invoices") == [] and csv_rows(out(exp_settings, "invoices_csv")) == []
    doc = json.loads(out(exp_settings, "json").read_text(encoding="utf-8"))
    assert doc["accepted"] == [] and doc["summary"]["invoices"]["accepted"] == 0
    header = out(exp_settings, "invoices_csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.startswith("Invoice ID,Invoice number,Invoice date,Seller")


def test_exporting_twice_changes_nothing(exp_settings, drop):
    process_five(exp_settings, drop)
    first = sheet(exp_settings, "Invoices")
    export_all(exp_settings)
    assert sheet(exp_settings, "Invoices") == first
    with db.session(exp_settings.db_path) as conn:
        assert {r["status"] for r in conn.execute("select status from invoices")} == {"accepted"}


def test_summary_sheet_totals(exp_settings, drop):
    process_five(exp_settings, drop)
    ws = load_workbook(out(exp_settings, "xlsx"), data_only=True)["Summary"]
    cells = {r[0]: r[1] for r in ws.iter_rows(values_only=True) if r[0]}
    assert cells["Accepted"] == 5 and cells["Waiting for review"] == 0 and cells["Rejected"] == 0
    assert cells["Invoice total"] == 1505.00 and cells["Total tax"] == pytest.approx(108.16 + 0.76 + 101.29 + 5.04 + 0.0)


def test_export_command(exp_settings, drop, monkeypatch, capsys):
    process_five(exp_settings, drop)
    monkeypatch.setattr(export_cli, "get_settings", lambda: exp_settings)
    assert export_cli.main() == 0
    assert "accepted: 5" in capsys.readouterr().out
