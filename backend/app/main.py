"""ZeroDTE backend entry point.
Run with: ZeroDTE/.venv/bin/python -m app.main
Or:       ZeroDTE/.venv/bin/uvicorn app.api:app --reload --host 0.0.0.0 --port 8765
"""
from __future__ import annotations

import logging
import sys

import uvicorn

from .config import settings


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    uvicorn.run(
        "app.api:app",
        host=settings.BACKEND_HOST,
        port=settings.BACKEND_PORT,
        reload=False,
        log_level="info",
        loop="asyncio",   # ib_insync's nest_asyncio doesn't support uvloop
    )


if __name__ == "__main__":
    main()
