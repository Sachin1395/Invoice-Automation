"""Live progress events. Nodes call emit(); whoever started the run decides where they go.

The listener and the current file live in context variables, so two runs in two threads (two web
sessions) never see each other's events. With no listener, emit() does nothing.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Callable, Optional

STAGES = ("RUN", "INGEST", "EXTRACT", "VALIDATE", "ROUTE", "EXPORT")

_listener: ContextVar[Optional[Callable[[dict], None]]] = ContextVar("invoice_events_listener", default=None)
_file: ContextVar[Optional[str]] = ContextVar("invoice_events_file", default=None)


def set_listener(fn: Optional[Callable[[dict], None]]):
    """Install a listener for the current context. Returns a token for reset_listener()."""
    return _listener.set(fn)


def reset_listener(token) -> None:
    _listener.reset(token)


def set_file(name: Optional[str]) -> None:
    _file.set(name)


def emit(stage: str, text: str, level: str = "", *, job_id: Optional[int] = None,
         file: Optional[str] = None) -> None:
    """level: '' normal | 'ok' success | 'w' needs attention | 'e' failure."""
    fn = _listener.get()
    if fn is None:
        return
    try:
        fn({"stage": stage, "text": text, "level": level, "job_id": job_id, "file": file or _file.get()})
    except Exception:       # a broken listener must never break the pipeline
        pass
