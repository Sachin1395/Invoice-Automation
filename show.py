"""Print what was extracted and how it was validated.

    python show.py            last 3 extractions
    python show.py 10         last 10
    python show.py json       full JSON of the latest extraction
"""
import json
import sqlite3
import sys

db = sqlite3.connect("data/invoices.db")
db.row_factory = sqlite3.Row
arg = sys.argv[1] if len(sys.argv) > 1 else "3"

if arg == "json":
    row = db.execute("select parsed_json from extractions where status='ok' order by extraction_id desc limit 1").fetchone()
    print(json.dumps(json.loads(row["parsed_json"]), indent=2, ensure_ascii=False) if row else "nothing yet")
    sys.exit()

rows = db.execute("select * from extractions order by extraction_id desc limit ?", (int(arg),)).fetchall()
for e in reversed(rows):
    print(f"\nextraction {e['extraction_id']} [{e['status']}] model={e['model_used']} prompt={e['prompt_version']} "
          f"tokens={e['input_tokens']}/{e['output_tokens']} {e['latency_ms']}ms")
    if not e["parsed_json"]:
        print("  error:", e["error"])
        print("  raw:", (e["raw_response"] or "")[:500])
        continue
    stored = {r["seq"]: r for r in db.execute("select * from invoices where extraction_id=? order by seq", (e["extraction_id"],))}
    for i, inv in enumerate(json.loads(e["parsed_json"])["invoices"]):
        s = inv["seller"]
        print(f"  pages {inv['source_pages']}  #{inv['invoice_number']}  {inv['invoice_date']}  {s['name']}  "
              f"GSTIN {s['gstin']}  total={inv['grand_total']} {inv['currency']}")
        print("    grounding: ", {k: v for k, v in inv["grounding"].items() if v is not None})
        for li in inv["line_items"]:
            taxes = ", ".join(f"{t['type']} {t['rate']}%={t['amount']}" for t in li["taxes"])
            print(f"    line {li['line_no']}: {(li['description'] or '')[:36]!r} hsn={li['hsn_sac']} qty={li['quantity']} "
                  f"unit={li['unit_price']} gross={li.get('gross_amount')} disc={li['discount']} net={li['net_amount']} "
                  f"[{taxes}] total={li['line_total']}")
        print(f"    totals: subtotal={inv['subtotal']} tax={inv['total_tax']} grand={inv['grand_total']} "
              f"| order: {inv['order_number']} | refs: {inv['reference_numbers']}")
        r = stored.get(i)
        if r:
            print(f"    -> {r['status'].upper()} (route {r['route']}, score {r['score']}) invoice #{r['invoice_id']}")
            for f in json.loads(r["flags_json"]):
                print(f"       [{f['severity']}] {f['code']}: {f['message']}")
