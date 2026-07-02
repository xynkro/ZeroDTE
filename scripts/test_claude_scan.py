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
    if not settings.ANTHROPIC_API_KEY:
        print("❌ ANTHROPIC_API_KEY not set in .env — add it (the file, not chat), then rerun.")
        return
    ctx = {"session": "manual test", "note": "offline test context",
           "spot": 7500.0, "open_move_pct": 0.12, "daily_atr": 55.0,
           "gex": {"regime": "positive", "max_pain": 7480.0},
           "macro_events_today": [{"event": "ISM Services PMI", "time_utc": "14:00", "impact": "medium"}]}
    print(f"calling {settings.CLAUDE_SCAN_MODEL} ...")
    v = await run_scan(ctx, settings.ANTHROPIC_API_KEY, settings.CLAUDE_SCAN_MODEL)
    if v is None:
        print("❌ scan failed (see log above) — check key/model/network.")
        return
    print("✅ verdict:")
    print(json.dumps(v, indent=2))
    append_scan({"date": "test", "test": True, "context": ctx, "verdict": v, "ok": True})
    print("appended to data/claude_scan.jsonl (test row)")


if __name__ == "__main__":
    asyncio.run(main())
