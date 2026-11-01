"""FastAPI service behind the web page.

    POST   /api/sessions                      upload up to 5 files, start processing   -> {id}
    GET    /api/sessions/{id}?after=N         status + log lines after line N
    GET    /api/sessions/{id}/result          files, invoices, validation (see results.py)
    POST   /api/sessions/{id}/review/{rid}    {"action": "approve" | "reject", "note"?}  -> result
    GET    /api/sessions/{id}/download/{fmt}  csv | lines | xlsx | json
    DELETE /api/sessions/{id}                 delete the session's files
    POST   /api/track                         {visitor, src}: anonymous 'opened' count (see analytics.py)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from .. import db, events
from ..config import PROJECT_ROOT, Settings, get_settings
from ..export import export_all
from ..export.exporter import FILES
from ..llm.client import LLMClient
from .analytics import Tracker, clean
from .limits import DemoLimits
from .results import build_result
from .sessions import MAX_FILES, SessionManager

log = logging.getLogger("invoice_pipeline.web")
STATIC = Path(__file__).parent / "static"
DOWNLOADS = {"csv": ("invoices_csv", "text/csv"), "lines": ("lines_csv", "text/csv"),
             "xlsx": ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
             "json": ("json", "application/json")}


class Hit(BaseModel):
    visitor: str = ""
    src: str = ""


class ReviewBody(BaseModel):
    action: str
    note: Optional[str] = None


def create_app(base: Optional[Settings] = None, llm: Optional[LLMClient] = None,
               sessions_root: Optional[Path] = None, synchronous: bool = False, ttl_s: float = 2 * 3600,
               limits: Optional[DemoLimits] = None, tracker: Optional[Tracker] = None) -> FastAPI:
    base = base or get_settings()
    limits = limits if limits is not None else DemoLimits.from_env()
    tracker = tracker if tracker is not None else Tracker.from_env()
    mgr = SessionManager(base, sessions_root or PROJECT_ROOT / "sessions", llm=llm, ttl_s=ttl_s,
                         synchronous=synchronous, tracker=tracker)
    app = FastAPI(title="Invoice Processing Agent", docs_url=None, redoc_url=None)
    app.state.sessions = mgr

    def need(sid: str):
        s = mgr.get(sid)
        if s is None:
            raise HTTPException(404, "This session has ended. Upload your files again.")
        return s

    @app.get("/", response_class=HTMLResponse)
    def index():
        return HTMLResponse((STATIC / "index.html").read_text(encoding="utf-8"))

    @app.get("/favicon.ico", include_in_schema=False)
    @app.get("/favicon.svg", include_in_schema=False)
    def favicon():
        return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml",
                            headers={"Cache-Control": "no-cache"})

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/track", status_code=204)
    def track(hit: Hit):
        tracker.track("opened", hit.visitor, hit.src)

    @app.post("/api/sessions")
    async def create_session(request: Request, files: list[UploadFile] = File(...),
                             visitor: str = Form(""), src: str = Form("")):
        if not files:
            raise HTTPException(400, "Choose at least one file.")
        if len(files) > MAX_FILES:
            raise HTTPException(400, f"You can upload up to {MAX_FILES} files at a time.")
        limit, uploads = base.max_file_bytes, []
        for f in files:
            data = await f.read(limit + 1)
            if len(data) > limit:
                raise HTTPException(413, f"{f.filename}: larger than {base.max_file_mb} MB.")
            uploads.append((f.filename or "file", data))
        # behind Render's proxy the visitor is the first X-Forwarded-For entry (spoofable, so the daily cap is the real guard)
        fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
        refused = limits.take(fwd or (request.client.host if request.client else "?"), len(uploads))
        if refused:
            raise HTTPException(429, refused)
        visitor, src = clean(visitor), clean(src)
        tracker.track("started", visitor, src, "")
        sess = mgr.create(uploads, visitor, src)
        return {"id": sess.id, "total": sess.total}

    @app.get("/api/sessions/{sid}")
    def status(sid: str, after: int = 0):
        s = need(sid)
        new = s.events_after(max(after, 0))
        return {"status": s.status, "error": s.error, "total": s.total,
                "events": new, "next": max(after, 0) + len(new)}

    @app.get("/api/sessions/{sid}/result")
    def result(sid: str):
        return build_result(need(sid).settings)

    @app.post("/api/sessions/{sid}/review/{review_id}")
    def review(sid: str, review_id: int, body: ReviewBody):
        s = need(sid)
        if s.status in ("queued", "running"):
            raise HTTPException(409, "Wait for processing to finish first.")
        if body.action not in ("approve", "reject"):
            raise HTTPException(400, "action must be 'approve' or 'reject'")
        with db.session(s.settings.db_path) as conn:
            err = db.resolve_review(conn, review_id, approve=body.action == "approve",
                                    note=(body.note or None))
        if err:
            raise HTTPException(409, err)
        try:
            export_all(s.settings)
        except Exception:
            log.exception("export after review failed")
        return build_result(s.settings)

    @app.get("/api/sessions/{sid}/download/{fmt}")
    def download(sid: str, fmt: str):
        s = need(sid)
        if fmt not in DOWNLOADS:
            raise HTTPException(404, "Unknown format")
        key, media = DOWNLOADS[fmt]
        tracker.track("downloaded", s.visitor, s.src, s.id)
        res = export_all(s.settings)               # always current: includes reviewer decisions
        name = FILES[key]
        path = res.paths[list(FILES).index(key)]     # same order as FILES; may carry a fallback name if locked
        return FileResponse(path, media_type=media, filename=name)

    @app.delete("/api/sessions/{sid}")
    def end(sid: str):
        try:
            mgr.delete(sid)
        except RuntimeError:
            raise HTTPException(409, "Still processing.")
        return {"deleted": True}

    return app
