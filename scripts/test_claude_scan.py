#!/usr/bin/env python3
"""One-shot manual test of the Claude morning scan (run any time, market open or not).

Usage: cd ~/Documents/Trading/ZeroDTE-Wave && PYTHONPATH=. .venv/bin/python scripts/test_claude_scan.py

Requires ANTHROPIC_API_KEY in .env. Builds a minimal context from the historical
file's last session + a live GEX snapshot if available, runs ONE real API call,
prints the verdict, and appends it to data/claude_scan.jsonl tagged test=True.
"""
from __future__ import annotations

import asyncio
import json

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app.claude_scan import run_scan, append_scan


async def main():
    import os
    ctx = {"session": "manual test", "note": "offline test context",
           "spot": 7500.0, "open_move_pct": 0.12, "daily_atr": 55.0,
           "gex": {"regime": "positive", "max_pain": 7480.0},
           "macro_events_today": [{"event": "ISM Services PMI", "time_utc": "14:00", "impact": "medium"}]}
    if settings.ANTHROPIC_API_KEY:
        print(f"transport: API · calling {settings.CLAUDE_SCAN_MODEL} ...")
        v = await run_scan(ctx, settings.ANTHROPIC_API_KEY, settings.CLAUDE_SCAN_MODEL)
    elif os.path.exists(settings.CLAUDE_SCAN_BIN):
        from backend.app.claude_scan import run_scan_cli
        print(f"transport: CLI (subscription, no key) · calling {settings.CLAUDE_SCAN_MODEL} ...")
        v = await run_scan_cli(ctx, settings.CLAUDE_SCAN_MODEL, settings.CLAUDE_SCAN_BIN)
    else:
        print("❌ no ANTHROPIC_API_KEY and no claude CLI found — nothing to test.")
        return
    if v is None:
        print("❌ scan failed (see log above) — check key/model/network.")
        return
    print("✅ verdict:")
    print(json.dumps(v, indent=2))
    append_scan({"date": "test", "test": True, "context": ctx, "verdict": v, "ok": True})
    print("appended to data/claude_scan.jsonl (test row)")


if __name__ == "__main__":
    asyncio.run(main())
