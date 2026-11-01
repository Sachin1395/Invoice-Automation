"""A scriptable fake LLM client plus helpers that build realistic API errors and sample data."""
from __future__ import annotations

import json

import pymupdf
from google.genai import errors

from invoice_pipeline.llm.types import LLMResponse

# --- errors exactly as the SDK raises them -----------------------------------
def _body(code, status, message):
    return {"error": {"code": code, "message": message, "status": status}}


def daily_quota_error():
    return errors.ClientError(429, _body(
        429, "RESOURCE_EXHAUSTED",
        "Quota exceeded for metric generate_content_free_tier_requests; quotaId: "
        "GenerateRequestsPerDayPerProjectPerModel-FreeTier"))


def rate_limit_error(delay=None):
    msg = "Quota exceeded for metric generate_content_free_tier_requests, limit: 10 per minute."
    if delay is not None:
        msg += f" Please retry in {delay}s."
    return errors.ClientError(429, _body(429, "RESOURCE_EXHAUSTED", msg))


def not_found_error():
    return errors.ClientError(404, _body(404, "NOT_FOUND", "models/x is not found for API version v1beta"))


def server_error(code=503):
    return errors.ServerError(code, _body(code, "UNAVAILABLE", "The model is overloaded."))


def auth_error():
    return errors.ClientError(403, _body(403, "PERMISSION_DENIED", "API key not valid"))


def bad_request_error():
    return errors.ClientError(400, _body(400, "INVALID_ARGUMENT", "Request contains an invalid argument."))


# --- the fake client -----------------------------------------------------------
class FakeLLM:
    """script: {model or '*': [action, ...]}. An action is a JSON string or an Exception.
    The last action of a list repeats forever."""

    def __init__(self, script=None, models=("model-a", "model-b")):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.models = set(models)
        self.list_error = None
        self.calls = []  # (model, parts)

    def list_models(self):
        if self.list_error:
            raise self.list_error
        return set(self.models)

    def generate(self, *, model, system_prompt, parts, schema, temperature):
        self.calls.append((model, parts))
        actions = self.script.get(model) or self.script.get("*")
        if actions is None:
            raise AssertionError(f"no script for model {model!r}")
        action = actions.pop(0) if len(actions) > 1 else actions[0]
        if isinstance(action, Exception):
            raise action
        return LLMResponse(text=action, input_tokens=1000, output_tokens=400, finish_reason="STOP")

    def models_called(self):
        return [m for m, _ in self.calls]


# --- sample data: two invoices in one file, modelled on a real Amazon.in GST PDF ---
def _tax(rate, amount):
    return [{"type": "IGST", "rate": rate, "amount": amount}]


SAMPLE_RESULT = {
    "invoices": [
        {
            "source_pages": [1],
            "document_type": "Tax Invoice",
            "invoice_number": "MKT-318400606",
            "invoice_date": "03.09.2026",           # day-first on purpose: schema must normalize it
            "invoice_date_raw": "03.09.2026",
            "order_number": "405-2216621-8321129",
            "reference_numbers": ["TG-1044-2627"],
            "place_of_supply": "TAMIL NADU",
            "reverse_charge": False,
            "currency": "inr",
            "seller": {"name": "Amazon Seller Services Private Limited",
                       "gstin": "29AAICA3918J1ZE", "pan": "AAICA3918J", "state_code": "29"},
            "buyer": {"name": "Test Buyer", "state_code": "33"},
            "line_items": [{
                "line_no": 1, "description": "Marketplace Fees", "quantity": None,
                "unit_price": "₹4.24", "net_amount": "4.24", "taxes": _tax("18%", "0.76"),
                "line_total": "5.00", "confidence": 0.95}],
            "subtotal": None, "total_tax": "0.76", "grand_total": "5.00",
            "amount_in_words": "Five only",
            "payment": {"transaction_id": "i93r4bcJb4QjgDpPCvkwVJqgLFNlrCBnWU5", "mode": "UPI",
                        "amount_paid": "5.00"},
            "confidence": {"invoice_number": 0.99, "invoice_date": 0.99, "seller_name": 0.98,
                           "seller_gstin": 0.97, "buyer_name": 0.9, "subtotal": 0,
                           "total_tax": 0.98, "grand_total": 0.99, "currency": 0.95},
        },
        {
            "source_pages": [2],
            "document_type": "Tax Invoice",
            "invoice_number": "IN-22242",
            "invoice_date": "2026-09-03",
            "invoice_date_raw": "03.09.2026",
            "order_number": "405-2216621-8321129",
            "reference_numbers": ["TG-479515333-2627"],
            "place_of_supply": "TAMIL NADU",
            "reverse_charge": False,
            "currency": "INR",
            "seller": {"name": "SRI SOUMYA MEDICALS", "gstin": "36AFFFS4938H1ZN",
                       "pan": "AFFFS4938H", "state_code": "36"},
            "buyer": {"name": "Test Buyer", "state_code": "33"},
            "line_items": [
                {"line_no": 1, "description": "Azac Acne Soap - 75 gm, Pack Of 3", "hsn_sac": "34011190",
                 "quantity": "1", "unit_price": "338.14", "net_amount": "338.14",
                 "taxes": _tax("18", "60.86"), "line_total": "399.00", "confidence": 0.95},
                {"line_no": 2, "description": "Shipping Charges", "quantity": None,
                 "unit_price": "58.47", "net_amount": "58.47", "taxes": _tax("18", "10.53"),
                 "line_total": "69.00", "confidence": 0.9},
            ],
            "subtotal": None, "total_tax": "71.39", "grand_total": "₹468.00",
            "amount_in_words": "Four Hundred Sixty-eight only",
            "payment": {"transaction_id": "i93r4bcJb4QjgDpPCvkwVJqgLFNlrCBnWU5", "mode": "UPI",
                        "amount_paid": "468.00"},
            "confidence": {"invoice_number": 0.99, "invoice_date": 0.99, "seller_name": 0.98,
                           "seller_gstin": 0.96, "buyer_name": 0.9, "subtotal": 0,
                           "total_tax": 0.98, "grand_total": 0.99, "currency": 0.95},
        },
    ]
}


