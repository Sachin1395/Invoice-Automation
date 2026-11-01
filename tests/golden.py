"""Golden invoices: the five digital invoices from the real sample PDFs, as the extraction node
returns them, plus helpers to derive scanned / broken variants. Buyer names are anonymised."""
from __future__ import annotations

import copy

import pymupdf

TXN = "i93r4es3ijW1IDli8eSVkuq3Dynmqo3pVSz"


def _conf(v=1.0):
    return dict(invoice_number=v, invoice_date=v, seller_name=v, seller_gstin=v, buyer_name=v,
                subtotal=v, total_tax=v, grand_total=v, currency=v)


def _tax(kind, rate, amt):
    return {"type": kind, "rate": rate, "amount": amt}


def _row(no, desc, **kw):
    base = dict(line_no=no, description=desc, hsn_sac=None, quantity=None, unit_price=None, gross_amount=None,
                other_charges=None, discount=None, net_amount=None, taxes=[], line_total=None, confidence=1.0)
    base.update(kw)
    return base


def _inv(**kw):
    base = dict(source_pages=[1], document_type="Tax Invoice", invoice_number=None, invoice_date=None,
                invoice_date_raw=None, due_date=None, order_number=None, reference_numbers=[],
                place_of_supply="TAMIL NADU", reverse_charge=False, currency="INR",
                seller={"name": None, "address": None, "gstin": None, "pan": None, "state_code": None},
                buyer={"name": "Test Buyer", "address": None, "gstin": None, "pan": None, "state_code": "33"},
                line_items=[], subtotal=None, total_tax=None, grand_total=None, other_charges=None,
                round_off=None, amount_in_words=None,
                payment={"transaction_id": None, "mode": None, "paid_at": None, "amount_paid": None},
                confidence=_conf())
    base.update(kw)
    return base


def retailez_backpack():           # CGST+SGST, a negative discount on the shipping row
    return _inv(
        invoice_number="CJB1-1303297", invoice_date="2026-09-19", invoice_date_raw="19.09.2026",
        order_number="405-9078484-6760355", reference_numbers=["TN-CJB1-1931441115-2627"],
        seller={"name": "RETAILEZ PRIVATE LIMITED", "address": None, "gstin": "33AALCR3173P1ZU",
                "pan": "AALCR3173P", "state_code": "33"},
        line_items=[
            _row(1, "Safari Omega laptop backpack", hsn_sac="4202", quantity="1", unit_price="600.84",
                 discount="0.00", net_amount="600.84", taxes=[_tax("CGST", "9", "54.08"), _tax("SGST", "9", "54.08")],
                 line_total="709.00"),
            _row(2, "Shipping Charges", unit_price="33.90", discount="-33.90", net_amount="0.00",
                 taxes=[_tax("CGST", "9", "0.00"), _tax("SGST", "9", "0.00")], line_total="0.00")],
        total_tax="108.16", grand_total="709.00", amount_in_words="Seven Hundred Nine only",
        payment={"transaction_id": TXN, "mode": "UPI", "paid_at": None, "amount_paid": "709.00"})


def amazon_fee():                  # IGST, no quantity, SAC from a footnote
    return _inv(
        invoice_number="MKT-353808321", invoice_date="2026-09-19", invoice_date_raw="19.09.2026",
        order_number="405-9078484-6760355", reference_numbers=["TN-CJB1-1044-2627"],
        seller={"name": "Amazon Seller Services Private Limited", "address": None,
                "gstin": "29AAICA3918J1ZE", "pan": "AAICA3918J", "state_code": "29"},
        line_items=[_row(1, "Marketplace Fees", hsn_sac="998319", unit_price="4.24", net_amount="4.24",
                         taxes=[_tax("IGST", "18", "0.76")], line_total="5.00")],
        total_tax="0.76", grand_total="5.00", amount_in_words="Five only",
        payment={"transaction_id": TXN, "mode": "UPI", "paid_at": None, "amount_paid": "5.00"})


def myntra_watch():                # tax-INCLUSIVE pricing: gross - discount = total
    return _inv(
        invoice_number="I1926JD000023630", invoice_date="2026-02-13", invoice_date_raw="13 Feb 2026",
        order_number="1322808-4772362-3393803", reference_numbers=["9852820345"],
        seller={"name": "JW BRANDS PRIVATE LIMITED - SJIT", "address": None, "gstin": "19AAECJ7669M1ZO",
                "pan": None, "state_code": None},
        line_items=[_row(1, "JOKER & WITCH Analogue Watch", hsn_sac="91021900", quantity="1",
                         gross_amount="3799.00", discount="3135.00", other_charges="0.00", net_amount="562.71",
                         taxes=[_tax("IGST", "18", "101.29")], line_total="664.00")],
        subtotal="562.71", total_tax="101.29", grand_total="664.00")


