"""One upload = one session = its own folders and SQLite file.

That keeps every visitor's invoices apart (duplicates are only detected inside a session), lets
'your files are deleted when your session ends' be literally true, and means one run never
reads another one's database.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from .. import db, events
from ..config import Settings
from ..llm.client import LLMClient
from ..llm.types import FatalLLMError
from ..runner import run_once

log = logging.getLogger("invoice_pipeline.web")

MAX_FILES = 5
_SAFE = re.compile(r"[^A-Za-z0-9._ ()\-]+")


def clean_name(name: str, taken: set) -> str:
    base = _SAFE.sub("_", Path(name or "").name).strip(" .") or "file"
    stem, dot, ext = base.rpartition(".")
    stem, ext = (stem, "." + ext) if dot and stem else (base, "")
    candidate, n = base, 1
    while candidate.lower() in taken:
        n += 1
        candidate = f"{stem} ({n}){ext}"
    taken.add(candidate.lower())
    return candidate


@dataclass
class Session:
    id: str
    root: Path
    settings: Settings
    created: float = field(default_factory=time.time)
    status: str = "queued"            # queued | running | done | failed
    error: Optional[str] = None
    total: int = 0
    log: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    t0: float = field(default_factory=time.time)
    visitor: str = ""
    src: str = ""

    def add_event(self, e: dict) -> None:
        with self.lock:
            self.log.append({"seq": len(self.log) + 1, "t": round(time.time() - self.t0, 1),
                             "stage": e["stage"], "text": e["text"], "level": e.get("level") or "",
                             "file": e.get("file")})

    def events_after(self, after: int) -> list:
        with self.lock:
            return list(self.log[after:])


class SessionManager:
    def __init__(self, base: Settings, root: Path, llm: Optional[LLMClient] = None,
                 ttl_s: float = 2 * 3600, synchronous: bool = False, tracker=None):
        self.tracker = tracker
        self.base, self.root, self.llm, self.ttl_s = base, Path(root), llm, ttl_s
        self.synchronous = synchronous          # tests: run in the calling thread
        self.sessions: dict[str, Session] = {}
        self._run_slot = threading.Lock()       # one model-bound run at a time: the free tier is tight
        self.root.mkdir(parents=True, exist_ok=True)
        self.purge_stale()

    # --- lifecycle -----------------------------------------------------------
    def create(self, uploads: list[tuple[str, bytes]], visitor: str = "", src: str = "") -> Session:
        self.purge_stale()
        sid = uuid.uuid4().hex[:16]
        d = self.root / sid
        s = replace(self.base, inbox_dir=d / "inbox", processed_dir=d / "processed", rejected_dir=d / "rejected",
                    pages_dir=d / "pages", db_path=d / "data" / "invoices.db", output_dir=d / "output")
        s.ensure_dirs()
        taken: set = set()
        now = time.time()
        for i, (name, data) in enumerate(uploads):
            path = s.inbox_dir / clean_name(name, taken)
            path.write_bytes(data)
            os.utime(path, (now + i, now + i))      # the run takes files oldest first = upload order
        sess = Session(id=sid, root=d, settings=s, total=len(uploads), visitor=visitor, src=src)
        self.sessions[sid] = sess
        if self.synchronous:
            self._run(sess)
        else:
            threading.Thread(target=self._run, args=(sess,), daemon=True, name=f"session-{sid}").start()
        return sess

    def get(self, sid: str) -> Optional[Session]:
        return self.sessions.get(sid)

    def delete(self, sid: str) -> bool:
        sess = self.sessions.get(sid)
        if sess is None:
            return False
        if sess.status in ("queued", "running"):
            raise RuntimeError("still running")
        self.sessions.pop(sid, None)
        shutil.rmtree(sess.root, ignore_errors=True)
        return True

    def purge_stale(self) -> None:
        cutoff = time.time() - self.ttl_s
        for sid, sess in list(self.sessions.items()):
            if sess.created < cutoff and sess.status not in ("queued", "running"):
                self.sessions.pop(sid, None)
        for d in self.root.iterdir() if self.root.exists() else []:     # folders left by an earlier server process
            if d.is_dir() and d.name not in self.sessions and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)

    # --- the run ---------------------------------------------------------------
    def _run(self, sess: Session) -> None:
        token = events.set_listener(sess.add_event)
        try:
            if not self._run_slot.acquire(blocking=False):
                events.emit("RUN", "Waiting for another run to finish")
                self._run_slot.acquire()
            try:
                sess.status, sess.t0 = "running", time.time()
                run_once(sess.settings, llm=self.llm)
                sess.status = "done"
                if self.tracker:
                    self.tracker.track("completed", sess.visitor, sess.src, sess.id)
            finally:
                self._run_slot.release()
        except FatalLLMError as exc:
            sess.status, sess.error = "failed", str(exc)
            events.emit("RUN", f"Stopped: {exc}", "e")
        except Exception as exc:                     # noqa: BLE001 - surface it, don't kill the server thread
            log.exception("session %s crashed", sess.id)
            sess.status, sess.error = "failed", f"Unexpected error: {exc}"
            events.emit("RUN", "Stopped by an unexpected error", "e")
        finally:
            events.reset_listener(token)
