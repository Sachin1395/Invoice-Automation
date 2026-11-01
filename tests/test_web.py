"""The web layer: upload -> live log -> result JSON -> review -> downloads, with a scripted fake Gemini."""
import copy
import csv
import io
import json
import time
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook

from invoice_pipeline import events
from invoice_pipeline.web.app import create_app

from fakes import FakeLLM
from golden import pdf_for, retailez_backpack


def reply(invs):
    return json.dumps({"invoices": invs})


def good():
    return retailez_backpack()


def bad_total():
    inv = retailez_backpack()
    inv["invoice_number"] = "CJB1-2000000"
    inv["grand_total"] = "719.00"
    return inv


@pytest.fixture
def web_settings(val_settings):
    return replace(val_settings, export_enabled=True)


def make_client(settings, tmp_path, llm, synchronous=True):
    app = create_app(settings, llm=llm, sessions_root=tmp_path / "sessions", synchronous=synchronous)
    return TestClient(app), app


def upload(client, files):
    r = client.post("/api/sessions", files=[("files", (n, d, "application/pdf")) for n, d in files])
    assert r.status_code == 200, r.text
    return r.json()["id"]


def mixed_batch():
    """good -> autopass, bad_total -> review, same invoice again -> duplicate, junk -> rejected file."""
    return [("good.pdf", pdf_for([good()])), ("check.pdf", pdf_for([bad_total()])),
            ("again.pdf", pdf_for([good()], footer="re-sent copy")), ("junk.pdf", b"this is not a pdf at all")]


@pytest.fixture
def run_mixed(web_settings, tmp_path):
    llm = FakeLLM({"*": [reply([good()]), reply([bad_total()]), reply([good()])]})
    client, app = make_client(web_settings, tmp_path, llm)
    sid = upload(client, mixed_batch())
    return client, app, sid


def test_result_covers_every_outcome(run_mixed):
    client, _, sid = run_mixed
    assert client.get(f"/api/sessions/{sid}").json()["status"] == "done"
    res = client.get(f"/api/sessions/{sid}/result").json()
    by_name = {f["name"]: f for f in res["files"]}
    assert [f["name"] for f in res["files"]] == ["good.pdf", "check.pdf", "again.pdf", "junk.pdf"]   # upload order

    ok = by_name["good.pdf"]
    assert ok["outcome"] == "done"
    inv = ok["invoices"][0]
    assert inv["validation"]["route"] == "autopass" and inv["invoice_total"] == "709.00"
    assert inv["seller"]["name"] and len(inv["line_items"]) == 2
    assert inv["line_items"][0]["tax_amount"] == "108.16" and inv["line_items"][0]["tax_rate"] == "18"
    assert inv["cgst"] == "54.08" and inv["sgst"] == "54.08"
    assert "No similar invoice found" in inv["validation"]["confirmations"]
    assert not any("_" in c and ":" in c for c in inv["validation"]["confirmations"])    # plain words only

    rev = by_name["check.pdf"]["invoices"][0]["validation"]
    assert rev["route"] == "human_review" and rev["review_id"] and rev["reasons"]
    assert any("719.00" in r for r in rev["reasons"])

    dup = by_name["again.pdf"]
    assert dup["outcome"] == "duplicate" and "invoice CJB1-1303297" in dup["reason"] and "good.pdf" in dup["reason"]
    assert dup["original"]["file"] == "good.pdf" and "invoices" not in dup

    junk = by_name["junk.pdf"]
    assert junk["outcome"] == "rejected" and junk["reason"] and "_" not in junk["reason"]

    s = res["summary"]
    assert (s["files"], s["invoices"], s["autopass"], s["needs_review"], s["duplicate_files"], s["rejected_files"]) \
        == (4, 2, 1, 1, 1, 1)


