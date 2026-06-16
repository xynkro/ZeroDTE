#!/usr/bin/env python3
"""MEIC stop-buffer sweep — does the breakeven (1.0×) stop cut winners on chop days,
and would a wider buffer (let the condor breathe) or a tighter one do better?

Context: on a mean-reverting chop day the per-condor breakeven stop fires on brief
short-strike tags that REVERT by expiry, scratching trades that would have been full
winners. But the stop also caps the rare TREND day at ~breakeven instead of full max
loss. This sweeps the stop trigger from tight → wide → hold-to-expiry so the DATA,
not one day, decides the right buffer.

  buffer = X  → stop fires when buy-back ≥ X × credit collected.
    1.00 = breakeven (backtest default)   1.05 = LIVE config (IC_STOP_BUFFER)
    >1   = looser (more room; fewer stops, bigger loss when it does stop)
    99   ≈ no stop (ride every condor to 16:00 expiry)

Judge on: per-DAY mean, worst day, green-day %, t-stat (the validation criteria) —
NOT win rate (breakeven stops scratch losers BY DESIGN, so WR is misleading).

Run: PYTHONPATH=. .venv/bin/python scripts/meic_stop_buffer_sweep.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401 — loads .env
from backend.app.config import settings
from scripts.meic_backtest import run_meic


def _agg(entries) -> dict:
    pnls = [e["pnl"] for e in entries]
    stops = sum(1 for e in entries if e["outcome"] == "stop")
    by_day = defaultdict(float)
    for e in entries:
        by_day[e["date"]] += e["pnl"]
    days = list(by_day.values())
    mean_d = st.mean(days)
    sd_d = st.stdev(days) if len(days) > 1 else 0.0
    t = mean_d / (sd_d / math.sqrt(len(days))) if sd_d > 0 else 0.0
    green = sum(1 for d in days if d > 0) / len(days) * 100
    wins = sum(1 for p in pnls if p > 0)
    return {
        "n": len(entries), "ndays": len(days),
        "stop_rate": stops / len(entries) * 100 if entries else 0,
        "wr": wins / len(entries) * 100 if entries else 0,
        "mean_day": mean_d, "worst_day": min(days), "green": green, "t": t,
        "total": sum(pnls),
    }


def main() -> None:
    ladder = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
    buffers = [1.00, 1.05, 1.15, 1.30, 1.50, 2.00, 99.0]  # 99 ≈ hold-to-expiry
    label = {1.00: "1.00 breakeven", 1.05: "1.05 LIVE", 99.0: "99 ≈ HOLD"}

    print(f"\n=== MEIC stop-buffer sweep | ladder {ladder} | $50 RT cost | full window ===")
    print("buffer          entries  stop%   day-mean   worst-day   green%    t-stat   total")
    print("-" * 84)
    rows = []
    for b in buffers:
        a = _agg(run_meic(ladder, cost_rt=50.0, stop_buffer=b))
        rows.append((b, a))
        lab = label.get(b, f"{b:.2f}")
        print(f"{lab:<14}  {a['n']:>6}  {a['stop_rate']:>4.0f}%  "
              f"${a['mean_day']:>+8.1f}  ${a['worst_day']:>+8.0f}  {a['green']:>5.0f}%  "
              f"{a['t']:>6.2f}  ${a['total']:>+7.0f}")

    # Decision read: best by t-stat, and best by worst-day (the two validation axes)
    best_t = max(rows, key=lambda r: r[1]["t"])
    best_worst = max(rows, key=lambda r: r[1]["worst_day"])
    live = next(r for r in rows if r[0] == 1.05)
    print("-" * 84)
    print(f"LIVE (1.05): day ${live[1]['mean_day']:+.1f} · worst ${live[1]['worst_day']:+.0f} · t={live[1]['t']:.2f}")
    print(f"best t-stat:   buffer {best_t[0]:.2f} (t={best_t[1]['t']:.2f}, day ${best_t[1]['mean_day']:+.1f}, worst ${best_t[1]['worst_day']:+.0f})")
    print(f"best worst-day: buffer {best_worst[0]:.2f} (worst ${best_worst[1]['worst_day']:+.0f}, t={best_worst[1]['t']:.2f})")
    print("\nNote: a change only earns a live flip if it beats LIVE on t-stat AND worst-day")
    print("(the improve_loop gate). WR is expected to RISE with wider buffers — ignore it;")
    print("judge day-mean / worst-day / t-stat.\n")


if __name__ == "__main__":
    main()
