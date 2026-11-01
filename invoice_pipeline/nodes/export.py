"""The output node: last step of the graph. It regenerates the export files from the database."""
from __future__ import annotations

import logging

from .. import events
from ..config import Settings
from ..export import export_all
from ..models import PipelineState

log = logging.getLogger("invoice_pipeline.export")


def export_node(state: PipelineState, settings: Settings) -> dict:
    try:
        result = export_all(settings)
    except Exception:   # an output problem must never undo or hide a processed invoice
        log.exception("job=%s export failed; invoices are safe in the database, run "
                      "'python -m invoice_pipeline.export' to retry", state.job_id)
        return {"exported_files": []}
    events.emit("EXPORT", f"Exports updated · {result.accepted} accepted · {result.needs_review} to review · "
                          f"{result.rejected} rejected", job_id=state.job_id)
    return {"exported_files": [str(p) for p in result.paths]}
