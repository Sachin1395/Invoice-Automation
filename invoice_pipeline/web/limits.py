"""Demo guard rails: the free Gemini quota is shared by every visitor, so cap how much one visitor,
and the whole demo, can use. Counters live in memory and reset on restart. 0 turns a limit off.

    DEMO_MAX_FILES_PER_IP_PER_HOUR   default 10
    DEMO_MAX_FILES_PER_DAY           default 150   (all visitors, per UTC day)
"""
from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, default)), 0)
    except ValueError:
        return default


@dataclass
class DemoLimits:
    per_ip_per_hour: int = 10
    per_day: int = 150
    _ip: dict = field(default_factory=lambda: defaultdict(deque))
    _day: str = ""
    _day_count: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def from_env(cls) -> "DemoLimits":
        return cls(per_ip_per_hour=_env_int("DEMO_MAX_FILES_PER_IP_PER_HOUR", 10),
                   per_day=_env_int("DEMO_MAX_FILES_PER_DAY", 150))

    def take(self, ip: str, files: int) -> Optional[str]:
        """Reserve `files` uploads. Returns None if allowed, else the message to show the visitor."""
        now = time.time()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._lock:
            if self._day != today:
                self._day, self._day_count = today, 0
            if self.per_day and self._day_count + files > self.per_day:
                return "The demo has reached its daily limit (it runs on a shared free quota). Please try again tomorrow."
            hits = self._ip[ip]
            while hits and hits[0] < now - 3600:
                hits.popleft()
            if self.per_ip_per_hour and len(hits) + files > self.per_ip_per_hour:
                wait = max(int((hits[0] + 3600 - now) / 60) + 1, 1) if hits else 60
                return (f"Limit reached: {self.per_ip_per_hour} files per hour in this demo. "
                        f"Try again in about {wait} minute{'s' if wait != 1 else ''}.")
            hits.extend([now] * files)
            self._day_count += files
        return None