def test_live_log_is_grouped_by_file_and_readable(run_mixed):
    client, _, sid = run_mixed
    ev = client.get(f"/api/sessions/{sid}?after=0").json()["events"]
    assert [e["seq"] for e in ev] == list(range(1, len(ev) + 1))
    text = [(e["stage"], e["text"], e["level"], e["file"]) for e in ev]
    assert ("INGEST", "File 1/4 · good.pdf", "", "good.pdf") == (text[2][0], text[2][1].rsplit(" · ", 1)[0], text[2][2], text[2][3])
    stages = {t[0] for t in text}
    assert {"RUN", "INGEST", "EXTRACT", "VALIDATE", "ROUTE", "EXPORT"} <= stages
    assert any(t[0] == "ROUTE" and t[1].startswith("Autopass") and t[3] == "good.pdf" for t in text)
    assert any(t[0] == "ROUTE" and t[1].startswith("Sent to human review") and t[2] == "w" for t in text)
    assert any(t[0] == "ROUTE" and t[1].startswith("Rejected") and "Exact duplicate" in t[1] and t[2] == "e"
               and t[3] == "again.pdf" and "#" not in t[1] for t in text)
    assert any(t[0] == "INGEST" and t[1].startswith("File rejected") and t[3] == "junk.pdf" for t in text)
    assert any(t[0] == "EXTRACT" and t[1].startswith("Structured JSON received") for t in text)
    # polling resumes where it left off
    tail = client.get(f"/api/sessions/{sid}?after={len(ev) - 2}").json()
    assert [e["seq"] for e in tail["events"]] == [len(ev) - 1, len(ev)] and tail["next"] == len(ev)


def test_approve_and_reject_through_the_api(web_settings, tmp_path):
    a, b = bad_total(), bad_total()
    b["invoice_number"] = "CJB1-2000001"
    llm = FakeLLM({"*": [reply([a]), reply([b])]})
    client, _ = make_client(web_settings, tmp_path, llm)
    sid = upload(client, [("a.pdf", pdf_for([a])), ("b.pdf", pdf_for([b]))])
    res = client.get(f"/api/sessions/{sid}/result").json()
    ids = [f["invoices"][0]["validation"]["review_id"] for f in res["files"]]
    assert all(ids)

    r = client.post(f"/api/sessions/{sid}/review/{ids[0]}", json={"action": "approve"})
    assert r.status_code == 200
    routes = [f["invoices"][0]["validation"]["route"] for f in r.json()["files"]]
    assert routes == ["approved", "human_review"]

    r = client.post(f"/api/sessions/{sid}/review/{ids[1]}", json={"action": "reject", "note": "wrong total"})
    inv = r.json()["files"][1]["invoices"][0]["validation"]
    assert inv["route"] == "reviewer_rejected" and "wrong total" in inv["reasons"][0]
    assert r.json()["summary"]["rejected_invoices"] == 1 and r.json()["summary"]["needs_review"] == 0

    assert client.post(f"/api/sessions/{sid}/review/{ids[0]}", json={"action": "reject"}).status_code == 409   # already decided
    assert client.post(f"/api/sessions/{sid}/review/{ids[0]}", json={"action": "maybe"}).status_code == 400


def test_downloads_reflect_decisions(run_mixed):
    client, _, sid = run_mixed
    rid = client.get(f"/api/sessions/{sid}/result").json()["files"][1]["invoices"][0]["validation"]["review_id"]
    csv_before = client.get(f"/api/sessions/{sid}/download/csv")
    assert csv_before.status_code == 200 and "invoices.csv" in csv_before.headers["content-disposition"]
    assert len(list(csv.reader(io.StringIO(csv_before.text.lstrip("\ufeff"), newline="")))) == 2          # header + 1 accepted
    client.post(f"/api/sessions/{sid}/review/{rid}", json={"action": "approve"})
    rows = list(csv.reader(io.StringIO(client.get(f"/api/sessions/{sid}/download/csv").text.lstrip("\ufeff"), newline="")))
    assert len(rows) == 3

    wb = load_workbook(io.BytesIO(client.get(f"/api/sessions/{sid}/download/xlsx").content))
    assert {"Invoices", "Line items", "Needs review", "Rejected", "Summary"} <= set(wb.sheetnames)
    rejected = [[c.value for c in r] for r in wb["Rejected"].iter_rows(min_row=2)]
    assert any("again.pdf" in str(r) for r in rejected) and any("junk.pdf" in str(r) for r in rejected)

    data = client.get(f"/api/sessions/{sid}/download/json").json()
    assert data
    assert client.get(f"/api/sessions/{sid}/download/pdf").status_code == 404


