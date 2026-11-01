"""Confidence update: turns the model's own scores plus independent evidence into final scores.

Order matters: caps first, then evidence that can lift a score, then failures that have the last word.
"""
from __future__ import annotations

from typing import Optional

from ..config import Settings
from .arithmetic import total_confirmed
from .types import Flag

CONF_FIELDS = ("invoice_number", "invoice_date", "seller_name", "seller_gstin", "buyer_name",
               "subtotal", "total_tax", "grand_total", "currency")
SCAN_CAPPED = ("invoice_number", "seller_gstin")          # identifiers an image can misread
GROUNDING_RAISES = ("invoice_number", "seller_gstin", "seller_name", "buyer_name", "invoice_date")
FAILED_CEILING = 0.3


def _value(inv: dict, field: str):
    return {
        "invoice_number": inv.get("invoice_number"),
        "invoice_date": inv.get("invoice_date"),
        "seller_name": (inv.get("seller") or {}).get("name"),
        "seller_gstin": (inv.get("seller") or {}).get("gstin"),
        "buyer_name": (inv.get("buyer") or {}).get("name"),
        "subtotal": inv.get("subtotal"),
        "total_tax": inv.get("total_tax"),
        "grand_total": inv.get("grand_total"),
        "currency": inv.get("currency"),
    }[field]


def update_confidence(
    inv: dict, flags: list[Flag], confirmations: list[str], is_scan: bool,
    settings: Settings, required_fields: list[str],
) -> tuple[dict, float]:
    model = inv.get("confidence") or {}
    grounding = inv.get("grounding") or {}
    conf: dict[str, Optional[float]] = {}

    for f in CONF_FIELDS:                                   # 0. start from what the model said
        if _value(inv, f) in (None, ""):
            conf[f] = None
        else:
            s = model.get(f)
            conf[f] = 0.5 if s is None else float(s)

    if is_scan:                                             # 1. nothing independent reads an image
        for f in SCAN_CAPPED:
            if conf[f] is not None:
                conf[f] = min(conf[f], settings.scan_id_cap)

    for f, g in grounding.items():                          # 2. does the value exist in the file's own text?
        if f in conf and conf[f] is not None:
            if g is False:
                conf[f] = min(conf[f], FAILED_CEILING)
            elif g is True and f in GROUNDING_RAISES:
                conf[f] = max(conf[f], 0.9)

    def lift(field: str, to: float) -> None:                # 3. independent confirmation
        if conf.get(field) is not None:
            conf[field] = max(conf[field], to)

    if "seller_gstin:checksum" in confirmations:
        lift("seller_gstin", 0.95)
    if "invoice_number:shape" in confirmations:
        lift("invoice_number", 0.9)
    if total_confirmed(confirmations):
        lift("grand_total", 0.95)
    if "grand_total:words" in confirmations:
        lift("grand_total", 0.98)
    if "total_tax:rows" in confirmations:
        lift("total_tax", 0.95)
    if "subtotal:rows" in confirmations:
        lift("subtotal", 0.95)

    for fl in flags:                                        # 4. failures have the last word
        for f in fl.fields:
            if f not in conf:
                continue
            if fl.code == "MISSING_REQUIRED":
                conf[f] = 0.0
            elif conf[f] is not None:
                conf[f] = min(conf[f], FAILED_CEILING)

    score = min((conf.get(f) or 0.0) for f in required_fields) if required_fields else 0.0
    return {k: (round(v, 3) if v is not None else None) for k, v in conf.items()}, round(score, 3)
