"""python -m invoice_pipeline  -> process everything currently in inbox/."""
import logging
import sys

from .llm.types import FatalLLMError
from .config import get_settings
from .runner import run_once


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "google_genai", "httpcore"):  # one line per HTTP call is just noise
        logging.getLogger(noisy).setLevel(logging.WARNING)
    try:
        results = run_once()
    except FatalLLMError as exc:
        print(f"\nCannot continue: {exc}", file=sys.stderr)
        return 2
    if not results:
        print("inbox is empty")
    elif get_settings().export_enabled:
        print(f"exports written to {get_settings().output_dir}")
    for r in results:
        print(f"job {r.job_id:>4}  {r.status.value:<10} {r.filename}" + (f"  ({r.detail})" if r.detail else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
