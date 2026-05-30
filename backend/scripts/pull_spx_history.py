"""Pull extended SPX 5min history from IBKR — chunked to respect duration limits.

IBKR limits:
  - 5min bars: max 30 days per request
  - Pacing: max 60 historical-data requests per 600 seconds

We fetch 30-day chunks working backwards. Default 12 chunks = ~360 days = 1Y.

Run with the backend STOPPED (so we don't fight for clientId).
Or use a different clientId via env var.

  cd backend
  ../.venv/bin/python -m scripts.pull_spx_history --months 12 --out SPX_5m_1y.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ib_insync import IB, Index

from app.config import settings


log = logging.getLogger("pull_spx_history")
ET = ZoneInfo("America/New_York")


async def fetch_chunk(ib: IB, contract, end_dt: datetime, duration_str: str) -> list:
    """Fetch one chunk of 5m bars ending at end_dt.

    end_dt format for IBKR: 'YYYYMMDD HH:MM:SS' in UTC, with 'UTC' suffix
    (or empty string for 'now').
    """
    if end_dt is None:
        end_str = ""
    else:
        end_str = end_dt.strftime("%Y%m%d-%H:%M:%S")
    bars = await ib.reqHistoricalDataAsync(
        contract=contract,
        endDateTime=end_str,
        durationStr=duration_str,
        barSizeSetting="5 mins",
        whatToShow="TRADES",
        useRTH=True,
        formatDate=2,  # UTC seconds — easier to merge
        keepUpToDate=False,
    )
    return list(bars)


def bar_to_dict(b) -> dict:
    """Convert ib_insync BarData to JSON-friendly dict (ET ISO time)."""
    d = b.date
    if not isinstance(d, datetime):
        d = datetime.fromisoformat(str(d))
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    d_et = d.astimezone(ET)
    return {
        "datetime": d_et.isoformat(),
        "open": float(b.open),
        "high": float(b.high),
        "low": float(b.low),
        "close": float(b.close),
        "volume": float(b.volume) if b.volume else 0.0,
    }


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--months", type=int, default=12, help="approx months back to fetch")
    p.add_argument("--symbol", default="SPX")
    p.add_argument("--exchange", default="CBOE")
    p.add_argument("--out", default="SPX_5m_1y.json")
    p.add_argument("--client-id", type=int, default=43, help="IBKR clientId (different from orchestrator)")
    p.add_argument("--chunk-days", type=int, default=30, help="days per chunk (max 30 for 5m)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    n_chunks = max(1, (args.months * 30 + args.chunk_days - 1) // args.chunk_days)
    log.info("Will fetch %d chunks of %d days each (~%d months total)",
             n_chunks, args.chunk_days, args.months)

    ib = IB()
    await ib.connectAsync(
        host=settings.IBKR_HOST,
        port=settings.IBKR_PORT,
        clientId=args.client_id,
        timeout=10,
    )
    log.info("Connected to IBKR clientId=%d", args.client_id)

    contract = Index(args.symbol, args.exchange, "USD")
    await ib.qualifyContractsAsync(contract)
    log.info("Qualified contract: %s", contract)

    all_bars: dict[str, dict] = {}  # keyed by datetime ISO to dedupe
    end_dt: datetime | None = None  # None = now

    for i in range(n_chunks):
        try:
            log.info("Chunk %d/%d  end=%s", i + 1, n_chunks, end_dt or "now")
            bars = await fetch_chunk(
                ib, contract, end_dt, f"{args.chunk_days} D"
            )
            log.info("  -> got %d bars", len(bars))
            if not bars:
                break
            for b in bars:
                d = bar_to_dict(b)
                all_bars[d["datetime"]] = d
            # Move end backwards to first bar's time MINUS one bar
            first_b = bars[0]
            first_dt = first_b.date if isinstance(first_b.date, datetime) else \
                       datetime.fromisoformat(str(first_b.date))
            if first_dt.tzinfo is None:
                first_dt = first_dt.replace(tzinfo=timezone.utc)
            end_dt = (first_dt - timedelta(minutes=5)).astimezone(timezone.utc)
            # Pacing: stay well under 60 reqs / 600 sec
            await asyncio.sleep(1.0)
        except Exception as e:
            log.error("chunk %d failed: %s", i + 1, e)
            await asyncio.sleep(3.0)
            continue

    ib.disconnect()
    log.info("Disconnected. Total unique bars collected: %d", len(all_bars))

    # Sort by datetime and write
    out_path = settings.data_dir / "historical" / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    sorted_bars = sorted(all_bars.values(), key=lambda b: b["datetime"])
    out_path.write_text(json.dumps(sorted_bars, indent=0))
    log.info("Wrote %d bars to %s", len(sorted_bars), out_path)
    if sorted_bars:
        log.info("Date range: %s -> %s", sorted_bars[0]["datetime"], sorted_bars[-1]["datetime"])
        # Quick stats
        from collections import Counter
        dates = Counter(b["datetime"][:10] for b in sorted_bars)
        log.info("Unique session dates: %d", len(dates))


if __name__ == "__main__":
    asyncio.run(main())
