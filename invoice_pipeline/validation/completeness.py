"""Completeness checks, driven by the profile's list of required fields."""
from __future__ import annotations

from .types import Flag, Severity


def get_path(inv: dict, path: str):
    cur = inv
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def check_completeness(inv: dict, required: dict[str, str]) -> list[Flag]:
    """required: {path: confidence-field-name}, e.g. {"seller.name": "seller_name"}."""
    flags: list[Flag] = []
    for path, conf_field in required.items():
        value = get_path(inv, path)
        if value is None or (isinstance(value, str) and not value.strip()):
            flags.append(Flag(code="MISSING_REQUIRED", severity=Severity.MAJOR,
                              message=f"Required field is missing: {path}", fields=[conf_field]))
    rows = inv.get("line_items") or []
    if not rows:
        flags.append(Flag(code="NO_LINE_ITEMS", severity=Severity.MAJOR,
                          message="No line items were extracted"))
    for i, r in enumerate(rows, start=1):
        if not (r.get("description") or "").strip():
            flags.append(Flag(code="ROW_NO_DESCRIPTION", severity=Severity.MINOR,
                              message=f"Row {i} has no description"))
    if not get_path(inv, "buyer.name"):
        flags.append(Flag(code="MISSING_BUYER", severity=Severity.MINOR, message="Buyer name is missing"))
    return flags
