"""The LangGraph extraction node: page images (+ text layer) -> structured invoices.

It extracts fields and model-reported confidence, and records grounding
evidence. It does not judge correctness; that is the validation node's job.
"""
from __future__ import annotations

import json
import logging

from .. import db, events
from ..config import Settings
from ..extract.grounding import compute_grounding
from ..extract.inputs import build_parts
from ..llm.chain import ModelChain
from ..llm.types import AllModelsUnavailable, ExtractionFailed
from ..models import ExtractionStatus, PipelineState
from ..profiles.base import Profile

log = logging.getLogger("invoice_pipeline.extract")


def extract_node(state: PipelineState, settings: Settings, chain: ModelChain, profile: Profile) -> dict:
    jid, sha = state.job_id, state.file_id
    updates: dict = {"extraction_profile": profile.name}

    # 1. Cache: same bytes + same profile + same prompt version = no API call.
    with db.session(settings.db_path) as conn:
        cached = db.find_cached_extraction(conn, sha, profile.name, profile.prompt_version)
    if cached is not None:
        log.info("job=%s extraction cache hit (extraction=%s)", jid, cached["extraction_id"])
        events.emit("EXTRACT", "Reused the earlier reading of this exact file (no model call needed)", "ok", job_id=jid)
        updates.update(
            invoices=json.loads(cached["parsed_json"])["invoices"],
            extraction_status=ExtractionStatus.OK,
            extraction_model=cached["model_used"],
            extraction_id=cached["extraction_id"],
            extraction_cached=True,
        )
        return updates

    # 2. Call the model chain.
    parts = build_parts(state)
    n = len(state.pages)
    events.emit("EXTRACT", f"Sending {n} page image{'s' if n != 1 else ''} to the model", job_id=jid)
    try:
        result = chain.extract(profile.load_prompt(), parts, profile.result_schema)
    except AllModelsUnavailable as exc:
        log.warning("job=%s deferred: %s", jid, exc)
        events.emit("EXTRACT", "The model service is unavailable right now; this file will be retried later", "e", job_id=jid)
        updates.update(
            extraction_status=ExtractionStatus.DEFERRED,
            extraction_error=str(exc),
            retry_after=exc.retry_after.isoformat(timespec="seconds") if exc.retry_after else None,
        )
        return updates
    except ExtractionFailed as exc:
        log.error("job=%s extraction failed: %s", jid, exc)
        events.emit("EXTRACT", "The model never returned usable data for this file", "e", job_id=jid)
        with db.session(settings.db_path) as conn:
            eid = db.save_extraction(
                conn, job_id=jid, sha256=sha, profile=profile.name,
                prompt_version=profile.prompt_version, status="failed",
                model_used=exc.model, raw_response=exc.raw_response, error=exc.error,
            )
        updates.update(
            extraction_status=ExtractionStatus.FAILED,
            extraction_error=str(exc),
            extraction_model=exc.model,
            extraction_id=eid,
        )
        return updates
    # FatalLLMError is deliberately not caught: it aborts the whole run.

    # 3. Normalize to plain dicts and attach grounding evidence.
    invoices = [inv.model_dump(mode="json") for inv in result.parsed.invoices]
    for inv in invoices:
        inv["grounding"] = compute_grounding(inv, state.text_layer)
    if not invoices:
        log.warning("job=%s model found no invoices in this file", jid)

    with db.session(settings.db_path) as conn:
        eid = db.save_extraction(
            conn, job_id=jid, sha256=sha, profile=profile.name,
            prompt_version=profile.prompt_version, status="ok", model_used=result.model,
            raw_response=result.raw_text, parsed_json=json.dumps({"invoices": invoices}),
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            latency_ms=result.latency_ms,
        )
    log.info("job=%s extracted %d invoice(s) with %s in %d ms (tokens in=%s out=%s)",
             jid, len(invoices), result.model, result.latency_ms,
             result.input_tokens, result.output_tokens)
    events.emit("EXTRACT", f"Structured JSON received · {len(invoices)} invoice{'s' if len(invoices) != 1 else ''} · "
                           f"{result.model} · {result.latency_ms / 1000:.1f}s", "ok", job_id=jid)
    updates.update(
        invoices=invoices,
        extraction_status=ExtractionStatus.OK,
        extraction_model=result.model,
        extraction_id=eid,
    )
    return updates
