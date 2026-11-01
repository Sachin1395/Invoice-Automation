"""Central configuration for the pipeline. Every tunable lives here."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    # --- paths -----------------------------------------------------------
    inbox_dir: Path = PROJECT_ROOT / "inbox"
    processed_dir: Path = PROJECT_ROOT / "processed"
    rejected_dir: Path = PROJECT_ROOT / "rejected"
    pages_dir: Path = PROJECT_ROOT / "pages"
    db_path: Path = PROJECT_ROOT / "data" / "invoices.db"
    output_dir: Path = PROJECT_ROOT / "output"

    # --- ingest limits ---------------------------------------------------
    max_file_mb: int = 20
    max_pages: int = 15
    allowed_mimes: frozenset = frozenset(
        {"application/pdf", "image/jpeg", "image/png", "image/heic", "image/heif"}
    )

    # --- rendering / normalization --------------------------------------
    pdf_dpi: int = 200
    max_dimension: int = 2048
    jpeg_quality: int = 88
    min_text_chars: int = 50

    # --- queue (used by the full queue later) ----------------------------
    max_attempts: int = 3

    # --- extraction ------------------------------------------------------
    extraction_enabled: bool = True          # False = ingest only (no API key needed)
    extraction_profile: str = "india_gst"    # folder under invoice_pipeline/profiles/
    # Tried in order. A model this key can't use is dropped at startup.
    model_chain: tuple = ("gemini-3.6-flash", "gemini-3.5-flash-lite")
    # None = leave the model's default (Google advises against low values on Gemini 3 models).
    extraction_temperature: Optional[float] = None
    llm_timeout_s: float = 45.0              # a 2-page invoice normally answers in <15s
    throttle_seconds: float = 6.0            # min gap between API calls (free tier is tight)
    # 429-per-minute / 5xx / timeouts. Retry hard only when there is nowhere else to go:
    max_transient_retries: int = 3               # for the last model still available
    max_transient_retries_with_fallback: int = 1  # for a model that has a fallback behind it
    model_cooldown_s: float = 600.0          # a model that stays overloaded is skipped this long
    parse_retries: int = 1                   # re-prompts after invalid JSON, per model
    backoff_base_s: float = 2.0
    backoff_cap_s: float = 60.0
    max_server_delay_s: float = 90.0         # longest "retry in Ns" we are willing to wait
    breaker_threshold: int = 3               # consecutive deferred files before the run stops

    # --- output ----------------------------------------------------------
    export_enabled: bool = True              # write output/ after each file and after each review decision

    # --- validation ------------------------------------------------------
    validation_enabled: bool = True
    row_tolerance: float = 0.02              # rupees, per line-item check
    invoice_tolerance: float = 0.05          # rupees, per invoice-level check
    round_off_max: float = 0.50              # unexplained difference tolerated as a likely round-off
    scan_id_cap: float = 0.75                # ceiling for identifiers read from an image, until confirmed
    low_risk_limit: float = 5000.0           # INR: at or below this, unverified invoices may autopass
    autopass_min_low_risk: float = 0.70      # invoice score needed to autopass at or below the limit
    autopass_min: float = 0.85               # ... and above it (needs a cleared bank match as well)
    today: Optional[date] = None             # fixed "today" for tests; None = the real date

    @property
    def max_file_bytes(self) -> int:
        return self.max_file_mb * 1024 * 1024

    def ensure_dirs(self) -> None:
        for d in (self.inbox_dir, self.processed_dir, self.rejected_dir,
                  self.pages_dir, self.db_path.parent, self.output_dir):
            d.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()


def load_env() -> None:
    """Load PROJECT_ROOT/.env into the environment (does not override real env vars)."""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover
        return
    load_dotenv(PROJECT_ROOT / ".env", override=False)


def get_api_key() -> Optional[str]:
    load_env()
    return os.environ.get("GEMINI_API_KEY") or None