def test_hard_rejected_invoice_inside_a_file_is_shown_with_its_reason(web_settings, tmp_path):
    first, second = good(), good()
    second["source_pages"] = [2]
    llm = FakeLLM({"*": [reply([first, second])]})
    client, _ = make_client(web_settings, tmp_path, llm)
    sid = upload(client, [("twice.pdf", pdf_for([first, second]))])
    f = client.get(f"/api/sessions/{sid}/result").json()["files"][0]
    assert f["outcome"] == "done"
    routes = [i["validation"]["route"] for i in f["invoices"]]
    assert routes == ["autopass", "rejected"]
    why = f["invoices"][1]["validation"]["reasons"]
    assert why and "Exact duplicate of invoice CJB1-1303297" in why[0] and "#" not in why[0]


def test_uploaded_text_cannot_run_as_a_spreadsheet_formula(web_settings, tmp_path):
    evil = good()
    evil["line_items"][0]["description"] = "=HYPERLINK(\"http://evil\",\"x\")"
    client, _ = make_client(web_settings, tmp_path, FakeLLM({"*": [reply([evil])]}))
    sid = upload(client, [("e.pdf", pdf_for([evil]))])
    assert "'=HYPERLINK" in client.get(f"/api/sessions/{sid}/download/lines").text
    wb = load_workbook(io.BytesIO(client.get(f"/api/sessions/{sid}/download/xlsx").content))
    cells = [c for row in wb["Line items"].iter_rows() for c in row if isinstance(c.value, str) and "HYPERLINK" in c.value]
    assert cells and all(c.data_type == "s" for c in cells)


def test_session_isolation_and_cleanup(web_settings, tmp_path):
    llm = FakeLLM({"*": [reply([good()])]})
    client, app = make_client(web_settings, tmp_path, llm)
    s1 = upload(client, [("one.pdf", pdf_for([good()]))])
    s2 = upload(client, [("one.pdf", pdf_for([good()]))])          # same invoice in another session: not a duplicate
    assert client.get(f"/api/sessions/{s2}/result").json()["files"][0]["outcome"] == "done"
    root = app.state.sessions.sessions[s1].root
    assert root.exists()
    assert client.delete(f"/api/sessions/{s1}").json() == {"deleted": True}
    assert not root.exists()
    assert client.get(f"/api/sessions/{s1}/result").status_code == 404
    assert client.get(f"/api/sessions/{s2}/result").status_code == 200


def test_upload_limits(web_settings, tmp_path):
    client, _ = make_client(replace(web_settings, max_file_mb=1), tmp_path, FakeLLM({"*": [reply([])]}))
    six = [(f"f{i}.pdf", b"x") for i in range(6)]
    assert client.post("/api/sessions", files=[("files", (n, d)) for n, d in six]).status_code == 400
    assert client.post("/api/sessions", files=[("files", ("big.pdf", b"x" * (2 * 1048576)))]).status_code == 413
    assert client.post("/api/sessions").status_code == 422


def test_odd_file_names_are_made_safe_and_unique(web_settings, tmp_path):
    client, app = make_client(web_settings, tmp_path, FakeLLM({"*": [reply([])]}))
    sid = upload(client, [("../../etc/pass wd.pdf", b"x"), ("a.pdf", b"x"), ("a.pdf", b"y")])
    names = [f["name"] for f in client.get(f"/api/sessions/{sid}/result").json()["files"]]
    assert sorted(names) == ["a (2).pdf", "a.pdf", "pass wd.pdf"]


