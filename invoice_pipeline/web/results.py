"""Turns one session's database into the JSON the web page shows.

Read-only. The shape is the contract of the UI:

    {"files":   [{"name", "outcome", "reason", "invoices": [...]?}],
     "summary": {...counts...}}

outcome: done | rejected | duplicate | failed | deferred | no_invoice | processing
invoice.validation.route: autopass | human_review | approved | reviewer_rejected | rejected
"""
from __future__ import annotations

import json
from decimal import Decimal
from typing import Optional

from .. import db
from ..export.data import ZERO, D, q, tax_split
from ..reasons import confirmation_text, file_reason_text, plain_ids

_DUP_CODES = ("DUPLICATE_EXACT", "DUPLICATE_LOOKALIKE", "DUPLICATE_PROBABLE", "DUPLICATE_NUMBER_DIFFERENT_TOTAL")


def _where(job: Optional[dict]) -> str:
    return f"from {job['original_filename']}" if job else "from an earlier file"


def _friendly(flag: dict, invoices: dict, jobs: dict) -> str:
    """Flag messages mention internal ids ('invoice #3'); show the invoice number and file instead."""
    rel = flag.get("related_invoice_id")
    other = invoices.get(rel) if rel else None
    if flag["code"] not in _DUP_CODES or other is None:
        return plain_ids(flag["message"])
    num, where = other["invoice_number"] or "(no number)", _where(jobs.get(other["job_id"]))
    code = flag["code"]
    if code == "DUPLICATE_EXACT":
        return f"Exact duplicate of invoice {num} ({where}): same seller, invoice number and total"
    if code == "DUPLICATE_LOOKALIKE":
        return (f"Looks like invoice {num} ({where}); the numbers differ only by look-alike characters "
                f"(I/1/L, O/0). Probably the same invoice read differently. Please confirm")
    if code == "DUPLICATE_PROBABLE":
        return f"Same seller, date and total as invoice {num} ({where}) but a different invoice number"
    return (f"Same seller and invoice number as invoice {num} ({where}) but the total differs. "
            f"A revised invoice or a misread?")


def _line(i: int, r: dict) -> dict:
    split = tax_split(r.get("taxes"))
    amounts = [v for v in split.values() if v is not None]
    rates = [x for x in (D(t.get("rate")) for t in r.get("taxes") or [] if t.get("type") not in ("CESS", "OTHER"))
             if x is not None]
    return {
        "line": r.get("line_no") or i, "description": r.get("description"), "hsn_sac": r.get("hsn_sac"),
        "quantity": r.get("quantity"), "unit_price": q(D(r.get("unit_price"))),
        "discount": q(D(r.get("discount"))), "taxable_value": q(D(r.get("net_amount"))),
        "tax_rate": str(sum(rates, ZERO).normalize()) if rates else None,
        "tax_amount": q(sum(amounts, ZERO)) if amounts else None,
        "line_total": q(D(r.get("line_total"))),
    }


def _invoice(row, review, reviews_open, invoices, jobs) -> dict:
    inv = json.loads(row["data_json"] or "{}")
    seller, buyer, pay = inv.get("seller") or {}, inv.get("buyer") or {}, inv.get("payment") or {}
    split: dict = {}
    for r in inv.get("line_items") or []:
        for g, v in tax_split(r.get("taxes")).items():
            if v is not None:
                split[g] = split.get(g, ZERO) + v
    known = list(split.values())
    flags = json.loads(row["flags_json"] or "[]")
    status = row["status"]
    note = review["note"] if review else None

    if status == "accepted":
        route = "approved" if review and review["status"] == "approved" else "autopass"
    elif status == "rejected":
        route = "rejected" if row["route"] == "hard_reject" and not (review and review["status"] == "rejected") \
            else "reviewer_rejected"
    else:
        route = "human_review"

    open_item = reviews_open.get(row["invoice_id"])
    why: list[str] = []
    if route == "rejected":
        why = [_friendly(f, invoices, jobs) for f in flags if f["severity"] == "blocker"]
    elif route == "reviewer_rejected":
        why = ["Rejected by the reviewer" + (f": {note}" if note else "")]
    elif route == "human_review":
        why = [plain_ids(x) for x in json.loads(open_item["reasons_json"] or "[]")] if open_item else []

    return {
        "invoice_id": row["invoice_id"],
        "document_type": inv.get("document_type"), "invoice_number": row["invoice_number"],
        "invoice_date": row["invoice_date"], "due_date": inv.get("due_date"),
        "order_number": inv.get("order_number"), "place_of_supply": inv.get("place_of_supply"),
        "currency": row["currency"] or "INR", "payment_mode": pay.get("mode"),
        "seller": {"name": seller.get("name"), "gstin": seller.get("gstin")},
        "buyer": {"name": buyer.get("name"), "gstin": buyer.get("gstin")},
        "line_items": [_line(i, r) for i, r in enumerate(inv.get("line_items") or [], start=1)],
        "taxable_value": q(D(inv.get("subtotal")) or D(row["computed_subtotal"])),
        "cgst": q(split.get("cgst")), "sgst": q(split.get("sgst_utgst")), "igst": q(split.get("igst")),
        "cess": q(split.get("cess")),
        "total_tax": q(D(inv.get("total_tax")) if D(inv.get("total_tax")) is not None
                       else (sum(known, ZERO) if known else None)),
        "invoice_total": q(D(row["grand_total"])),
        "validation": {
            "route": route, "score": float(row["score"] or 0), "review_id": open_item["review_id"] if open_item else None,
            "flags": [{"severity": f["severity"], "code": f["code"], "message": _friendly(f, invoices, jobs)}
                      for f in flags],
            "confirmations": [confirmation_text(c) for c in json.loads(row["confirmations_json"] or "[]")],
            "reasons": why, "note": note,
        },
    }


