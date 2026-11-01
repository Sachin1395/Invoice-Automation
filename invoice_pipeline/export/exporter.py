"""export_all: regenerate every output file from the database.

It is a view, not a log: running it twice changes nothing, and it never changes a status. An
invoice appears in the 'accepted' files once it is accepted (autopassed, or approved by a
reviewer); the next export picks it up.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .. import db
from ..config import Settings
from .data import load_export_data
from .writers import write_invoices_csv, write_json, write_lines_csv, write_xlsx

log = logging.getLogger("invoice_pipeline.export")

FILES = {
    "xlsx": "invoices.xlsx",
    "invoices_csv": "invoices.csv",
    "lines_csv": "line_items.csv",
    "json": "invoices.json",
}


@dataclass
class ExportResult:
    paths: list = field(default_factory=list)
    accepted: int = 0
    needs_review: int = 0
    rejected: int = 0
    used_fallback_names: bool = False


def _safe_write(path: Path, write, stamp: str) -> tuple[Path, bool]:
    """Write to a temp file, then swap it in. If the target is locked (an open Excel window on
    Windows), keep the data under a time-stamped name instead of failing the pipeline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    write(tmp)
    try:
        os.replace(tmp, path)
        return path, False
    except PermissionError:
        alt = path.with_name(f"{path.stem}_{stamp}{path.suffix}")
        os.replace(tmp, alt)
        log.warning("%s is open in another program; wrote %s instead. Close the file to get the usual name back.",
                    path.name, alt.name)
        return alt, True


def export_all(settings: Settings) -> ExportResult:
    db.init_db(settings.db_path)
    now = datetime.now(timezone.utc)
    with db.session(settings.db_path) as conn:
        data = load_export_data(conn, now.isoformat(timespec="seconds"))

    out = settings.output_dir
    stamp = now.strftime("%Y%m%d_%H%M%S")
    result = ExportResult(accepted=len(data.accepted), needs_review=len(data.review), rejected=len(data.rejected))
    writers = {
        "xlsx": lambda p: write_xlsx(p, data),
        "invoices_csv": lambda p: write_invoices_csv(p, data),
        "lines_csv": lambda p: write_lines_csv(p, data),
        "json": lambda p: write_json(p, data),
    }
    for key, name in FILES.items():
        path, fallback = _safe_write(out / name, writers[key], stamp)
        result.paths.append(path)
        result.used_fallback_names |= fallback
    log.info("exported %d accepted, %d awaiting review, %d rejected -> %s",
             result.accepted, result.needs_review, result.rejected, out)
    return result