def sample_json() -> str:
    return json.dumps(SAMPLE_RESULT)


def make_sample_invoice_pdf() -> bytes:
    """Two-page text PDF carrying the same values as SAMPLE_RESULT (digital text layer)."""
    pages = [
        ["Tax Invoice/Bill of Supply/Cash Memo",
         "Sold By: Amazon Seller Services Private Limited",
         "GST Registration No: 29AAICA3918J1ZE",
         "Billing Address: Test Buyer",
         "Order Number: 405-2216621-8321129",
         "Invoice Number : MKT-318400606   Invoice Date : 03.09.2026",
         "1  Marketplace Fees  4.24  4.24  18%  IGST  0.76  5.00",
         "TOTAL: 0.76  5.00",
         "Payment Transaction ID: i93r4bcJb4QjgDpPCvkwVJqgLFNlrCBnWU5  Invoice Value: 5.00  UPI"],
        ["Tax Invoice/Bill of Supply/Cash Memo",
         "Sold By: SRI SOUMYA MEDICALS",
         "GST Registration No: 36AFFFS4938H1ZN",
         "Billing Address: Test Buyer",
         "Order Number: 405-2216621-8321129",
         "Invoice Number : IN-22242   Invoice Date : 03.09.2026",
         "1  Azac Acne Soap  338.14  1  338.14  18%  IGST  60.86  399.00",
         "Shipping Charges  58.47  58.47  18%  IGST  10.53  69.00",
         "TOTAL: 71.39  468.00",
         "Payment Transaction ID: i93r4bcJb4QjgDpPCvkwVJqgLFNlrCBnWU5  Invoice Value: 468.00  UPI"],
    ]
    doc = pymupdf.open()
    for lines in pages:
        page = doc.new_page()
        for i, line in enumerate(lines):
            page.insert_text((50, 60 + 18 * i), line, fontsize=10)
    data = doc.tobytes()
    doc.close()
    return data


# An invoice with CGST+SGST on one row and a negative discount (modelled on a real Retailez/Amazon.in invoice)
BACKPACK = {
    "source_pages": [1], "document_type": "Tax Invoice", "invoice_number": "CJB1-1303297",
    "invoice_date": "2026-09-19", "invoice_date_raw": "19.09.2026", "order_number": "405-9078484-6760355",
    "currency": "INR", "place_of_supply": "TAMIL NADU", "reverse_charge": False,
    "seller": {"name": "RETAILEZ PRIVATE LIMITED", "gstin": "33AALCR3173P1ZU", "pan": "AALCR3173P", "state_code": "33"},
    "buyer": {"name": "Test Buyer", "state_code": "33"},
    "line_items": [
        {"line_no": 1, "description": "Laptop backpack 30 Litre", "hsn_sac": "4202", "quantity": "1",
         "unit_price": "600.84", "discount": "0.00", "net_amount": "600.84",
         "taxes": [{"type": "CGST", "rate": "9", "amount": "54.08"}, {"type": "SGST", "rate": "9", "amount": "54.08"}],
         "line_total": "709.00", "confidence": 0.95},
        {"line_no": 2, "description": "Shipping Charges", "quantity": None,
         "unit_price": "33.90", "discount": "-₹33.90", "net_amount": "0.00",
         "taxes": [{"type": "CGST", "rate": "9", "amount": "0.00"}, {"type": "SGST", "rate": "9", "amount": "0.00"}],
         "line_total": "0.00", "confidence": 0.9},
    ],
    "subtotal": None, "total_tax": "108.16", "grand_total": "709.00",
    "payment": {"transaction_id": "i93r4es3ijW1IDli8eSVkuq3Dynmqo3pVSz", "mode": "UPI", "amount_paid": "709.00"},
}

BACKPACK_TEXT = (
    "Order Number: 405-9078484-6760355  Invoice Number : CJB1-1303297  Invoice Date : 19.09.2026\n"
    "Sold By: RETAILEZ PRIVATE LIMITED  GST Registration No: 33AALCR3173P1ZU  Billing Address: Test Buyer\n"
    "1 Laptop backpack 600.84 0.00 1 600.84 9% CGST 54.08 709.00  9% SGST 54.08\n"
    "Shipping Charges 33.90 -33.90 0.00 9% CGST 0.00 0.00  9% SGST 0.00\n"
    "TOTAL: 108.16 709.00\n"
    "Payment Transaction ID: i93r4es3ijW1IDli8eSVkuq3Dynmqo3pVSz Invoice Value: 709.00 UPI"
)
