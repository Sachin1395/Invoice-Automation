"""Whole pipeline (ingest -> extract) through the runner, with a scripted fake Gemini."""
import json
import time

import pytest

from invoice_pipeline import db
from invoice_pipeline import runner as runner_mod
from invoice_pipeline.llm.types import FatalLLMError, NoUsableModelsError
from invoice_pipeline.models import JobStatus
from invoice_pipeline.runner import run_once

from conftest import fake_exe_bytes, make_text_pdf
from fakes import (
    FakeLLM, auth_error, bad_request_error, daily_quota_error, make_sample_invoice_pdf, sample_json, server_error,
)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)


def rows(settings, sql, *args):
    with db.session(settings.db_path) as conn:
        return conn.execute(sql, args).fetchall()


def test_two_invoices_in_one_pdf(ex_settings, drop):
    drop("both.pdf", make_sample_invoice_pdf())
    llm = FakeLLM({"*": [sample_json()]})
    [res] = run_once(ex_settings, llm=llm)

    assert res.status == JobStatus.DONE and res.detail == "2 invoice(s)"
    assert not list(ex_settings.inbox_dir.iterdir())
    assert len(list(ex_settings.processed_dir.iterdir())) == 1

    [ext] = rows(ex_settings, "SELECT * FROM extractions")
    assert ext["status"] == "ok" and ext["model_used"] == "model-a"
    assert (ext["input_tokens"], ext["output_tokens"]) == (1000, 400)
    invoices = json.loads(ext["parsed_json"])["invoices"]
    assert [i["invoice_number"] for i in invoices] == ["MKT-318400606", "IN-22242"]
    assert invoices[0]["invoice_date"] == "2026-09-03"
    assert invoices[1]["grounding"]["grand_total"] is True       # text layer confirmed it
    assert invoices[1]["grounding"]["subtotal"] is None          # null in -> no evidence out

    # The request carried both page images and the per-page text layer
    parts = llm.calls[0][1]
    assert sum(type(p).__name__ == "ImagePart" for p in parts) == 2
    assert any("Text layer for page 2" in getattr(p, "text", "") for p in parts)


def test_ingest_rejections_never_reach_the_api(ex_settings, drop):
    drop("fake.pdf", fake_exe_bytes())
    llm = FakeLLM({"*": [sample_json()]})
    [res] = run_once(ex_settings, llm=llm)
    assert res.status == JobStatus.REJECTED and llm.calls == []


def test_exact_duplicate_never_reaches_the_api(ex_settings, drop):
    data = make_sample_invoice_pdf()
    drop("one.pdf", data)
    llm = FakeLLM({"*": [sample_json()]})
    run_once(ex_settings, llm=llm)
    drop("two.pdf", data)
    [res] = run_once(ex_settings, llm=llm)
    assert res.status == JobStatus.DUPLICATE
    assert len(llm.calls) == 1


def test_cached_extraction_skips_the_api(ex_settings, drop):
    """If a later stage failed, resubmitting the file must not pay for extraction again."""
    data = make_sample_invoice_pdf()
    drop("a.pdf", data)
    llm = FakeLLM({"*": [sample_json()]})
    [first] = run_once(ex_settings, llm=llm)
    with db.session(ex_settings.db_path) as conn:  # simulate: downstream stage failed afterwards
        conn.execute("UPDATE jobs SET status='failed' WHERE job_id=?", (first.job_id,))

    drop("a_again.pdf", data)
    [second] = run_once(ex_settings, llm=llm)
    assert second.status == JobStatus.DONE and second.detail == "2 invoice(s) (cached)"
    assert len(llm.calls) == 1


def test_invalid_output_fails_the_job_and_keeps_the_raw_response(ex_settings, drop):
    drop("x.pdf", make_text_pdf())
    llm = FakeLLM({"*": ["this is not json"]})
    [res] = run_once(ex_settings, llm=llm)
    assert res.status == JobStatus.FAILED
    assert [p.name for p in ex_settings.rejected_dir.iterdir()] == ["x.pdf"]
    [ext] = rows(ex_settings, "SELECT * FROM extractions")
    assert ext["status"] == "failed" and ext["raw_response"] == "this is not json"


def test_quota_exhaustion_pauses_the_run_and_a_later_run_resumes_the_same_job(ex_settings, drop):
    for name in ("a.pdf", "b.pdf", "c.pdf"):
        drop(name, make_text_pdf(text=f"invoice {name} " * 6))
    exhausted = FakeLLM({"*": [daily_quota_error()]})
    results = run_once(ex_settings, llm=exhausted)

    assert len(results) == 1                      # stopped at the first file, did not hammer the API
    assert results[0].status == JobStatus.PENDING
    assert len(list(ex_settings.inbox_dir.iterdir())) == 3   # nothing moved
    [job] = rows(ex_settings, "SELECT * FROM jobs")
    assert job["status"] == "pending" and "unavailable" in job["last_error"]
    assert job["next_attempt_at"] is not None and job["attempts"] == 1

    healthy = FakeLLM({"*": [sample_json()]})     # next day: quota is back
    results = run_once(ex_settings, llm=healthy)
    assert [r.status for r in results] == [JobStatus.DONE] * 3
    jobs = rows(ex_settings, "SELECT * FROM jobs ORDER BY job_id")
    assert len(jobs) == 3                         # the deferred job was resumed, not duplicated
    assert jobs[0]["attempts"] == 2 and jobs[0]["status"] == "done"


