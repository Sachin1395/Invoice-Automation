"""The LangGraph ingest node.

Pipeline: size check -> read + SHA-256 -> magic-byte type check -> dedup
-> integrity check -> render -> text layer -> normalize -> save pages.
Each step lives in invoice_pipeline.ingest.*; this module only composes them.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .. import db, events
from ..reasons import file_reason
from ..config import Settings
from ..ingest.hashing import sha256_hex
from ..ingest.normalize import normalize_image, save_jpeg
from ..ingest.render import extract_text_layer, load_image, render_pdf_pages
from ..ingest.validate import (
    PDF_MIME,
    check_image_integrity,
    check_pdf_integrity,
    check_size,
    detect_mime,
)
from ..models import IngestRejection, IngestState, IngestStatus, PageInfo

log = logging.getLogger("invoice_pipeline.ingest")


def ingest_node(state: IngestState, settings: Settings) -> dict:
    """Process ONE file. Returns state updates (a partial dict)."""
    path = Path(state.source_path)
    updates: dict = {"original_filename": path.name}
    jid = state.job_id
    sha = None

    try:
        size = check_size(path, settings)
        updates["size_bytes"] = size

        raw = path.read_bytes()
        sha = sha256_hex(raw)
        updates["file_id"] = sha

        mime = detect_mime(raw, settings)
        updates["detected_mime"] = mime

        # Early dedup: only *successful* earlier jobs block this file.
        with db.session(settings.db_path) as conn:
            earlier = db.find_done_job(conn, sha)
        if earlier is not None:
            log.info("job=%s duplicate of job=%s sha=%s", jid, earlier, sha[:12])
            events.emit("INGEST", f"Exact duplicate of a file already processed · fingerprint {sha[:8]}", "w", job_id=jid)
            updates.update(ingest_status=IngestStatus.DUPLICATE, duplicate_of=earlier)
            return updates

        is_pdf = mime == PDF_MIME
        if is_pdf:
            check_pdf_integrity(raw, settings)
            images = render_pdf_pages(raw, settings)
            updates["text_layer"] = extract_text_layer(raw, settings)
        else:
            check_image_integrity(raw)
            images = [load_image(raw)]

        pages = _save_pages(images, sha, settings)
        updates["pages"] = pages
        updates["page_count"] = len(pages)

        with db.session(settings.db_path) as conn:
            db.upsert_file(conn, sha, path.name, size, mime, len(pages))

        updates["ingest_status"] = IngestStatus.OK
        log.info("job=%s ok pages=%d mime=%s sha=%s", jid, len(pages), mime, sha[:12])
        kind = "PDF" if is_pdf else "image"
        events.emit("INGEST", f"Valid {kind} · {len(pages)} page{'s' if len(pages) != 1 else ''} · "
                              f"fingerprint {sha[:8]}", "ok", job_id=jid)
        return updates

    except IngestRejection as rej:
        log.warning("job=%s rejected reason=%s detail=%s", jid, rej.reason.value, rej.detail)
        events.emit("INGEST", f"File rejected: {file_reason(rej.reason.value)[0]}", "e", job_id=jid)
        updates.update(
            ingest_status=IngestStatus.REJECTED,
            rejection_reason=rej.reason,
            rejection_detail=rej.detail or None,
        )
        return updates


def _save_pages(images, sha: str, settings: Settings) -> list[PageInfo]:
    """Normalize each page and write it as JPEG under pages/<sha>/."""
    out_dir = settings.pages_dir / sha
    if out_dir.exists():  # leftovers from an earlier failed attempt
        shutil.rmtree(out_dir)
    pages: list[PageInfo] = []
    try:
        for i, img in enumerate(images, start=1):
            norm = normalize_image(img, settings.max_dimension)
            page_path = out_dir / f"page_{i:03d}.jpg"
            save_jpeg(norm, page_path, settings.jpeg_quality)
            pages.append(
                PageInfo(page_no=i, path=str(page_path), width=norm.width, height=norm.height)
            )
    except Exception as exc:
        shutil.rmtree(out_dir, ignore_errors=True)
        from ..models import RejectionReason

        raise IngestRejection(RejectionReason.RENDER_FAILED, f"normalize/save failed: {exc}")
    return pages
