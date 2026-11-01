"""SQLite persistence: the `files` (dedup) and `jobs` (queue) tables."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .models import JobStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    sha256         TEXT PRIMARY KEY,
    first_filename TEXT NOT NULL,
    size_bytes     INTEGER NOT NULL,
    mime           TEXT NOT NULL,
    page_count     INTEGER NOT NULL,
    first_seen_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256             TEXT,                -- NULL until the file is hashed
    source_path        TEXT NOT NULL,
    original_filename  TEXT NOT NULL,
    status             TEXT NOT NULL,
    rejection_reason   TEXT,
    error_message      TEXT,                -- unexpected exceptions only
    duplicate_of       INTEGER REFERENCES jobs(job_id),
    attempts           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT,
    last_error         TEXT,                -- why the job was deferred
    next_attempt_at    TEXT                 -- when a deferred job is worth retrying
);

CREATE TABLE IF NOT EXISTS extractions (
    extraction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id         INTEGER REFERENCES jobs(job_id),
    sha256         TEXT NOT NULL,
    profile        TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model_used     TEXT,
    status         TEXT NOT NULL,           -- ok | failed
    raw_response   TEXT,
    parsed_json    TEXT,                    -- {"invoices": [...]} incl. grounding evidence
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    latency_ms     INTEGER,
    error          TEXT,
    created_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_extractions_lookup
    ON extractions(sha256, profile, prompt_version, status);

-- One row per invoice found in a file. status: pending_route | accepted | review | rejected
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id       INTEGER,
    job_id              INTEGER,
    sha256              TEXT,
    seq                 INTEGER,
    status              TEXT NOT NULL,
    route               TEXT,
    score               REAL,
    vendor_id           TEXT,
    vendor_fold         TEXT,
    seller_name         TEXT,
    invoice_number      TEXT,
    number_norm         TEXT,
    number_fold         TEXT,
    number_shape        TEXT,
    order_number_norm   TEXT,
    invoice_date        TEXT,
    currency            TEXT,
    subtotal            TEXT,
    computed_subtotal   TEXT,
    total_tax           TEXT,
    grand_total         TEXT,
    duplicate_of        INTEGER,
    flags_json          TEXT,
    field_confidence_json TEXT,
    data_json           TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_key ON invoices(vendor_fold, number_fold);
CREATE INDEX IF NOT EXISTS idx_invoices_vendor_date ON invoices(vendor_fold, invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_extraction ON invoices(extraction_id);
CREATE INDEX IF NOT EXISTS idx_invoices_order ON invoices(order_number_norm);

CREATE TABLE IF NOT EXISTS invoice_lines (
    line_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   INTEGER NOT NULL,
    line_no      INTEGER,
    description  TEXT,
    hsn_sac      TEXT,
    quantity     TEXT,
    unit_price   TEXT,
    discount     TEXT,
    net_amount   TEXT,
    tax_total    TEXT,
    line_total   TEXT,
    taxes_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_lines_invoice ON invoice_lines(invoice_id);

-- Items waiting for a person. invoice_id is NULL for file-level items (nothing readable in an image).
CREATE TABLE IF NOT EXISTS review_queue (
    review_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id    INTEGER,
    extraction_id INTEGER,
    job_id        INTEGER,
    sha256        TEXT,
    status        TEXT NOT NULL,        -- open | approved | rejected
    reasons_json  TEXT,
    summary       TEXT,
    created_at    TEXT NOT NULL,
    resolved_at   TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status);

CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_sha256 ON jobs(sha256);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def session(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection, commit on success, roll back on error, always close."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
# them to a database created earlier, so existing files are upgraded in place.
_ADDED_COLUMNS = {
    "jobs": {"last_error": "TEXT", "next_attempt_at": "TEXT", "rejection_detail": "TEXT"},
    "invoices": {"confirmations_json": "TEXT"},
}


def init_db(db_path: Path) -> None:
    with session(db_path) as conn:
        conn.executescript(SCHEMA)
        for table, columns in _ADDED_COLUMNS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# --- jobs ----------------------------------------------------------------
def create_job(conn: sqlite3.Connection, source_path: str, original_filename: str) -> int:
    cur = conn.execute(
        "INSERT INTO jobs (source_path, original_filename, status, created_at) "
        "VALUES (?, ?, ?, ?)",
        (source_path, original_filename, JobStatus.PENDING.value, _now()),
    )
    return int(cur.lastrowid)


def start_job(conn: sqlite3.Connection, job_id: int) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, started_at=?, attempts=attempts+1 WHERE job_id=?",
        (JobStatus.PROCESSING.value, _now(), job_id),
    )


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    status: JobStatus,
    *,
    sha256: Optional[str] = None,
    rejection_reason: Optional[str] = None,
    error_message: Optional[str] = None,
    duplicate_of: Optional[int] = None,
    rejection_detail: Optional[str] = None,
) -> None:
    conn.execute(
        "UPDATE jobs SET status=?, sha256=COALESCE(?, sha256), rejection_reason=?, "
        "error_message=?, duplicate_of=?, finished_at=?, rejection_detail=? WHERE job_id=?",
        (status.value, sha256, rejection_reason, error_message,
         duplicate_of, _now(), rejection_detail, job_id),
    )


def find_pending_job(conn: sqlite3.Connection, source_path: str) -> Optional[int]:
    """A deferred job for this path, so a re-run resumes it instead of duplicating it."""
    row = conn.execute(
        "SELECT job_id FROM jobs WHERE source_path=? AND status=? ORDER BY job_id LIMIT 1",
        (source_path, JobStatus.PENDING.value),
    ).fetchone()
    return int(row["job_id"]) if row else None


def defer_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    error: str,
    next_attempt_at: Optional[str] = None,
    sha256: Optional[str] = None,
) -> None:
    """Put a job back to pending (API outage / quota). The file stays in the inbox."""
    conn.execute(
        "UPDATE jobs SET status=?, last_error=?, next_attempt_at=?, "
        "sha256=COALESCE(?, sha256), finished_at=NULL WHERE job_id=?",
        (JobStatus.PENDING.value, error[:1000], next_attempt_at, sha256, job_id),
    )


def get_job(conn: sqlite3.Connection, job_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()


def find_done_job(conn: sqlite3.Connection, sha256: str) -> Optional[int]:
    """job_id of the earliest *successful* job for this hash, if any.

    Only `done` jobs block resubmission, so a file whose earlier attempt
    failed or was rejected can be submitted again.
    """
    row = conn.execute(
        "SELECT job_id FROM jobs WHERE sha256=? AND status=? ORDER BY job_id LIMIT 1",
        (sha256, JobStatus.DONE.value),
    ).fetchone()
    return int(row["job_id"]) if row else None


# --- files ---------------------------------------------------------------
def upsert_file(
    conn: sqlite3.Connection,
    sha256: str,
    first_filename: str,
    size_bytes: int,
    mime: str,
    page_count: int,
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO files "
        "(sha256, first_filename, size_bytes, mime, page_count, first_seen_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (sha256, first_filename, size_bytes, mime, page_count, _now()),
    )


# --- extractions ---------------------------------------------------------
def save_extraction(
    conn: sqlite3.Connection,
    *,
    job_id: int,
    sha256: str,
    profile: str,
    prompt_version: str,
    status: str,
    model_used: Optional[str] = None,
    raw_response: Optional[str] = None,
    parsed_json: Optional[str] = None,
    input_tokens: Optional[int] = None,
    output_tokens: Optional[int] = None,
    latency_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO extractions (job_id, sha256, profile, prompt_version, model_used, status, "
        "raw_response, parsed_json, input_tokens, output_tokens, latency_ms, error, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, sha256, profile, prompt_version, model_used, status, raw_response,
         parsed_json, input_tokens, output_tokens, latency_ms, error, _now()),
    )
    return int(cur.lastrowid)


def find_cached_extraction(
    conn: sqlite3.Connection, sha256: str, profile: str, prompt_version: str
) -> Optional[sqlite3.Row]:
    """Latest successful extraction for this file + profile + prompt version.
    A hit means the API call (and its quota/cost) can be skipped entirely."""
    return conn.execute(
        "SELECT * FROM extractions WHERE sha256=? AND profile=? AND prompt_version=? "
        "AND status='ok' ORDER BY extraction_id DESC LIMIT 1",
        (sha256, profile, prompt_version),
    ).fetchone()


# --- invoices ------------------------------------------------------------
ACTIVE_STATUSES = ("accepted", "review", "pending_route")  # rows that count for duplicate checks


def delete_invoices_for_extraction(conn: sqlite3.Connection, extraction_id: int) -> None:
    """Makes validation idempotent: a re-run replaces its own earlier rows."""
    ids = [r["invoice_id"] for r in conn.execute(
        "SELECT invoice_id FROM invoices WHERE extraction_id=?", (extraction_id,))]
    for i in ids:
        conn.execute("DELETE FROM invoice_lines WHERE invoice_id=?", (i,))
    conn.execute("DELETE FROM review_queue WHERE extraction_id=? AND status='open'", (extraction_id,))
    conn.execute("DELETE FROM invoices WHERE extraction_id=?", (extraction_id,))


def insert_invoice(conn, *, extraction_id, job_id, sha256, seq, inv: dict, validation: dict, keys: dict) -> int:
    import json
    now = _now()
    cur = conn.execute(
        "INSERT INTO invoices (extraction_id, job_id, sha256, seq, status, route, score, vendor_id, vendor_fold, "
        "seller_name, invoice_number, number_norm, number_fold, number_shape, order_number_norm, invoice_date, "
        "currency, subtotal, computed_subtotal, total_tax, grand_total, duplicate_of, flags_json, "
        "field_confidence_json, data_json, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'pending_route', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (extraction_id, job_id, sha256, seq, validation["route"], validation["score"],
         keys.get("vendor_id"), keys.get("vendor_fold"), (inv.get("seller") or {}).get("name"),
         inv.get("invoice_number"), keys.get("number_norm"), keys.get("number_fold"), keys.get("number_shape"),
         keys.get("order_norm"), inv.get("invoice_date"), inv.get("currency"), inv.get("subtotal"),
         validation.get("computed_subtotal"), inv.get("total_tax"), inv.get("grand_total"),
         validation.get("duplicate_of"), json.dumps(validation["flags"]),
         json.dumps(validation["field_confidence"]), json.dumps(inv), now, now),
    )
    invoice_id = int(cur.lastrowid)
    conn.execute("UPDATE invoices SET confirmations_json=? WHERE invoice_id=?",
                 (json.dumps(validation.get("confirmations") or []), invoice_id))
    for r in inv.get("line_items") or []:
        taxes = r.get("taxes") or []
        tax_total = None
        try:
            from decimal import Decimal
            if taxes and all(t.get("amount") not in (None, "") for t in taxes):
                tax_total = str(sum(Decimal(str(t["amount"])) for t in taxes))
        except Exception:
            tax_total = None
        conn.execute(
            "INSERT INTO invoice_lines (invoice_id, line_no, description, hsn_sac, quantity, unit_price, "
            "discount, net_amount, tax_total, line_total, taxes_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (invoice_id, r.get("line_no"), r.get("description"), r.get("hsn_sac"), r.get("quantity"),
             r.get("unit_price"), r.get("discount"), r.get("net_amount"), tax_total, r.get("line_total"),
             json.dumps(taxes)),
        )
    return invoice_id


def set_invoice_status(conn: sqlite3.Connection, invoice_ids: list, status: str) -> None:
    for i in invoice_ids:
        conn.execute("UPDATE invoices SET status=?, updated_at=? WHERE invoice_id=?", (status, _now(), i))


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> Optional[sqlite3.Row]:
    return conn.execute("SELECT * FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()


def _active_clause() -> str:
    return "status IN (" + ",".join("?" for _ in ACTIVE_STATUSES) + ")"


def find_invoices_by_folded_key(conn, vendor_fold: str, number_fold: str) -> list:
    return conn.execute(
        f"SELECT * FROM invoices WHERE vendor_fold=? AND number_fold=? AND {_active_clause()} ORDER BY invoice_id",
        (vendor_fold, number_fold, *ACTIVE_STATUSES)).fetchall()


def find_invoices_same_day(conn, vendor_fold: str, invoice_date: str) -> list:
    return conn.execute(
        f"SELECT * FROM invoices WHERE vendor_fold=? AND invoice_date=? AND {_active_clause()} ORDER BY invoice_id",
        (vendor_fold, invoice_date, *ACTIVE_STATUSES)).fetchall()


def accepted_shapes(conn, vendor_fold: str) -> set:
    return {r["number_shape"] for r in conn.execute(
        "SELECT DISTINCT number_shape FROM invoices WHERE vendor_fold=? AND status='accepted' "
        "AND number_shape IS NOT NULL", (vendor_fold,))}


# --- review queue --------------------------------------------------------
def add_review_item(conn, *, invoice_id, extraction_id, job_id, sha256, reasons: list, summary: str) -> int:
    import json
    cur = conn.execute(
        "INSERT INTO review_queue (invoice_id, extraction_id, job_id, sha256, status, reasons_json, summary, created_at) "
        "VALUES (?, ?, ?, ?, 'open', ?, ?, ?)",
        (invoice_id, extraction_id, job_id, sha256, json.dumps(reasons), summary, _now()))
    return int(cur.lastrowid)


def list_review(conn, status: Optional[str] = "open") -> list:
    q = ("SELECT r.*, i.invoice_number, i.seller_name, i.grand_total, i.currency, i.score "
         "FROM review_queue r LEFT JOIN invoices i ON i.invoice_id = r.invoice_id")
    if status:
        return conn.execute(q + " WHERE r.status=? ORDER BY r.review_id", (status,)).fetchall()
    return conn.execute(q + " ORDER BY r.review_id").fetchall()


def get_review(conn, review_id: int):
    return conn.execute("SELECT * FROM review_queue WHERE review_id=?", (review_id,)).fetchone()


def resolve_review(conn, review_id: int, approve: bool, note: Optional[str] = None) -> Optional[str]:
    """Returns an error message, or None on success."""
    row = get_review(conn, review_id)
    if row is None:
        return f"no review item {review_id}"
    if row["status"] != "open":
        return f"review item {review_id} is already {row['status']}"
    conn.execute("UPDATE review_queue SET status=?, resolved_at=?, note=? WHERE review_id=?",
                 ("approved" if approve else "rejected", _now(), note, review_id))
    if row["invoice_id"] is not None:
        set_invoice_status(conn, [row["invoice_id"]], "accepted" if approve else "rejected")
    return None


def invoice_job_id(conn, invoice_id: int) -> Optional[int]:
    row = conn.execute("SELECT job_id FROM invoices WHERE invoice_id=?", (invoice_id,)).fetchone()
    return int(row["job_id"]) if row and row["job_id"] is not None else None