def test_outage_on_every_model_stops_the_run_after_the_first_file(ex_settings, drop):
    for i in range(4):
        drop(f"f{i}.pdf", make_text_pdf(text=f"unique invoice text number {i} " * 4))
    llm = FakeLLM({"*": [server_error(503)]})
    results = run_once(ex_settings, llm=llm)
    assert len(results) == 1                      # both models are resting: no point trying the rest
    assert len(list(ex_settings.inbox_dir.iterdir())) == 4


def test_circuit_breaker_stops_after_consecutive_deferrals(ex_settings, drop):
    """Models stay usable here (rejected requests don't rest them), so the breaker is what stops the run."""
    for i in range(4):
        drop(f"f{i}.pdf", make_text_pdf(text=f"unique invoice text number {i} " * 4))
    llm = FakeLLM({"*": [bad_request_error()]})
    results = run_once(ex_settings, llm=llm)      # breaker_threshold = 2
    assert len(results) == 2
    assert len(list(ex_settings.inbox_dir.iterdir())) == 4


def test_overloaded_primary_is_only_waited_on_once(ex_settings, drop):
    for i in range(3):
        drop(f"f{i}.pdf", make_text_pdf(text=f"unique invoice text number {i} " * 4))
    llm = FakeLLM({"model-a": [server_error(503)], "model-b": [sample_json()]})
    results = run_once(ex_settings, llm=llm)
    assert [r.status for r in results] == [JobStatus.DONE] * 3
    assert llm.models_called().count("model-a") == 2   # first file only (1 try + 1 retry); the others skip it


def test_fallback_model_is_used_and_recorded(ex_settings, drop):
    drop("a.pdf", make_text_pdf())
    llm = FakeLLM({"model-a": [daily_quota_error()], "model-b": [sample_json()]})
    [res] = run_once(ex_settings, llm=llm)
    assert res.status == JobStatus.DONE
    assert rows(ex_settings, "SELECT model_used FROM extractions")[0]["model_used"] == "model-b"


def test_auth_failure_aborts_the_run_and_leaves_the_file_alone(ex_settings, drop):
    drop("a.pdf", make_text_pdf())
    with pytest.raises(FatalLLMError):
        run_once(ex_settings, llm=FakeLLM({"*": [auth_error()]}))
    assert [p.name for p in ex_settings.inbox_dir.iterdir()] == ["a.pdf"]
    [job] = rows(ex_settings, "SELECT * FROM jobs")
    assert job["status"] == "pending"


def test_no_usable_models_aborts_before_creating_any_job(ex_settings, drop):
    drop("a.pdf", make_text_pdf())
    with pytest.raises(NoUsableModelsError):
        run_once(ex_settings, llm=FakeLLM(models=("something-else",)))
    assert rows(ex_settings, "SELECT * FROM jobs") == []
    assert len(list(ex_settings.inbox_dir.iterdir())) == 1


def test_missing_api_key_gives_a_clear_error(ex_settings, drop, monkeypatch):
    drop("a.pdf", make_text_pdf())
    monkeypatch.setattr(runner_mod, "get_api_key", lambda: None)
    with pytest.raises(FatalLLMError, match="GEMINI_API_KEY"):
        run_once(ex_settings)


def test_empty_inbox_needs_no_key_and_no_api_call(ex_settings, monkeypatch):
    monkeypatch.setattr(runner_mod, "get_api_key", lambda: None)
    assert run_once(ex_settings) == []


def test_old_database_is_migrated_in_place(settings):
    """A DB created by the ingest-only release must gain the new columns and table."""
    import sqlite3
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.db_path)
    conn.executescript("""
        CREATE TABLE jobs (job_id INTEGER PRIMARY KEY AUTOINCREMENT, sha256 TEXT,
            source_path TEXT NOT NULL, original_filename TEXT NOT NULL, status TEXT NOT NULL,
            rejection_reason TEXT, error_message TEXT, duplicate_of INTEGER,
            attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
            started_at TEXT, finished_at TEXT);
        INSERT INTO jobs (source_path, original_filename, status, created_at)
            VALUES ('x', 'x.pdf', 'done', '2026-10-25');
    """)
    conn.commit(); conn.close()

    db.init_db(settings.db_path)
    with db.session(settings.db_path) as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(jobs)")}
        assert {"last_error", "next_attempt_at"} <= cols
        assert c.execute("SELECT COUNT(*) n FROM extractions").fetchone()["n"] == 0
        assert c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] == 1   # data kept
