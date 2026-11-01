"""Thin sequential runner: one file at a time through the graph.

The full queue (continuous watcher, scheduled retries) comes after the whole
pipeline exists; the jobs table already carries last_error / next_attempt_at.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from . import db, events
from .config import Settings, get_api_key, get_settings
from .graph import build_graph, run_graph
from .llm.chain import ModelChain
from .llm.client import GeminiClient, LLMClient
from .llm.types import FatalLLMError
from .models import ExtractionStatus, IngestStatus, JobStatus
from .profiles.base import load_profile
from .reasons import FILE_REASONS
from .sources.base import FileSource
from .validation.bank import BankVerifier
from .sources.folder_source import FolderSource

log = logging.getLogger("invoice_pipeline.runner")


@dataclass
class JobResult:
    job_id: int
    filename: str
    status: JobStatus
    detail: Optional[str] = None


def _plain(detail: Optional[str]) -> Optional[str]:
    """Run-log wording: no '(s)', no internal reason codes."""
    if not detail:
        return detail
    if detail in FILE_REASONS:
        return FILE_REASONS[detail][0]
    return re.sub(r"(\d+) invoice\(s\)", lambda m: f"{m.group(1)} invoice{'' if m.group(1) == '1' else 's'}", detail)


def _size(n: int) -> str:
    return f"{n / 1048576:.2f} MB" if n >= 1048576 else f"{max(n / 1024, 0.1):.1f} KB"


def _record(results: list, r: JobResult) -> None:
    results.append(r)
    detail = _plain(r.detail)
    level, text = {
        JobStatus.DONE: ("ok", detail or "Done"),
        JobStatus.DUPLICATE: ("w", detail or "Duplicate"),
        JobStatus.REJECTED: ("e", detail or "Rejected"),
        JobStatus.FAILED: ("e", "Could not be processed"),
        JobStatus.PENDING: ("w", "Postponed; the file stays queued for the next run"),
    }.get(r.status, ("", r.status.value))
    events.emit("RUN", f"Finished · {text}", level, job_id=r.job_id)


def _build_gemini_client(settings: Settings) -> LLMClient:
    key = get_api_key()
    if not key:
        raise FatalLLMError(
            "GEMINI_API_KEY is not set. Create a .env file in the project root with "
            "GEMINI_API_KEY=your-key (get one in Google AI Studio), or set extraction_enabled=False."
        )
    return GeminiClient(key, timeout_s=settings.llm_timeout_s)


def _claim_job(settings: Settings, path) -> int:
    """Resume a deferred job for this file if there is one, else create a new job."""
    with db.session(settings.db_path) as conn:
        job_id = db.find_pending_job(conn, str(path)) or db.create_job(conn, str(path), path.name)
        db.start_job(conn, job_id)
    return job_id


def run_once(
    settings: Optional[Settings] = None,
    source: Optional[FileSource] = None,
    llm: Optional[LLMClient] = None,
    bank: Optional[BankVerifier] = None,
) -> list[JobResult]:
    settings = settings or get_settings()
    settings.ensure_dirs()
    db.init_db(settings.db_path)
    source = source or FolderSource(settings)

    files = source.discover()
    if not files:
        return []

    events.set_file(None)
    events.emit("RUN", f"Run started · {len(files)} file{'s' if len(files) != 1 else ''}")
    chain = profile = None
    if settings.extraction_enabled:
        chain = ModelChain(llm or _build_gemini_client(settings), settings)
        chain.probe()  # drops unavailable models; raises if none are usable
        events.emit("RUN", f"Models ready · {', '.join(chain.live_models())}", "ok")
        profile = load_profile(settings.extraction_profile)
        log.info("model chain for this run: %s", chain.live_models())
    graph = build_graph(settings, chain, profile, bank)

    results: list[JobResult] = []
    consecutive_deferred = 0
    needs_final_export = False   # a file that ended before the export node (rejected, duplicate, failed)
    for n, path in enumerate(files, start=1):  # strictly one by one, oldest first
        events.set_file(path.name)
        try:
            size = _size(path.stat().st_size)
        except OSError:
            size = "unknown size"
        events.emit("INGEST", f"File {n}/{len(files)} · {path.name} · {size}")
        job_id = _claim_job(settings, path)

        try:
            state = run_graph(graph, job_id, str(path))
        except FatalLLMError as exc:  # auth/config: nothing is wrong with the file
            with db.session(settings.db_path) as conn:
                db.defer_job(conn, job_id, error=str(exc))
            raise
        except Exception as exc:  # unexpected: not a deterministic rejection
            needs_final_export = True
            log.exception("job=%s failed unexpectedly", job_id)
            with db.session(settings.db_path) as conn:
                db.finish_job(conn, job_id, JobStatus.FAILED, error_message=repr(exc))
            source.mark_rejected(path)
            _record(results, JobResult(job_id, path.name, JobStatus.FAILED, repr(exc)))
            consecutive_deferred = 0
            continue

        if not state.exported_files:
            needs_final_export = True
        # --- ingest outcomes that end the job -------------------------------
        if state.ingest_status == IngestStatus.DUPLICATE:
            _finish(settings, job_id, JobStatus.DUPLICATE, state)
            source.mark_processed(path)
            _record(results, JobResult(job_id, path.name, JobStatus.DUPLICATE,
                                     f"duplicate of job {state.duplicate_of}"))
            consecutive_deferred = 0
            continue
        if state.ingest_status != IngestStatus.OK:
            _finish(settings, job_id, JobStatus.REJECTED, state)
            source.mark_rejected(path)
            detail = state.rejection_reason.value if state.rejection_reason else "rejected"
            _record(results, JobResult(job_id, path.name, JobStatus.REJECTED, detail))
            consecutive_deferred = 0
            continue

        # --- extraction outcomes --------------------------------------------
        ex = state.extraction_status if settings.extraction_enabled else ExtractionStatus.OK
        if ex == ExtractionStatus.OK:
            status, detail = _outcome(settings, state)
            if status == JobStatus.DUPLICATE:
                original = _original_job(settings, state)
                _finish(settings, job_id, status, state, duplicate_of=original)
            else:
                _finish(settings, job_id, status, state)
            if status == JobStatus.REJECTED:
                source.mark_rejected(path)
            else:
                source.mark_processed(path)
            _record(results, JobResult(job_id, path.name, status, detail))
            consecutive_deferred = 0
        elif ex == ExtractionStatus.DEFERRED:
            # The file is fine; the API wasn't. Leave it in the inbox for the next run.
            with db.session(settings.db_path) as conn:
                db.defer_job(conn, job_id, error=state.extraction_error or "deferred",
                             next_attempt_at=state.retry_after, sha256=state.file_id)
            _record(results, JobResult(job_id, path.name, JobStatus.PENDING,
                                     f"deferred: {state.extraction_error}"))
            consecutive_deferred += 1
            if not chain.live_models():
                log.error("no models left (quota or availability); stopping the run. "
                          "Remaining files stay in the inbox. Retry after %s", state.retry_after)
                break
            if consecutive_deferred >= settings.breaker_threshold:
                log.error("%d files in a row were deferred; stopping the run", consecutive_deferred)
                break
        else:  # FAILED
            with db.session(settings.db_path) as conn:
                db.finish_job(conn, job_id, JobStatus.FAILED, sha256=state.file_id,
                              error_message=state.extraction_error)
            source.mark_rejected(path)
            _record(results, JobResult(job_id, path.name, JobStatus.FAILED, state.extraction_error))
            consecutive_deferred = 0
        log.info("job=%s %s -> %s", job_id, path.name, results[-1].status.value)

    events.set_file(None)
    # Job statuses are written after each graph run finishes, so files that never reached the export
    # node only show up in the output (Rejected sheet, file counts) after one more refresh.
    if settings.export_enabled and needs_final_export and results:
        try:
            from .export import export_all
            export_all(settings)
        except Exception:
            log.exception("final export failed; run 'python -m invoice_pipeline.export' to retry")
    return results


def _finish(settings: Settings, job_id: int, status: JobStatus, state, duplicate_of=None) -> None:
    with db.session(settings.db_path) as conn:
        db.finish_job(
            conn, job_id, status,
            sha256=state.file_id,
            rejection_reason=state.rejection_reason.value if state.rejection_reason else None,
            rejection_detail=state.rejection_detail,
            duplicate_of=duplicate_of if duplicate_of is not None else state.duplicate_of,
        )


def _outcome(settings: Settings, state) -> tuple[JobStatus, Optional[str]]:
    """Job status and a one-line summary once extraction (and validation) have run."""
    n = len(state.invoices)
    cached = " (cached)" if state.extraction_cached else ""
    if not settings.extraction_enabled:
        return JobStatus.DONE, None
    if not settings.validation_enabled or not state.validations:
        return JobStatus.DONE, f"{n} invoice(s){cached}"

    routes = [v["route"] for v in state.validations]
    counts = {r: routes.count(r) for r in ("autopass", "human_review", "hard_reject") if r in routes}
    summary = ", ".join(f"{c} {r.replace('_', ' ')}" for r, c in counts.items())
    if all(r == "hard_reject" for r in routes):
        codes = {f["code"] for v in state.validations for f in v["flags"]}
        if any(c.startswith("DUPLICATE_EXACT") for c in codes):
            return JobStatus.DUPLICATE, f"duplicate of an invoice already stored ({summary})"
        return JobStatus.REJECTED, f"{summary}: " + "; ".join(
            f["message"] for v in state.validations for f in v["flags"] if f["severity"] == "blocker")
    if n == 0:
        return JobStatus.DONE, f"no invoice found ({summary})"
    return JobStatus.DONE, f"{n} invoice(s){cached}: {summary}"


def _original_job(settings: Settings, state) -> Optional[int]:
    """The job that first stored the invoice this file duplicates."""
    related = next((f["related_invoice_id"] for v in state.validations for f in v["flags"]
                    if f["code"] == "DUPLICATE_EXACT" and f.get("related_invoice_id")), None)
    if related is None:
        return None
    with db.session(settings.db_path) as conn:
        return db.invoice_job_id(conn, related)
