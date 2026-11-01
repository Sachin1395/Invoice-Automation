"""File writers: Excel, CSV and JSON. Each takes an ExportData and a path."""
from __future__ import annotations

import csv
import json
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from .columns import INVOICE_COLUMNS, LINE_COLUMNS, REJECTED_COLUMNS, REVIEW_COLUMNS
from .data import ExportData

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(bold=True, color="FFFFFF")
MONEY_FMT = "#,##0.00"


_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _cell_value(value, kind: str):
    """Excel gets real numbers and dates (so sorting and sums work); anything unparseable stays text."""
    if value in (None, ""):
        return None
    try:
        if kind == "money":
            return float(Decimal(str(value)))
        if kind in ("num", "score"):
            return float(Decimal(str(value)))
        if kind == "int":
            return int(value)
        if kind == "date":
            return date.fromisoformat(str(value)[:10])
    except (InvalidOperation, ValueError):
        return str(value)
    return str(value)


def _write_table(ws, columns, rows) -> None:
    for j, (_, title, _) in enumerate(columns, start=1):
        c = ws.cell(row=1, column=j, value=title)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        c.alignment = Alignment(vertical="center")
    widths = [len(title) for _, title, _ in columns]
    for i, row in enumerate(rows, start=2):
        for j, (key, _, kind) in enumerate(columns, start=1):
            v = _cell_value(row.get(key), kind)
            c = ws.cell(row=i, column=j, value=v)
            if isinstance(v, str) and v[:1] in _FORMULA_START:
                c.data_type = "s"          # text from an uploaded document must never run as a formula
            if kind == "money":
                c.number_format = MONEY_FMT
            elif kind == "date":
                c.number_format = "DD-MMM-YYYY"
            elif kind == "score":
                c.number_format = "0.00"
            elif kind == "wrap":
                c.alignment = Alignment(wrap_text=True, vertical="top")
            if v is not None:
                longest = max(len(part) for part in str(v if kind != "money" else f"{v:,.2f}").split("\n"))
                widths[j - 1] = max(widths[j - 1], longest)
    for j, (_, _, kind) in enumerate(columns, start=1):
        ws.column_dimensions[get_column_letter(j)].width = min(max(widths[j - 1] + 2, 10), 60)
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions


def _write_summary(ws, data: ExportData) -> None:
    s = data.summary
    r = 1

    def put(label, value="", bold=False):
        nonlocal r
        a = ws.cell(row=r, column=1, value=label)
        if bold:
            a.font = Font(bold=True)
        if value != "":
            ws.cell(row=r, column=2, value=value)
        r += 1

    def header(text):
        nonlocal r
        r += 1
        c = ws.cell(row=r, column=1, value=text)
        c.fill, c.font = HEADER_FILL, HEADER_FONT
        ws.cell(row=r, column=2).fill = HEADER_FILL
        r += 1

    put("Invoice pipeline export", bold=True)
    put("Generated (UTC)", s["generated_at"])

    header("Files")
    for status, n in sorted(s["files"].items()):
        put(status, n)

    header("Invoices")
    inv = s["invoices"]
    put("Accepted", inv["accepted"])
    put("   of which autopass", inv["autopass"])
    put("   of which approved by a reviewer", inv["approved_by_reviewer"])
    put("Waiting for review", inv["awaiting_review"])
    put("Rejected", inv["rejected"])
    put("   of which duplicates", inv["rejected_as_duplicates"])

    for cur, t in s["accepted_totals"].items():
        header(f"Accepted invoices, totals in {cur}")
        for label, key in (("Invoices", "invoices"), ("Taxable value", "taxable_value"), ("CGST", "cgst"),
                           ("SGST/UTGST", "sgst_utgst"), ("IGST", "igst"), ("Cess", "cess"),
                           ("Total tax", "total_tax"), ("Invoice total", "invoice_total")):
            cell_value = t[key] if key == "invoices" else float(Decimal(t[key]))
            put(label, cell_value)
            if key != "invoices":
                ws.cell(row=r - 1, column=2).number_format = MONEY_FMT

    if s["by_seller"]:
        header("By seller (accepted invoices)")
        titles = ["Seller", "GSTIN", "Currency", "Invoices", "Taxable value", "Total tax", "Invoice total"]
        for j, t in enumerate(titles, start=1):
            c = ws.cell(row=r, column=j, value=t)
            c.font = Font(bold=True)
        r += 1
        for sel in s["by_seller"]:
            vals = [sel["seller"], sel["gstin"], sel["currency"], sel["invoices"],
                    float(Decimal(sel["taxable_value"])), float(Decimal(sel["total_tax"])),
                    float(Decimal(sel["invoice_total"]))]
            for j, v in enumerate(vals, start=1):
                c = ws.cell(row=r, column=j, value=v)
                if j >= 5:
                    c.number_format = MONEY_FMT
            r += 1

    ws.column_dimensions["A"].width = 44
    ws.column_dimensions["B"].width = 26
    for col in "CDEFG":
        ws.column_dimensions[col].width = 16


