#!/usr/bin/env python3
"""MEIC take-profit + time-stop sweep — does an early TP and/or a time exit beat
the live breakeven stop? Tests the configs the 0DTE creators actually run:

  OptionsPlay : 25% TP + time exit (~2h), NO price stop ("stops didn't help")
  ZeroDayMark : 40% TP + 1× (breakeven) credit stop
  tasty-Mark  : ride to expiry, no fixed credit stop (≈ HOLD here)

Run on the SETTLEMENT-FIXED engine (held condors settle at real intrinsic, not
fake-zero — see meic_backtest.py). Judge on per-DAY mean, worst-day, green%,
t-stat. A config only earns a live flip if it beats LIVE on t-stat AND worst-day
(the improve_loop gate) — and even then NOT on one in-sample run.

Caveats (so we don't fool ourselves): in-sample, BS-mid (not real CBOE) pricing,
frozen entry vol, no regime gate, settlement proxied at the 15:55 bar. Directional
read only — this informs the mid-July fork, it is not a deploy signal.

Run: PYTHONPATH=. .venv/bin/python scripts/meic_tp_timestop_sweep.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict, Counter

import backend.app.config  # noqa: F401 — loads .env
from backend.app.config import settings
from scripts.meic_backtest import run_meic


def _agg(entries) -> dict:
    pnls = [e["pnl"] for e in entries]
    by_day = defaultdict(float)
    for e in entries:
        by_day[e["date"]] += e["pnl"]
    days = list(by_day.values())
    mean_d = st.mean(days)
    sd_d = st.stdev(days) if len(days) > 1 else 0.0
    t = mean_d / (sd_d / math.sqrt(len(days))) if sd_d > 0 else 0.0
    mix = Counter(e["outcome"] for e in entries)
    n = len(entries)
    return {
        "n": n, "wr": sum(1 for p in pnls if p > 0) / n * 100,
        "mean_day": mean_d, "worst_day": min(days), "total": sum(pnls),
        "green": sum(1 for d in days if d > 0) / len(days) * 100, "t": t,
        "mix": {k: mix.get(k, 0) / n * 100 for k in ("tp", "stop", "time", "expiry")},
    }


def main() -> None:
    ladder = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
    configs = [
        ("LIVE breakeven 1.05 (no TP)",       dict(stop_buffer=1.05)),
        ("TP25 only · no stop",               dict(stop_buffer=99, take_profit_pct=25)),
        ("TP40 only · no stop",               dict(stop_buffer=99, take_profit_pct=40)),
        ("TP50 only · no stop",               dict(stop_buffer=99, take_profit_pct=50)),
        ("TP40 + breakeven [ZeroDayMark]",    dict(stop_buffer=1.05, take_profit_pct=40)),
        ("TP25 + breakeven",                  dict(stop_buffer=1.05, take_profit_pct=25)),
        ("TP25 + time120 [OptionsPlay]",      dict(stop_buffer=99, take_profit_pct=25, time_stop_min=120)),
        ("TP25 + time90",                     dict(stop_buffer=99, take_profit_pct=25, time_stop_min=90)),
        ("time120 only · no TP/stop",         dict(stop_buffer=99, time_stop_min=120)),
        ("HOLD · no stop/TP [tasty-Mark]",    dict(stop_buffer=99)),
    ]

    print(f"\n=== MEIC TP+time-stop sweep | ladder {ladder} | $50 RT cost | settlement-fixed ===")
    print(f"{'config':<32}  day-mean   worst-day  green%   t-stat   exit-mix tp/stop/time/exp")
    print("-" * 100)
    rows = []
    for label, cfg in configs:
        a = _agg(run_meic(ladder, cost_rt=50.0, **cfg))
        rows.append((label, a))
        m = a["mix"]
        print(f"{label:<32}  ${a['mean_day']:>+7.1f}  ${a['worst_day']:>+8.0f}  {a['green']:>5.0f}%  "
              f"{a['t']:>6.2f}   {m['tp']:>2.0f}/{m['stop']:>2.0f}/{m['time']:>2.0f}/{m['expiry']:>2.0f}")

    live = rows[0][1]
    print("-" * 100)
    print(f"LIVE: day ${live['mean_day']:+.1f} · worst ${live['worst_day']:+.0f} · t={live['t']:.2f}")
    beats = [(l, a) for l, a in rows[1:]
             if a["t"] > live["t"] and a["worst_day"] > live["worst_day"]]
    if beats:
        print("\nBeat LIVE on BOTH t-stat AND worst-day (gate-clearing candidates):")
        for l, a in beats:
            print(f"  ✓ {l:<30} day ${a['mean_day']:+.1f} (Δ{a['mean_day']-live['mean_day']:+.0f}) · "
                  f"worst ${a['worst_day']:+.0f} (Δ{a['worst_day']-live['worst_day']:+.0f}) · t={a['t']:.2f}")
    else:
        print("\nNothing beats LIVE on BOTH axes — no gate-clearing change.")
    print("\nNOTE: in-sample, BS-mid, settlement proxied at 15:55. Directional read for the")
    print("mid-July fork — NOT a deploy signal. Validate forward on live fills first.\n")


if __name__ == "__main__":
    main()
