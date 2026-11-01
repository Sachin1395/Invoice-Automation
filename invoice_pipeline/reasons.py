"""Plain-language wording for rejections and passed checks, shared by the log, the UI and the exports."""
from __future__ import annotations

import re
from typing import Optional

# reason code -> (what happened, what to do about it)
FILE_REASONS = {
    "file_too_large": ("File is too large", "Keep files under 20 MB, or split the document."),
    "unsupported_type": ("Unsupported file type", "Upload a PDF, JPEG or PNG."),
    "corrupt_file": ("The document could not be opened", "The file looks damaged. Export or scan it again."),
    "encrypted_pdf": ("The PDF is password protected", "Remove the password and upload it again."),
    "too_many_pages": ("Too many pages", "Split the document into smaller files."),
    "render_failed": ("The pages could not be rendered", "Try printing the document to PDF and uploading that."),
}

CONFIRMATIONS = {
    "grand_total:formula": "Subtotal plus tax matches the invoice total",
    "grand_total:rows": "Line items add up to the invoice total",
    "grand_total:words": "Amount in words matches the total",
    "subtotal:rows": "Line items add up to the subtotal",
    "total_tax:rows": "Tax on the line items adds up to the total tax",
    "seller_gstin:checksum": "Seller GSTIN check digit is valid",
    "buyer_gstin:checksum": "Buyer GSTIN check digit is valid",
    "invoice_number:shape": "Invoice number follows this seller's usual pattern",
    "payment:amount": "Payment amount matches the total",
    "no_duplicate": "No similar invoice found",
}


def file_reason(code: Optional[str]) -> tuple[str, str]:
    if code in FILE_REASONS:
        return FILE_REASONS[code]
    return ((code or "Rejected").replace("_", " ").capitalize(), "")


def file_reason_text(code: Optional[str], detail: Optional[str] = None) -> str:
    title, hint = file_reason(code)
    return " — ".join(x for x in (title, hint) if x)


def confirmation_text(code: str) -> str:
    return CONFIRMATIONS.get(code) or code.replace("_", " ").replace(":", " · ").capitalize()


_REF = re.compile(r"invoice #\d+ \(([^,()]*), [a-z_]+\)")


def plain_ids(message: str) -> str:
    """'invoice #3 (I1926JD0, accepted)' -> 'invoice I1926JD0': internal row ids mean nothing to a reader."""
    return _REF.sub(lambda m: f"invoice {m.group(1) or '(no number)'}", message)