def test_missing_api_key_fails_the_session_with_a_readable_message(web_settings, tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr("invoice_pipeline.runner.get_api_key", lambda: None)
    client, _ = make_client(web_settings, tmp_path, llm=None)
    sid = upload(client, [("a.pdf", pdf_for([good()]))])
    st = client.get(f"/api/sessions/{sid}").json()
    assert st["status"] == "failed" and "GEMINI_API_KEY" in st["error"]
    assert any(e["level"] == "e" for e in st["events"])


def test_background_run_streams_events_from_the_worker_thread(web_settings, tmp_path):
    llm = FakeLLM({"*": [reply([good()])]})
    client, _ = make_client(web_settings, tmp_path, llm, synchronous=False)
    sid = upload(client, [("good.pdf", pdf_for([good()]))])
    seen, after, deadline = [], 0, time.time() + 30
    while time.time() < deadline:
        st = client.get(f"/api/sessions/{sid}?after={after}").json()
        seen += st["events"]
        after = st["next"]
        if st["status"] in ("done", "failed"):
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    stages = [e["stage"] for e in seen]
    assert stages[0] == "RUN" and "EXTRACT" in stages and "ROUTE" in stages        # LangGraph nodes reached the listener


def test_two_sessions_never_see_each_others_events(web_settings, tmp_path):
    llm = FakeLLM({"*": [reply([good()])]})
    client, app = make_client(web_settings, tmp_path, llm)
    a = upload(client, [("alpha.pdf", pdf_for([good()]))])
    b = upload(client, [("beta.pdf", pdf_for([good()]))])
    files = lambda sid: {e["file"] for e in client.get(f"/api/sessions/{sid}").json()["events"]} - {None}
    assert files(a) == {"alpha.pdf"} and files(b) == {"beta.pdf"}


def test_events_are_silent_without_a_listener():
    events.emit("RUN", "nobody is listening")          # must not raise


def test_home_page_is_served_and_wired_to_the_api(web_settings, tmp_path):
    client, _ = make_client(web_settings, tmp_path, FakeLLM({"*": [reply([])]}))
    html = client.get("/").text
    assert "/api/sessions" in html and "Mock" not in html and "simulated" not in html


def test_favicon_is_served_for_the_default_browser_request(web_settings, tmp_path):
    client, _ = make_client(web_settings, tmp_path, FakeLLM({"*": [reply([])]}))
    for url in ("/favicon.ico", "/favicon.svg"):
        r = client.get(url)
        assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml") and "<svg" in r.text


def test_demo_limits_per_visitor_and_per_day(web_settings, tmp_path):
    from invoice_pipeline.web.limits import DemoLimits
    app = create_app(web_settings, llm=FakeLLM({"*": [reply([good()])]}), sessions_root=tmp_path / "s",
                     synchronous=True, limits=DemoLimits(per_ip_per_hour=3, per_day=5))
    client = TestClient(app)
    one = [("files", ("a.pdf", pdf_for([good()]), "application/pdf"))]
    two = one * 2
    mine = {"x-forwarded-for": "1.1.1.1, 10.0.0.1"}
    assert client.post("/api/sessions", files=two, headers=mine).status_code == 200      # 2 of 3
    r = client.post("/api/sessions", files=two, headers=mine)                           # would be 4 of 3
    assert r.status_code == 429 and "per hour" in r.json()["detail"]
    assert client.post("/api/sessions", files=one, headers=mine).status_code == 200      # 3 of 3
    other = {"x-forwarded-for": "2.2.2.2"}
    assert client.post("/api/sessions", files=two, headers=other).status_code == 200     # 5 of 5 today
    r = client.post("/api/sessions", files=one, headers={"x-forwarded-for": "3.3.3.3"})
    assert r.status_code == 429 and "daily limit" in r.json()["detail"]


def test_limits_can_be_switched_off():
    from invoice_pipeline.web.limits import DemoLimits
    off = DemoLimits(per_ip_per_hour=0, per_day=0)
    assert all(off.take("ip", 5) is None for _ in range(50))
