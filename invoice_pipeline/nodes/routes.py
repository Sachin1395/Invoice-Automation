"""The three route nodes. Each acts only on the invoices that were routed to it."""
from __future__ import annotations

import logging

from .. import db, events
from ..config import Settings
from ..models import PipelineState
from ..reasons import plain_ids

log = logging.getLogger("invoice_pipeline.routes")


def _mine(state: PipelineState, route: str) -> list[dict]:
    return [v for v in state.validations if v["route"] == route]


def _summary(state: PipelineState, v: dict) -> str:
    inv = state.invoices[v["index"]] if 0 <= v["index"] < len(state.invoices) else {}
    return " · ".join(str(x) for x in (
        inv.get("invoice_number") or "no number", (inv.get("seller") or {}).get("name") or "unknown seller",
        f"{inv.get('grand_total')} {inv.get('currency') or ''}".strip()) if x)


def autopass_node(state: PipelineState, settings: Settings) -> dict:
    ids = [v["invoice_id"] for v in _mine(state, "autopass") if v.get("invoice_id") is not None]
    with db.session(settings.db_path) as conn:
        db.set_invoice_status(conn, ids, "accepted")
    for v in _mine(state, "autopass"):
        events.emit("ROUTE", f"Autopass · {_summary(state, v)}", "ok", job_id=state.job_id)
    log.info("job=%s autopassed invoices %s", state.job_id, ids)
    return {"autopassed_ids": ids}


def human_review_node(state: PipelineState, settings: Settings) -> dict:
    ids: list[int] = []
    with db.session(settings.db_path) as conn:
        for v in _mine(state, "human_review"):
            if v.get("invoice_id") is not None:
                db.set_invoice_status(conn, [v["invoice_id"]], "review")
                ids.append(v["invoice_id"])
            db.add_review_item(conn, invoice_id=v.get("invoice_id"), extraction_id=state.extraction_id,
                               job_id=state.job_id, sha256=state.file_id,
                               reasons=v.get("reasons") or [], summary=_summary(state, v))
            why = plain_ids((v.get("reasons") or ["needs a person to look at it"])[0])
            events.emit("ROUTE", f"Sent to human review · {_summary(state, v)} · {why}", "w", job_id=state.job_id)
    log.info("job=%s sent to human review: %s", state.job_id, ids or "file-level item")
    return {"review_ids": ids}


def hard_reject_node(state: PipelineState, settings: Settings) -> dict:
    ids = [v["invoice_id"] for v in _mine(state, "hard_reject") if v.get("invoice_id") is not None]
    with db.session(settings.db_path) as conn:
        db.set_invoice_status(conn, ids, "rejected")
    for v in _mine(state, "hard_reject"):
        why = next((f["message"] for f in v["flags"] if f["severity"] == "blocker"), "failed a blocking check")
        events.emit("ROUTE", f"Rejected · {_summary(state, v)} · {plain_ids(why)}", "e", job_id=state.job_id)
    log.warning("job=%s hard-rejected invoices %s", state.job_id, ids)
    return {"rejected_ids": ids}
