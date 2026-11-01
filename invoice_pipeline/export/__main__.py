"""python -m invoice_pipeline.export  -> rewrite output/ from the current database."""
import logging
import sys

from ..config import get_settings
from . import export_all


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    settings = get_settings()
    result = export_all(settings)
    print(f"accepted: {result.accepted}   waiting for review: {result.needs_review}   rejected: {result.rejected}")
    for p in result.paths:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
