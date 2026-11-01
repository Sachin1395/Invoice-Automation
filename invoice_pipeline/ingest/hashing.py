"""Step: content hashing."""
from __future__ import annotations

import hashlib


def sha256_hex(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()
