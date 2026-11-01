# Invoice pipeline: ingest + extraction (v1)

    pip install -r requirements.txt
    python -m pytest -q                       # fake Gemini, no key needed
    copy .env.example .env                    # then put your GEMINI_API_KEY in .env
    # drop PDF/JPEG/PNG/HEIC files into inbox/, then:
    python -m invoice_pipeline
    RUN_LIVE=1 python -m pytest tests/test_live_smoke.py -s    # one real API call
    python show.py                            # print what was extracted (invoices, lines, confidence, grounding)

## Flow per file
Ingest: size -> SHA-256 -> magic-byte type -> dedup (only `done` jobs block) -> integrity
-> render (PDF 200 DPI) -> text layer (per page, if >= 50 chars) -> normalize -> pages/<sha>/page_NNN.jpg

Extract: cache check -> Gemini (page images + text layer, structured JSON) -> schema validation
-> grounding evidence -> `extractions` table. One file can hold several invoices.

## Model chain (config.py `model_chain`)
Tried in order; models your key can't use are dropped at startup; a 404 mid-run or an exhausted
daily quota skips that model for the rest of the run; a model that stays overloaded (503/504) rests for
`model_cooldown_s` so only the first file pays the wait. If every model is unavailable the run pauses:
files stay in inbox/, jobs go back to `pending` with `last_error` / `next_attempt_at`, and the
next run resumes the same jobs.

## Outcomes
done -> processed/ | duplicate -> processed/ | rejected (bad file) -> rejected/
failed (model output never valid, or unexpected error) -> rejected/ | deferred (API outage/quota) -> stays in inbox/

New customer or country = new folder under invoice_pipeline/profiles/ (schema + prompt) and
`extraction_profile` in config.py. Validation (arithmetic, completeness, duplicates, confidence update) is the next node.


## Validation and routing (v1)
After extraction every invoice is checked: arithmetic (both tax-exclusive and tax-inclusive pricing, amount in
words), completeness, GST sanity (GSTIN check digit, tax type vs place of supply, rates, dates), and duplicates
(exact, look-alike I/1 O/0, same seller+date+total, seller invoice-number pattern). Confidence is then updated
from that evidence and each invoice is routed:

- **autopass**: no serious flags, total independently confirmed, and either at or below `low_risk_limit`
  (config.py, default 5000 INR) or matched to a cleared bank charge (hook in validation/bank.py, not wired yet)
- **human_review**: anything doubtful, every look-alike duplicate, and high-value invoices that can't be verified
- **hard_reject**: exact duplicates (same seller, number and total) and text files with no invoice in them

    python -m invoice_pipeline.review list
    python -m invoice_pipeline.review show 3
    python -m invoice_pipeline.review approve 3 --note "checked"
    python -m invoice_pipeline.review reject 3
    python show.py            # extraction + validation result per invoice

## Output (Excel, CSV, JSON)
The last graph node, `export`, rewrites the files in `output/` from the database after every file, once more at
the end of a run (so rejected files show up), and after every `review approve` / `review reject`.
It is a view of the database: running it again changes nothing and never changes a status.

    python -m invoice_pipeline.export       # regenerate by hand

| File | Contents |
|---|---|
| `invoices.xlsx` | sheets: Invoices (purchase-register layout), Line items, Needs review, Rejected, Summary |
| `invoices.csv`, `line_items.csv` | the Invoices and Line items sheets, flat, UTF-8 with BOM (opens correctly in Excel) |
| `invoices.json` | full detail: totals, lines, approval, confidence, checks, source file; plus review and rejected lists |

Only `accepted` invoices (autopassed, or approved by a reviewer) are in the Invoices/Line items files. Items
waiting for a person are in "Needs review"; duplicates, reviewer rejections and unreadable files are in "Rejected".
If Excel has `invoices.xlsx` open, the new copy is saved as `invoices_<timestamp>.xlsx` instead of failing.

## Web UI (upload, live log, review, downloads)

    pip install -r requirements.txt
    python -m invoice_pipeline.web            # then open http://127.0.0.1:8000

Upload up to 5 PDF/JPEG/PNG files. The page shows the pipeline log live while it runs, then:

- **Invoices table**: one tab per invoice with every extracted field, line items, the checks that passed, and the
  flags that did not. Each invoice carries a badge: Autopass, Needs human review, Approved/Rejected by reviewer,
  or Rejected by the pipeline (with the reasons in a box above the table).
- **Rejected and duplicate files**: files that never became an invoice (unreadable, wrong type, password
  protected, exact duplicate of an earlier file or invoice, model unavailable) with a plain-language reason.
- **Approve / Reject** on invoices that need review. This calls the same code as `python -m invoice_pipeline.review`
  and refreshes the exports.
- **Downloads**: Excel (all sheets, including Needs review and Rejected), CSV, JSON.

Every upload gets its own session folder under `sessions/<id>/` (own inbox, SQLite file, pages, output), so
duplicates are only detected within one session and nothing leaks between visitors. "Start over" deletes the
session; folders older than 2 hours are purged automatically. Runs go one at a time to respect the free-tier
quota. Needs `GEMINI_API_KEY` in `.env`, as for the command-line run.

| Endpoint | |
|---|---|
| `POST /api/sessions` | upload files, start processing -> `{id}` |
| `GET /api/sessions/{id}?after=N` | status (`queued/running/done/failed`) + log lines after line N |
| `GET /api/sessions/{id}/result` | files, invoices, validation, summary |
| `POST /api/sessions/{id}/review/{review_id}` | `{"action": "approve" \| "reject", "note"?}` |
| `GET /api/sessions/{id}/download/{csv\|lines\|xlsx\|json}` | exports |
| `DELETE /api/sessions/{id}` | delete the session's files |

The log lines come from `invoice_pipeline/events.py` (`emit(stage, text, level)`); nothing is printed unless a
listener is installed, so the command-line run is unchanged. Text from uploaded documents is escaped in CSV and
Excel so a line description such as `=HYPERLINK(...)` can never run as a formula.


## Deploying the demo on Render (free)

1. Push this folder to a GitHub repo (`.env` and `sessions/` are git-ignored).
2. Render dashboard: **New + > Blueprint**, pick the repo. It reads `render.yaml` (free plan, one instance, health check `/api/health`).
3. When asked, paste `GEMINI_API_KEY` (never commit it).
4. Open the `*.onrender.com` URL once the build finishes.

Notes
- `.python-version` pins Python 3.12 so PyMuPDF / pillow-heif install from wheels.
- The app reads `PORT` from Render and binds `0.0.0.0` automatically.
- Free plan sleeps after ~15 min idle (first visit then takes about a minute) and its disk is wiped on every restart; sessions are in memory, so run exactly one instance.
- Demo limits (env vars, `0` disables): `DEMO_MAX_FILES_PER_IP_PER_HOUR` (default 10), `DEMO_MAX_FILES_PER_DAY` (default 150, all visitors). Over the limit the API returns 429 with a readable message.
- The free Gemini tier may use submitted data for training: keep the "no sensitive documents" notice.
