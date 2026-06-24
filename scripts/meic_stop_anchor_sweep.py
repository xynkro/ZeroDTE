#!/usr/bin/env python3
"""Stop-anchor experiment — does anchoring the breakeven stop to the REAL fill
credit (vs the BS MODEL credit) fix the live 100%-stop-rate / capped-edge problem?

THE LIVE BUG (found in the Jun-23 debrief): the stop threshold is
`ic.total_credit_dollars × 1.05`, where total_credit_dollars is the BS MODEL credit.
But Alpaca fills land ~1.5-1.9× richer, and the buy-back marks off the (rich) CBOE
chain. So the "breakeven" stop actually fires at ~1.05/R of the REAL credit — a tight
early exit. Live stop-rate ran 100% vs the backtest's 58%, and "stopped" nights
finished green (Jun-22 +$154, 3/3 stopped) because the stop fired while still net
profitable in real terms.

This sweeps credit-richness R ∈ {1.0, 1.3, 1.5, 1.8} and compares two stop anchors:
  • model  — threshold = 1.05 × model credit  (current LIVE behavior; the bug)
  • real   — threshold = 1.05 × real credit    (the proposed fix; true breakeven)

Honesty caveats: R is applied uniformly to entry credit AND intraday buy-back; the
expiry intrinsic is unscaled (richness is time-premium). Real R varies night to night;
this is a directional study to decide whether to QUEUE the change for the gated weekly
loop — NOT a deploy. Live N is 9 condor nights (< the 20-night gate).

Run: PYTHONPATH=. .venv/bin/python scripts/meic_stop_anchor_sweep.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401 — load .env
from backend.app.config import settings
from scripts.meic_backtest import run_meic

BUF = getattr(settings, "IC_STOP_BUFFER", 1.05)


def _day_stats(entries):
    if not entries:
        return None
    by_day = defaultdict(float)
    for e in entries:
        by_day[e["date"]] += e["pnl"]
    days = list(by_day.values())
    mean_d = st.mean(days)
    from backend.app.quant_utils import newey_west_tstat
    t = newey_west_tstat(days)["t"]   # HAC t — naive overstates on autocorrelated daily P&L
    stops = sum(1 for e in entries if e["outcome"] == "stop")
    return {
        "n_days": len(days), "mean_day": mean_d, "worst_day": min(days),
        "green_pct": sum(1 for d in days if d > 0) / len(days) * 100,
        "stop_rate": stops / len(entries) * 100, "t": t,
        "mean_entry": st.mean(e["pnl"] for e in entries),
    }


if __name__ == "__main__":
    ladder = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
    print(f"=== MEIC stop-anchor sweep | ladder {ladder} | buffer ×{BUF} | $50 RT ===")
    print("    model = threshold off BS model credit (LIVE BUG) · real = off collected credit (FIX)\n")
    print(f"{'R':>4} {'anchor':>6} | {'stop%':>6} {'green%':>7} {'$/day':>9} "
          f"{'worst':>9} {'t':>6} {'$/SPY/day':>10}")
    print("    " + "-" * 70)
    for R in (1.0, 1.3, 1.5, 1.8):
        for anchor in ("model", "real"):
            e = run_meic(ladder, cost_rt=50.0, stop_buffer=BUF,
                         credit_richness=R, stop_anchor=anchor)
            s = _day_stats(e)
            if not s:
                continue
            print(f"{R:>4.1f} {anchor:>6} | {s['stop_rate']:>5.0f}% {s['green_pct']:>6.0f}% "
                  f"{s['mean_day']:>+9.0f} {s['worst_day']:>+9.0f} {s['t']:>6.2f} "
                  f"{s['mean_day']/10:>+10.1f}")
        if R != 1.8:
            print()
    print("\n  Read: at R=1.0 the two anchors are identical (model==real). As R rises")
    print("  (real fills richer), 'model' keeps firing the stop early — higher stop-rate,")
    print("  thinner $/day, tighter worst-day. 'real' lets winners ride to fuller decay.")
    print("  The fix is worth queuing iff 'real' lifts $/day & t WITHOUT blowing worst-day")
    print("  past the live config's risk tolerance. This is the gated-loop decision input.")