def write_xlsx(path: Path, data: ExportData) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "Invoices"
    _write_table(ws, INVOICE_COLUMNS, data.accepted)
    _write_table(wb.create_sheet("Line items"), LINE_COLUMNS, data.line_rows)
    _write_table(wb.create_sheet("Needs review"), REVIEW_COLUMNS, data.review)
    _write_table(wb.create_sheet("Rejected"), REJECTED_COLUMNS, data.rejected)
    _write_summary(wb.create_sheet("Summary"), data)
    wb.save(path)


def _csv_value(value, kind: str):
    if value is None:
        return ""
    text = str(value)
    if kind not in ("money", "num", "score", "int", "date") and text[:1] in _FORMULA_START:
        return "'" + text            # spreadsheet apps would otherwise run it as a formula
    return text


def write_csv(path: Path, columns, rows) -> None:
    # utf-8 with BOM so Excel on Windows shows names and symbols correctly
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([title for _, title, _ in columns])
        for row in rows:
            w.writerow([_csv_value(row.get(key), kind) for key, _, kind in columns])


def write_invoices_csv(path: Path, data: ExportData) -> None:
    write_csv(path, INVOICE_COLUMNS, data.accepted)


def write_lines_csv(path: Path, data: ExportData) -> None:
    write_csv(path, LINE_COLUMNS, data.line_rows)


def _json_invoice(inv: dict) -> dict:
    return {
        "invoice_id": inv["invoice_id"], "invoice_number": inv["invoice_number"],
        "invoice_date": inv["invoice_date"], "document_type": inv["document_type"],
        "seller": {"name": inv["seller_name"], "gstin": inv["seller_gstin"]},
        "buyer": {"name": inv["buyer_name"]}, "place_of_supply": inv["place_of_supply"],
        "order_number": inv["order_number"], "reference_numbers": inv["reference_numbers"],
        "currency": inv["currency"],
        "totals": {k: inv[k] for k in ("taxable_value", "cgst", "sgst_utgst", "igst", "cess",
                                       "total_tax", "invoice_total")},
        "payment": {"mode": inv["payment_mode"], "reference": inv["payment_ref"]},
        "lines": [{k: v for k, v in ln.items()} for ln in inv["lines"]],
        "approval": {"method": inv["approval"], "reviewed_at": inv["reviewed_at"], "note": inv["reviewer_note"]},
        "confidence": {"score": inv["score"], "fields": inv["field_confidence"]},
        "checks": inv["flags"],
        "source": {"file": inv["source_file"], "job_id": inv["job_id"], "sha256": inv["sha256"],
                   "pages": inv["source_pages"]},
    }


def write_json(path: Path, data: ExportData) -> None:
    doc = {
        "schema_version": 1,
        "generated_at": data.generated_at,
        "note": "Amounts are decimal strings. 'accepted' is the only list meant for downstream use.",
        "summary": data.summary,
        "accepted": [_json_invoice(i) for i in data.accepted],
        "needs_review": [{k: v for k, v in r.items() if k not in ("reasons",)} for r in data.review],
        "rejected": data.rejected,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, ensure_ascii=False)