def myntra_fees():
    return _inv(
        source_pages=[2, 3], invoice_number="I2926MY227475971", invoice_date="2026-02-13",
        invoice_date_raw="13 Feb 2026", order_number="132280847723623393803",
        seller={"name": "MYNTRA DESIGNS PVT LTD", "address": None, "gstin": "29AAECM9636P1ZJ",
                "pan": None, "state_code": None},
        line_items=[
            _row(1, "Platform Fee", hsn_sac="999799", quantity="1.0", gross_amount="23.00", discount="0.00",
                 other_charges="0.00", net_amount="19.49", taxes=[_tax("IGST", "18", "3.51")], line_total="23.00"),
            _row(2, "Cash/Pay on Delivery Fee", hsn_sac="999799", quantity="1.0", gross_amount="10.00",
                 discount="0.00", other_charges="0.00", net_amount="8.47", taxes=[_tax("IGST", "18", "1.53")],
                 line_total="10.00")],
        subtotal="27.96", total_tax="5.04", grand_total="33.00")


def flipkart_bill():               # Bill of Supply, intra-state, 0% CGST/SGST
    return _inv(
        source_pages=[4, 5], document_type="Bill of Supply", invoice_number="I1926FI015395867",
        invoice_date="2026-02-19", invoice_date_raw="2026-02-19 16:27:21", order_number="132280847723623393803",
        place_of_supply="WEST BENGAL",
        seller={"name": "Flipkart India Private Limited", "address": None, "gstin": "19AABCF8078M1ZY",
                "pan": "AABCF8078M", "state_code": None},
        line_items=[_row(1, "GT charges", hsn_sac="996511", quantity="1.0", gross_amount="94.00",
                         net_amount="94.00", taxes=[_tax("SGST", "0", "0.00"), _tax("CGST", "0", "0.00")],
                         line_total="94.00")],
        subtotal="94.00", total_tax="0.00", grand_total="94.00")


DIGITAL = [retailez_backpack, amazon_fee, myntra_watch, myntra_fees, flipkart_bill]


def grounded(inv: dict, value=True) -> dict:
    """Return a copy with grounding as a digital PDF would give for a correct extraction."""
    inv = copy.deepcopy(inv)
    fields = ("invoice_number", "invoice_date", "seller_name", "seller_gstin", "buyer_name", "order_number",
              "transaction_id", "subtotal", "total_tax", "grand_total")
    present = {"invoice_number": inv["invoice_number"], "invoice_date": inv["invoice_date"],
               "seller_name": inv["seller"]["name"], "seller_gstin": inv["seller"]["gstin"],
               "buyer_name": inv["buyer"]["name"], "order_number": inv["order_number"],
               "transaction_id": inv["payment"]["transaction_id"], "subtotal": inv["subtotal"],
               "total_tax": inv["total_tax"], "grand_total": inv["grand_total"]}
    inv["grounding"] = {f: (value if present[f] else None) for f in fields}
    inv["grounding"]["line_items"] = [value] * len(inv["line_items"])
    return inv


def scanned(inv: dict, number=None) -> dict:
    """The same invoice read from an image: no grounding evidence, optionally a misread number."""
    inv = copy.deepcopy(inv)
    inv["grounding"] = {f: None for f in ("invoice_number", "invoice_date", "seller_name", "seller_gstin",
                                          "buyer_name", "order_number", "transaction_id", "subtotal",
                                          "total_tax", "grand_total")}
    inv["grounding"]["line_items"] = None
    if number:
        inv["invoice_number"] = number
    return inv