def _file_entry(job, rows, reviews, reviews_open, invoices, jobs) -> dict:
    status, name = job["status"], job["original_filename"]
    entry: dict = {"name": name, "job_id": job["job_id"]}
    inv_json = [_invoice(r, reviews.get(r["invoice_id"]), reviews_open, invoices, jobs) for r in rows]

    if status == "duplicate":
        original = None
        for r in rows:
            for f in json.loads(r["flags_json"] or "[]"):
                if f["code"] == "DUPLICATE_EXACT" and f.get("related_invoice_id") in invoices:
                    original = invoices[f["related_invoice_id"]]
        if original is not None:
            num = original["invoice_number"] or "(no number)"
            entry["reason"] = f"Already processed: invoice {num} {_where(jobs.get(original['job_id']))}"
            entry["original"] = {"invoice_number": original["invoice_number"],
                                 "file": jobs[original["job_id"]]["original_filename"] if original["job_id"] in jobs else None}
        elif job["duplicate_of"] in jobs:
            entry["reason"] = f"This exact file was already processed ({jobs[job['duplicate_of']]['original_filename']})"
            entry["original"] = {"invoice_number": None, "file": jobs[job["duplicate_of"]]["original_filename"]}
        else:
            entry["reason"] = "Duplicate of a file that was already processed"
        entry["outcome"] = "duplicate"
        return entry

    if status == "rejected":
        entry["outcome"] = "rejected"
        if rows:
            blockers = [m for i in inv_json for m in i["validation"]["reasons"]]
            entry["reason"] = "; ".join(blockers) or "Failed a blocking check"
            entry["invoices"] = inv_json
        else:
            entry["reason"] = file_reason_text(job["rejection_reason"], job["rejection_detail"])
            if job["rejection_detail"]:
                entry["detail"] = job["rejection_detail"]
        return entry

    if status == "failed":
        entry["outcome"] = "failed"
        entry["reason"] = ("The model could not turn this file into valid data. Try a clearer copy."
                           if "model" in (job["error_message"] or "").lower() or "extraction" in (job["error_message"] or "").lower()
                           else "An unexpected error stopped this file. It was set aside.")
        return entry

    if status == "pending":
        entry["outcome"] = "deferred"
        entry["reason"] = "The model service was busy or out of quota. The file was not processed; try again later."
        return entry

    if status == "processing":
        entry["outcome"] = "processing"
        entry["reason"] = "Still being processed"
        return entry

    if not rows:
        entry["outcome"] = "no_invoice"
        entry["reason"] = "No invoice was found in this file"
        return entry
    entry["outcome"] = "done"
    entry["invoices"] = inv_json
    return entry


def build_result(settings) -> dict:
    db.init_db(settings.db_path)
    with db.session(settings.db_path) as conn:
        jobs = {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs ORDER BY job_id")}
        all_rows = conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()
        reviews: dict = {}
        reviews_open: dict = {}
        for r in conn.execute("SELECT * FROM review_queue ORDER BY review_id"):
            if r["invoice_id"] is None:
                continue
            reviews[r["invoice_id"]] = r
            if r["status"] == "open":
                reviews_open[r["invoice_id"]] = r
    invoices = {r["invoice_id"]: r for r in all_rows}
    by_job: dict = {}
    for r in all_rows:
        by_job.setdefault(r["job_id"], []).append(r)

    files = [_file_entry(j, by_job.get(jid, []), reviews, reviews_open, invoices, jobs) for jid, j in jobs.items()]
    routes = [i["validation"]["route"] for f in files for i in f.get("invoices", [])]
    summary = {
        "files": len(files),
        "invoices": len(routes),
        "autopass": routes.count("autopass") + routes.count("approved"),
        "needs_review": routes.count("human_review"),
        "rejected_invoices": routes.count("rejected") + routes.count("reviewer_rejected"),
        "rejected_files": sum(1 for f in files if f["outcome"] in ("rejected", "failed", "no_invoice") and not f.get("invoices")),
        "duplicate_files": sum(1 for f in files if f["outcome"] == "duplicate"),
        "deferred_files": sum(1 for f in files if f["outcome"] == "deferred"),
    }
    return {"files": files, "summary": summary}
