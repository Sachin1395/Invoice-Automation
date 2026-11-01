"""python -m invoice_pipeline.web  -> http://127.0.0.1:8000"""
import argparse
import logging
import os

import uvicorn

from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m invoice_pipeline.web")
    hosted = "PORT" in os.environ                      # Render and similar hosts tell us the port to use
    ap.add_argument("--host", default="0.0.0.0" if hosted else "127.0.0.1")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for noisy in ("httpx", "google_genai", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    print(f"\n  Invoice Processing Agent -> http://{args.host}:{args.port}\n")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
