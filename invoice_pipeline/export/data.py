"""Turns the database into export-ready records. Pure reads: nothing here changes any status.

What goes where:
  accepted      invoices with status 'accepted' (autopassed, or approved by a reviewer)
  needs review  open items in the review queue
  rejected      hard-rejected or reviewer-rejected invoices, plus files that never produced an invoice
Amounts are decimal strings with two places, as everywhere else in the pipeline.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Optional

TAX_GROUP = {"CGST": "cgst", "SGST": "sgst_utgst", "UTGST": "sgst_utgst", "IGST": "igst",
             "CESS": "cess", "OTHER": "other"}
GROUPS = ("cgst", "sgst_utgst", "igst", "cess", "other")
ZERO = Decimal(0)


def D(v) -> Optional[Decimal]:
    if v in (None, ""):
        return None
    try:
        return Decimal(str(v))
    except InvalidOperation:
        return None


def q(d: Optional[Decimal]) -> Optional[str]:
    return None if d is None else f"{d.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def first(*values):
    return next((v for v in values if v is not None), None)


def tax_split(taxes) -> dict:
    out: dict = {g: None for g in GROUPS}
    for t in taxes or []:
        amount = D(t.get("amount"))
        if amount is None:
            continue
        g = TAX_GROUP.get(t.get("type"), "other")
        out[g] = (out[g] or ZERO) + amount
    return out


@dataclass
class ExportData:
    generated_at: str
    accepted: list = field(default_factory=list)       # one dict per invoice, with a "lines" list
    review: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    summary: dict = field(default_factory=dict)

    @property
    def line_rows(self) -> list:
        rows = []
        for inv in self.accepted:
            for ln in inv["lines"]:
                rows.append({"invoice_id": inv["invoice_id"], "invoice_number": inv["invoice_number"],
                             "invoice_date": inv["invoice_date"], "seller_name": inv["seller_name"], **ln})
        return rows


def _accepted_record(row, review, job) -> dict:
    inv = json.loads(row["data_json"] or "{}")
    seller, buyer, pay = inv.get("seller") or {}, inv.get("buyer") or {}, inv.get("payment") or {}
    rows = inv.get("line_items") or []

    split: dict = {g: None for g in GROUPS}
    lines = []
    for i, r in enumerate(rows, start=1):
        s = tax_split(r.get("taxes"))
        for g in GROUPS:
            if s[g] is not None:
                split[g] = (split[g] or ZERO) + s[g]
        rates = [D(t.get("rate")) for t in r.get("taxes") or [] if t.get("type") not in ("CESS", "OTHER")]
        rates = [x for x in rates if x is not None]
        lines.append({
            "line": i, "description": r.get("description"), "hsn_sac": r.get("hsn_sac"),
            "quantity": r.get("quantity"), "unit_price": q(D(r.get("unit_price"))),
            "gross_amount": q(D(r.get("gross_amount"))), "discount": q(D(r.get("discount"))),
            "taxable_value": q(D(r.get("net_amount"))),
            "tax_rate": str(sum(rates, ZERO).normalize()) if rates else None,
            **{g: q(s[g]) for g in ("cgst", "sgst_utgst", "igst", "cess")},
            "line_total": q(D(r.get("line_total"))),
        })

    known = [v for v in split.values() if v is not None]
    total_tax = first(D(inv.get("total_tax")), sum(known, ZERO) if known else None)
    taxable = first(D(inv.get("subtotal")), D(row["computed_subtotal"]))

    return {
        "invoice_id": row["invoice_id"], "invoice_number": row["invoice_number"],
        "invoice_date": row["invoice_date"], "document_type": inv.get("document_type"),
        "seller_name": seller.get("name"), "seller_gstin": seller.get("gstin"),
        "buyer_name": buyer.get("name"), "place_of_supply": inv.get("place_of_supply"),
        "order_number": inv.get("order_number"), "currency": row["currency"],
        "taxable_value": q(taxable),
        **{g: q(split[g]) for g in ("cgst", "sgst_utgst", "igst", "cess")},
        "total_tax": q(total_tax), "invoice_total": q(D(row["grand_total"])),
        "payment_mode": pay.get("mode"), "payment_ref": pay.get("transaction_id"),
        "approval": "Autopass" if row["route"] == "autopass" else "Approved by reviewer",
        "reviewed_at": review["resolved_at"] if review else None,
        "reviewer_note": review["note"] if review else None,
        "score": row["score"], "source_file": job["original_filename"] if job else None,
        "job_id": row["job_id"], "sha256": row["sha256"], "source_pages": inv.get("source_pages"),
        "reference_numbers": inv.get("reference_numbers"),
        "field_confidence": json.loads(row["field_confidence_json"] or "{}"),
        "flags": [{"code": f["code"], "severity": f["severity"], "message": f["message"]}
                  for f in json.loads(row["flags_json"] or "[]")],
        "lines": lines,
    }


def load_export_data(conn, generated_at: str) -> ExportData:
    jobs = {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs")}
    invoices = conn.execute("SELECT * FROM invoices ORDER BY invoice_id").fetchall()
    numbers = {r["invoice_id"]: r["invoice_number"] for r in invoices}
    reviews = {}
    for r in conn.execute("SELECT * FROM review_queue ORDER BY review_id"):
        if r["invoice_id"] is not None:
            reviews[r["invoice_id"]] = r          # the latest decision wins

    data = ExportData(generated_at=generated_at)

    for row in invoices:
        if row["status"] == "accepted":
            data.accepted.append(_accepted_record(row, reviews.get(row["invoice_id"]), jobs.get(row["job_id"])))
        elif row["status"] == "rejected":
            flags = json.loads(row["flags_json"] or "[]")
            blockers = [f["message"] for f in flags if f["severity"] == "blocker"]
            by_pipeline = row["route"] == "hard_reject"
            rv = reviews.get(row["invoice_id"])
            reason = "; ".join(blockers) if by_pipeline and blockers else (
                "Rejected by reviewer" + (f": {rv['note']}" if rv and rv["note"] else ""))
            dup = row["duplicate_of"]
            data.rejected.append({
                "kind": "Invoice", "invoice_id": row["invoice_id"], "invoice_number": row["invoice_number"],
                "seller_name": row["seller_name"], "invoice_total": q(D(row["grand_total"])),
                "currency": row["currency"], "reason": reason,
                "duplicate_of": f"#{dup} ({numbers.get(dup)})" if dup else None,
                "decided_by": "Pipeline" if by_pipeline else "Reviewer",
                "source_file": jobs[row["job_id"]]["original_filename"] if row["job_id"] in jobs else None,
                "when": row["updated_at"],
            })

    for r in conn.execute("SELECT * FROM review_queue WHERE status='open' ORDER BY review_id"):
        inv = next((x for x in invoices if x["invoice_id"] == r["invoice_id"]), None)
        job = jobs.get(r["job_id"])
        data.review.append({
            "review_id": r["review_id"], "invoice_id": r["invoice_id"],
            "invoice_number": inv["invoice_number"] if inv else None,
            "seller_name": inv["seller_name"] if inv else None,
            "invoice_total": q(D(inv["grand_total"])) if inv else None,
            "currency": inv["currency"] if inv else None, "score": inv["score"] if inv else None,
            "reasons": "\n".join(json.loads(r["reasons_json"] or "[]")),
            "reasons_list": json.loads(r["reasons_json"] or "[]"),
            "source_file": job["original_filename"] if job else None, "created_at": r["created_at"],
        })

    file_counts = Counter(j["status"] for j in jobs.values())
    for j in jobs.values():       # files that never became an invoice row
        if j["status"] in ("rejected", "failed", "duplicate") and not any(
                i["job_id"] == j["job_id"] for i in invoices):
            reason = (j["rejection_reason"] or j["error_message"]
                      or (f"Duplicate of job {j['duplicate_of']}" if j["duplicate_of"] else j["status"]))
            data.rejected.append({
                "kind": "File", "invoice_id": None, "invoice_number": j["original_filename"],
                "seller_name": None, "invoice_total": None, "currency": None, "reason": reason,
                "duplicate_of": f"job {j['duplicate_of']}" if j["duplicate_of"] else None,
                "decided_by": "Pipeline", "source_file": j["original_filename"], "when": j["finished_at"],
            })

    data.summary = _summary(data, file_counts)
    return data


def _summary(data: ExportData, file_counts: Counter) -> dict:
    by_currency: dict = {}
    by_seller: dict = {}
    for inv in data.accepted:
        cur = inv["currency"] or "?"
        tot = by_currency.setdefault(cur, {k: ZERO for k in
                                           ("taxable_value", "cgst", "sgst_utgst", "igst", "cess", "total_tax", "invoice_total")}
                                     | {"invoices": 0})
        tot["invoices"] += 1
        for k in ("taxable_value", "cgst", "sgst_utgst", "igst", "cess", "total_tax", "invoice_total"):
            tot[k] += D(inv[k]) or ZERO
        key = (inv["seller_gstin"] or inv["seller_name"], cur)
        s = by_seller.setdefault(key, {"seller": inv["seller_name"], "gstin": inv["seller_gstin"], "currency": cur,
                                       "invoices": 0, "taxable_value": ZERO, "total_tax": ZERO, "invoice_total": ZERO})
        s["invoices"] += 1
        for k in ("taxable_value", "total_tax", "invoice_total"):
            s[k] += D(inv[k]) or ZERO

    def fmt(d: dict) -> dict:
        return {k: (q(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

    rejected_inv = [r for r in data.rejected if r["kind"] == "Invoice"]
    return {
        "generated_at": data.generated_at,
        "files": dict(file_counts),
        "invoices": {
            "accepted": len(data.accepted),
            "autopass": sum(1 for i in data.accepted if i["approval"] == "Autopass"),
            "approved_by_reviewer": sum(1 for i in data.accepted if i["approval"] != "Autopass"),
            "awaiting_review": len(data.review),
            "rejected": len(rejected_inv),
            "rejected_as_duplicates": sum(1 for r in rejected_inv if r["duplicate_of"]),
        },
        "accepted_totals": {cur: fmt(t) for cur, t in by_currency.items()},
        "by_seller": sorted((fmt(s) for s in by_seller.values()),
                            key=lambda s: -(D(s["invoice_total"]) or ZERO)),
    }
