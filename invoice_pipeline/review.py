"""Minimal review commands.

    python -m invoice_pipeline.review list [--all]
    python -m invoice_pipeline.review show 3
    python -m invoice_pipeline.review approve 3 [--note "checked against the paper copy"]
    python -m invoice_pipeline.review reject 3 [--note "..."]

Approving marks the invoice accepted; rejecting marks it rejected. Correcting fields by hand
comes later, along with a proper review screen.
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from . import db
from .config import Settings, get_settings


def _list(conn, show_all: bool) -> int:
    rows = db.list_review(conn, None if show_all else "open")
    if not rows:
        print("nothing to review" if not show_all else "review queue is empty")
        return 0
    for r in rows:
        reasons = json.loads(r["reasons_json"] or "[]")
        who = f"invoice #{r['invoice_id']}" if r["invoice_id"] else "whole file"
        print(f"[{r['review_id']:>3}] {r['status']:<8} {who:<14} {r['summary'] or ''}")
        if r["score"] is not None:
            print(f"        score {r['score']:.2f}")
        for reason in reasons[:3]:
            print(f"        - {reason}")
        if len(reasons) > 3:
            print(f"        - ... and {len(reasons) - 3} more (use 'show {r['review_id']}')")
    return 0


def _show(conn, settings: Settings, review_id: int) -> int:
    r = db.get_review(conn, review_id)
    if r is None:
        print(f"no review item {review_id}")
        return 1
    print(f"review item {r['review_id']}  [{r['status']}]  {r['summary'] or ''}")
    for reason in json.loads(r["reasons_json"] or "[]"):
        print(f"  why: {reason}")
    pages = sorted((settings.pages_dir / (r["sha256"] or "_")).glob("page_*.jpg"))
    if pages:
        print("  page images:")
        for p in pages:
            print(f"    {p}")
    if r["invoice_id"] is None:
        return 0
    inv = db.get_invoice(conn, r["invoice_id"])
    data = json.loads(inv["data_json"])
    seller = data.get("seller") or {}
    print(f"\n  invoice #{inv['invoice_id']}  {inv['invoice_number']}  dated {inv['invoice_date']}")
    print(f"  seller {seller.get('name')}  GSTIN {seller.get('gstin')}")
    print(f"  taxable {inv['subtotal'] or inv['computed_subtotal']}  tax {inv['total_tax']}  "
          f"total {inv['grand_total']} {inv['currency']}   score {inv['score']}")
    for li in data.get("line_items") or []:
        taxes = ", ".join(f"{t['type']} {t['rate']}%={t['amount']}" for t in li.get("taxes") or [])
        print(f"    row {li.get('line_no')}: {(li.get('description') or '')[:44]!r} net={li.get('net_amount')} "
              f"[{taxes}] total={li.get('line_total')}")
    print("  field confidence:", {k: v for k, v in json.loads(inv["field_confidence_json"]).items() if v is not None})
    print("  checks:")
    for f in json.loads(inv["flags_json"]):
        print(f"    [{f['severity']:<7}] {f['code']}: {f['message']}")
        if f.get("related_invoice_id"):
            other = db.get_invoice(conn, f["related_invoice_id"])
            if other:
                print(f"              -> invoice #{other['invoice_id']} {other['invoice_number']} "
                      f"{other['grand_total']} on {other['invoice_date']} ({other['status']}, job {other['job_id']})")
    codes = {f["code"] for f in json.loads(inv["flags_json"])}
    if "DUPLICATE_LOOKALIKE" in codes:
        print("\n  hint: this looks like another copy of an invoice already stored. If the paper copy shows the same "
              "invoice, REJECT this item (approving would count it twice). Approve only if it is a different invoice.")
    elif codes & {"DUPLICATE_NUMBER_DIFFERENT_TOTAL", "DUPLICATE_PROBABLE"}:
        print("\n  hint: compare with the invoice linked above. Reject if it is the same invoice; approve if it is a "
              "separate one (or a genuine revision).")
    return 0


def main(argv: Optional[list] = None, settings: Optional[Settings] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m invoice_pipeline.review")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_list = sub.add_parser("list", help="show open review items")
    p_list.add_argument("--all", action="store_true", help="include resolved items")
    for name in ("show", "approve", "reject"):
        p = sub.add_parser(name)
        p.add_argument("review_id", type=int)
        if name != "show":
            p.add_argument("--note", default=None)
    args = parser.parse_args(argv)

    settings = settings or get_settings()
    db.init_db(settings.db_path)
    with db.session(settings.db_path) as conn:
        if args.cmd == "list":
            return _list(conn, args.all)
        if args.cmd == "show":
            return _show(conn, settings, args.review_id)
        err = db.resolve_review(conn, args.review_id, approve=(args.cmd == "approve"), note=args.note)
        if err:
            print(err)
            return 1
        print(f"review item {args.review_id} {'approved' if args.cmd == 'approve' else 'rejected'}")
    if settings.export_enabled:      # approved invoices join the accepted files straight away
        try:
            from .export import export_all
            export_all(settings)
            print(f"exports refreshed in {settings.output_dir}")
        except Exception as exc:
            print(f"(export not refreshed: {exc}; run 'python -m invoice_pipeline.export')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
