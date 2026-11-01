"""FileSource: where invoices come from. Swap implementations freely."""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


class FileSource(ABC):
    @abstractmethod
    def discover(self) -> list[Path]:
        """Return files waiting to be processed, oldest first."""

    @abstractmethod
    def mark_processed(self, path: Path) -> Path:
        """Called when a file was handled successfully (ok or duplicate)."""

    @abstractmethod
    def mark_rejected(self, path: Path) -> Path:
        """Called when a file was rejected or failed."""