def pdf_for(invoices: list[dict], footer: str = "") -> bytes:
    """A text PDF (one page per invoice) that literally contains every value, so grounding is True."""
    doc = pymupdf.open()
    for inv in invoices:
        page = doc.new_page()
        lines = ["Tax Invoice", f"Invoice Number: {inv['invoice_number']}", f"Invoice Date: {inv['invoice_date_raw']}",
                 f"Order Number: {inv['order_number']}", f"Sold By: {inv['seller']['name']}",
                 f"GSTIN: {inv['seller']['gstin']}", f"Bill to: {inv['buyer']['name']}",
                 f"Payment Transaction ID: {inv['payment']['transaction_id'] or ''}"]
        for r in inv["line_items"]:
            lines.append(" ".join(str(x) for x in (r["description"], r["gross_amount"], r["discount"],
                                                   r["net_amount"], r["line_total"],
                                                   *[t["amount"] for t in r["taxes"]]) if x))
        lines += [f"Taxable Value {inv['subtotal'] or ''}", f"Total tax {inv['total_tax'] or ''}",
                  f"TOTAL {inv['grand_total']}", footer]
        for i, line in enumerate(lines):
            page.insert_text((40, 50 + 16 * i), line, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


# ---- Nykaa: a tax-INCLUSIVE table whose "NET AMT" is the row total (see the real-run findings) ----
def _nykaa_row(no, desc, hsn, qty, unit, disc, net, rate, amt, total):
    zero = {"type": "IGST", "rate": "0.00", "amount": "0.00"}
    return _row(no, desc, hsn_sac=hsn, quantity=qty, unit_price=unit, discount=disc, net_amount=net, line_total=total,
                taxes=[_tax("CGST", rate, amt), _tax("SGST", rate, amt), zero], confidence=0.9)


def nykaa_as_returned():
    """Exactly what Gemini returned for the real invoice: 'NET AMT' stored as the taxable value, and a
    line_total the model calculated itself (not printed anywhere), plus all-zero IGST entries."""
    rows = [
        _nykaa_row(1, "Free Vaseline Body Lotion", "33049930", "1", "0.00", "0.00", "0.00", "0.00", "0.00", "0.00"),
        _nykaa_row(2, "Maybelline Serum Satin Lipstick", "33041000", "1", "467.00", "52.18", "414.82", "9.00", "31.64", "478.10"),
        _nykaa_row(3, "Lakme Eyeconic Mascara", "34013019", "1", "359.00", "40.11", "318.89", "9.00", "24.32", "367.53"),
        _nykaa_row(4, "Bronson Eyelash Curler", "82142090", "1", "98.00", "10.95", "87.05", "9.00", "6.64", "100.33"),
        _nykaa_row(5, "FAE Beauty Kajal", "33042000", "1", "349.00", "38.99", "310.01", "9.00", "23.64", "357.29"),
        _nykaa_row(6, "Fae Beauty Lip Liner", "33049910", "1", "360.00", "40.22", "319.78", "9.00", "24.39", "368.56"),
        _nykaa_row(7, "Plush Shaving Gel", "34013012", "1", "179.00", "20.00", "159.00", "2.50", "3.79", "166.58"),
        _nykaa_row(8, "COD Charge", "999799", None, None, "0.00", "0.00", "0.00", "0.00", "0.00"),
        _nykaa_row(9, "Shipping & Platform Fees", "996819", None, None, "0.00", "0.00", "0.00", "0.00", "0.00"),
        _nykaa_row(10, "GiftWrap Charge", "998541", None, None, "0.00", "0.00", "0.00", "0.00", "0.00"),
    ]
    return _inv(
        source_pages=[1], document_type="Retail / Tax Invoice", invoice_number="CHN012066608",
        invoice_date="2026-09-28", invoice_date_raw="28/09/2026", order_number="NYK-385344518-6477643-1",
        reference_numbers=["CHN596876502"], place_of_supply="Tamil Nadu",
        seller={"name": "CHN-NYKAA E-RETAIL LIMITED", "address": None, "gstin": "33AAFCN5072P1Z2",
                "pan": None, "state_code": None},
        line_items=rows, total_tax="228.84", grand_total="1609.55")


def nykaa_correct():
    """What prompt v3 asks for: sale price as gross, NET AMT as the row total, nothing calculated."""
    inv = nykaa_as_returned()
    for r in inv["line_items"]:
        r["gross_amount"] = r["unit_price"] if r["unit_price"] not in (None, "0.00") else None
        r["unit_price"] = None
        r["line_total"], r["net_amount"] = r["net_amount"], None
        r["taxes"] = [t for t in r["taxes"] if t["type"] != "IGST"]
    return inv


def nykaa_pdf() -> bytes:
    """Text PDF with only the numbers the real invoice prints (so the model's calculated totals are absent)."""
    inv = nykaa_as_returned()
    doc = pymupdf.open()
    page = doc.new_page()
    lines = ["RETAIL / TAX INVOICE", f"Order# : {inv['order_number']}", f"INVOICE # : {inv['invoice_number']}",
             f"Date : {inv['invoice_date_raw']}", f"SOLD BY : {inv['seller']['name']} GSTIN # : {inv['seller']['gstin']}",
             f"Bill to {inv['buyer']['name']}"]
    for r in inv["line_items"]:
        t = r["taxes"]
        lines.append(f"{r['description']} HSN# : {r['hsn_sac']} {r['unit_price'] or ''} {r['quantity'] or ''} "
                     f"{r['discount']} {t[0]['rate']} {t[0]['amount']} {t[1]['rate']} {t[1]['amount']} 0.00 0.00 {r['net_amount']}")
    lines += ["DISCOUNT (SEPT15) : INR 202.45", "TAX AMOUNT : INR 228.84", "ROUND OFF : INR 0.00", "GRAND TOTAL : INR 1609.55"]
    for i, line in enumerate(lines):
        page.insert_text((20, 40 + 14 * i), line, fontsize=7)
    data = doc.tobytes()
    doc.close()
    return data
