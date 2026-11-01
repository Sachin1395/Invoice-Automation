"""The validation node: runs every check on each extracted invoice, updates confidence and
stores one row per invoice. The route nodes that follow act on the decision."""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from .. import db, events
from ..config import Settings
from ..models import PipelineState
from ..profiles.base import Profile
from ..validation.bank import BankVerifier
from ..validation.engine import Ctx, validate_empty_file, validate_invoice

log = logging.getLogger("invoice_pipeline.validate")


def invoice_is_scan(inv: dict, text_layer) -> bool:
    """No usable text on any page this invoice sits on: its values were read from pixels only."""
    if text_layer is None:
        return True
    pages = [p for p in (inv.get("source_pages") or []) if 1 <= p <= len(text_layer)]
    return bool(pages) and all(not text_layer[p - 1] for p in pages)


def validate_node(state: PipelineState, settings: Settings, profile: Profile,
                  bank: Optional[BankVerifier] = None) -> dict:
    is_scan = state.text_layer is None   # nothing in the file itself to read the values from
    today = settings.today or date.today()
    out: list[dict] = []

    with db.session(settings.db_path) as conn:
        db.delete_invoices_for_extraction(conn, state.extraction_id)   # re-runs replace, never duplicate
        ctx = Ctx(settings=settings, rules=profile.rules, conn=conn, is_scan=is_scan, today=today, bank=bank)

        if not state.invoices:
            out.append(validate_empty_file(is_scan).model_dump(mode="json"))
            events.emit("VALIDATE", "No invoice was found in this file", "w", job_id=state.job_id)

        for idx, inv in enumerate(state.invoices):
            ctx.is_scan = invoice_is_scan(inv, state.text_layer)
            validation, keys, inv = validate_invoice(inv, idx, ctx)   # inv: repaired copy, if any
            payload = validation.model_dump(mode="json")
            payload["invoice_id"] = db.insert_invoice(
                conn, extraction_id=state.extraction_id, job_id=state.job_id, sha256=state.file_id,
                seq=idx, inv=inv, validation=payload, keys=keys)
            payload["repaired"] = any(f["code"] == "INCLUSIVE_NET_DETECTED" for f in payload["flags"])
            out.append(payload)
            nflag = sum(1 for f in payload["flags"] if f["severity"] != "minor")
            events.emit("VALIDATE", f"{inv.get('invoice_number') or 'No invoice number'} · "
                                    f"{len(payload['confirmations'])} checks passed · "
                                    f"{nflag} need{'s' if nflag == 1 else ''} attention · score {payload['score']:.2f}",
                        "w" if nflag else "ok", job_id=state.job_id)
            log.info("job=%s invoice %s (%s): %s score=%.2f flags=%s", state.job_id, idx,
                     inv.get("invoice_number"), payload["route"], payload["score"],
                     [f["code"] for f in payload["flags"]])
    return {"validations": out}
