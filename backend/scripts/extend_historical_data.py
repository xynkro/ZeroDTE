"""Extend SPX historical data via Alpaca IEX feed (SPY × 10 → SPX-equivalent).

Pulls SPY 5m bars from 2022-01-01 to now, scales to SPX (multiplier × 10),
filters to RTH 09:30-16:00 ET only, and saves as JSON in the historical dir.

Output: backend/data/historical/SPX_5m_3y.json (or whatever the date range covers).

Run from project root:
  source .venv/bin/activate
  python3 backend/scripts/extend_historical_data.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")

KEY = os.environ["ALPACA_API_KEY"]
SEC = os.environ["ALPACA_SECRET_KEY"]
DATA_URL = os.environ.get("ALPACA_DATA_URL", "https://data.alpaca.markets")
FEED = os.environ.get("ALPACA_FEED", "iex")

ET = ZoneInfo("America/New_York")
START = "2022-01-03T00:00:00Z"  # First Monday of 2022
END = datetime.now(ET).strftime("%Y-%m-%dT%H:%M:%SZ")
SCALE = 10.0  # SPY × 10 ≈ SPX

OUT_DIR = ROOT / "backend" / "data" / "historical"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def fetch_all_bars(symbol: str = "SPY") -> list[dict]:
    """Paginate through all 5m bars from START to END."""
    client = httpx.Client(
        headers={
            "APCA-API-KEY-ID": KEY,
            "APCA-API-SECRET-KEY": SEC,
        },
        timeout=60.0,
    )
    bars: list[dict] = []
    page_token: str | None = None
    page = 0
    while True:
        page += 1
        params = {
            "timeframe": "5Min",
            "start": START,
            "end": END,
            "feed": FEED,
            "adjustment": "raw",
            "limit": "10000",
        }
        if page_token:
            params["page_token"] = page_token
        resp = client.get(f"{DATA_URL}/v2/stocks/{symbol}/bars", params=params)
        resp.raise_for_status()
        data = resp.json()
        chunk = data.get("bars", [])
        bars.extend(chunk)
        print(f"  Page {page}: +{len(chunk)} bars (total {len(bars)})  page_token={'...' if data.get('next_page_token') else 'END'}")
        page_token = data.get("next_page_token")
        if not page_token:
            break
        time.sleep(0.05)  # Light pacing
    client.close()
    return bars


def to_spx_rth(bars_raw: list[dict]) -> list[dict]:
    """Scale SPY → SPX, filter to RTH 09:30-15:55 ET, keep one bar per timestamp."""
    out: list[dict] = []
    seen: set[str] = set()
    for b in bars_raw:
        # Alpaca returns UTC timestamps like "2025-01-03T14:30:00Z"
        ts_utc = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
        ts_et = ts_utc.astimezone(ET)
        # RTH 09:30-15:55 (last 5m bar ends at 16:00)
        minute = ts_et.hour * 60 + ts_et.minute
        if minute < 9 * 60 + 30 or minute > 15 * 60 + 55:
            continue
        # Skip weekends defensively
        if ts_et.weekday() >= 5:
            continue
        key = ts_et.isoformat()
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "datetime": ts_et.isoformat(),
            "open":   round(b["o"] * SCALE, 2),
            "high":   round(b["h"] * SCALE, 2),
            "low":    round(b["l"] * SCALE, 2),
            "close":  round(b["c"] * SCALE, 2),
            "volume": float(b.get("v", 0) or 0),
        })
    # Sort by datetime ascending
    out.sort(key=lambda x: x["datetime"])
    return out


def main():
    print(f"Fetching SPY 5m bars from {START} to {END} (feed={FEED}, scale=×{SCALE})")
    print()
    raw = fetch_all_bars("SPY")
    print(f"\nRaw bars: {len(raw)}")
    if not raw:
        print("ERROR: no bars returned")
        sys.exit(1)
    bars = to_spx_rth(raw)
    print(f"After RTH filter + SPX scaling: {len(bars)} bars")
    if not bars:
        print("ERROR: no bars after filtering")
        sys.exit(1)
    first = datetime.fromisoformat(bars[0]["datetime"])
    last = datetime.fromisoformat(bars[-1]["datetime"])
    days = (last - first).days
    print(f"Date range: {bars[0]['datetime']} → {bars[-1]['datetime']}")
    print(f"Span: {days} days (~{days / 365:.1f} years)")
    out_path = OUT_DIR / "SPX_5m_3y.json"
    out_path.write_text(json.dumps(bars, separators=(",", ":")))
    size_mb = out_path.stat().st_size / 1024 / 1024
    print(f"\nSaved: {out_path} ({size_mb:.1f} MB)")
    print(f"OK")


if __name__ == "__main__":
    main()
