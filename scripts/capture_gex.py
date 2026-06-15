#!/usr/bin/env python3
"""One-shot GEX snapshot → append to backend/data/gex_history.jsonl.

The live backend already logs a snapshot on every refresh (every GEX_REFRESH_MIN
during 07:00–16:30 ET). This standalone capture is a BACKSTOP: run it from cron/
launchd at a fixed time (e.g. ~10:00 ET, near the wave/MEIC entry window) so the
history keeps accruing even if the backend is down. No historical GEX feed exists
— CBOE serves current-day only — so this rolling file IS the future backtest set.

Run: PYTHONPATH=. .venv/bin/python scripts/capture_gex.py
Cron: 0 10 * * 1-5  cd ~/Documents/Trading/ZeroDTE && PYTHONPATH=. .venv/bin/python scripts/capture_gex.py
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import backend.app.config  # noqa: F401 — loads .env
from backend.app.config import settings
from backend.app import gex as gexmod

ET = ZoneInfo("America/New_York")


async def _run() -> int:
    res = await gexmod.fetch_gex(settings.GEX_SYMBOL)
    if not res.ok:
        print(f"GEX fetch failed: {res.error}")
        return 1
    et = datetime.now(ET)
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "date": et.strftime("%Y-%m-%d"), "et": et.strftime("%H:%M"),
        "symbol": settings.GEX_SYMBOL, "source": "capture_gex",
        "regime": res.regime, "net_ratio": res.net_ratio,
        "net_gex_b": res.net_gex_b, "spot": res.spot,
        "call_wall": res.call_wall, "put_wall": res.put_wall, "asof": res.asof,
    }
    gexmod.append_history(rec)
    print(f"appended GEX snapshot: {res.summary()}  → {gexmod.GEX_HISTORY_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
