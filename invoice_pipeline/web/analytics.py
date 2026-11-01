"""Anonymous funnel counts written to a Google Sheet (via an Apps Script web app).

Events: opened, started, completed, downloaded. Each row is time, event, visitor, src, session.
The visitor is a random id made by the browser; no IP address, file name, file content or email is sent.

    ANALYTICS_WEBHOOK_URL   Apps Script web app URL  (unset = analytics off)
    ANALYTICS_SECRET        shared secret the script checks

Sending happens on a background thread. It can never slow down or break a request.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from typing import Callable, Optional

log = logging.getLogger("invoice_pipeline.analytics")

EVENTS = ("opened", "started", "completed", "downloaded")
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def clean(value: Optional[str], limit: int = 40) -> str:
    return _SAFE.sub("", value or "")[:limit]


def _post(url: str, row: dict) -> None:
    import httpx
    httpx.post(url, json=row, timeout=10, follow_redirects=True).raise_for_status()


class Tracker:
    def __init__(self, url: str = "", secret: str = "", sender: Optional[Callable[[str, dict], None]] = None,
                 ignore_src: tuple = ("me",)):
        self.url, self.secret, self.ignore_src = url, secret, ignore_src
        self._send = sender or _post
        self._q: "queue.Queue[dict]" = queue.Queue(maxsize=500)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "Tracker":
        return cls(os.environ.get("ANALYTICS_WEBHOOK_URL", "").strip(), os.environ.get("ANALYTICS_SECRET", "").strip())

    @property
    def enabled(self) -> bool:
        return bool(self.url)

    def track(self, event: str, visitor: str = "", src: str = "", session: str = "") -> bool:
        """Queue one event. Returns False when it was ignored (off, unknown event, your own `me` visits, queue full)."""
        src = clean(src)
        if not self.enabled or event not in EVENTS or src in self.ignore_src:
            return False
        row = {"secret": self.secret, "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
               "event": event, "visitor": clean(visitor), "src": src or "direct", "session": clean(session)}
        try:
            self._q.put_nowait(row)
        except queue.Full:
            return False
        self._ensure_thread()
        return True

    def flush(self, timeout: float = 5.0) -> None:
        """Wait until queued events are sent (tests)."""
        end = time.time() + timeout
        while self._q.unfinished_tasks and time.time() < end:
            time.sleep(0.01)

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, daemon=True, name="analytics")
                self._thread.start()

    def _loop(self) -> None:
        while True:
            row = self._q.get()
            try:
                self._send(self.url, row)
            except Exception as exc:                  # noqa: BLE001 - analytics must never matter
                log.warning("analytics send failed: %s", exc)
            finally:
                self._q.task_done()
