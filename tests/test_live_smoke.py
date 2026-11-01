"""Real Gemini call. Skipped unless RUN_LIVE=1 and GEMINI_API_KEY is available.

    RUN_LIVE=1 python -m pytest tests/test_live_smoke.py -s
"""
import json
import os
from dataclasses import replace

import pytest

from invoice_pipeline import db
from invoice_pipeline.config import get_api_key
from invoice_pipeline.models import JobStatus
from invoice_pipeline.runner import run_once

from fakes import make_sample_invoice_pdf

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE") != "1" or not get_api_key(),
    reason="set RUN_LIVE=1 and GEMINI_API_KEY to call the real API",
)


def test_real_extraction_of_two_invoices(ex_settings, drop):
    from invoice_pipeline.config import Settings
    live = replace(ex_settings, model_chain=Settings().model_chain, throttle_seconds=6.0)
    drop("sample.pdf", make_sample_invoice_pdf())
    [res] = run_once(live)
    assert res.status == JobStatus.DONE, res
    with db.session(live.db_path) as conn:
        row = conn.execute("SELECT * FROM extractions").fetchone()
    invoices = json.loads(row["parsed_json"])["invoices"]
    print("\nmodel:", row["model_used"], "tokens:", row["input_tokens"], row["output_tokens"])
    print(json.dumps(invoices, indent=2)[:3000])
    assert {i["invoice_number"] for i in invoices} == {"MKT-318400606", "IN-22242"}
