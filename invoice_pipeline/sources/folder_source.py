"""FileSource backed by an inbox folder, with processed/ and rejected/ subfolders."""
from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Settings
from .base import FileSource


class FolderSource(FileSource):
    def __init__(self, settings: Settings):
        self.settings = settings

    def discover(self) -> list[Path]:
        inbox = self.settings.inbox_dir
        if not inbox.exists():
            return []
        files = [p for p in inbox.iterdir() if p.is_file() and not p.name.startswith(".")]
        return sorted(files, key=lambda p: (p.stat().st_mtime, p.name))

    def mark_processed(self, path: Path) -> Path:
        return self._move(path, self.settings.processed_dir)

    def mark_rejected(self, path: Path) -> Path:
        return self._move(path, self.settings.rejected_dir)

    @staticmethod
    def _move(path: Path, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / path.name
        n = 1
        while dest.exists():  # never overwrite an earlier file with the same name
            dest = dest_dir / f"{path.stem}__{n}{path.suffix}"
            n += 1
        shutil.move(str(path), str(dest))
        return dest
