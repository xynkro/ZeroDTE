#!/usr/bin/env python3
"""Edge check — is there a signal worth scaling, or N lucky nights?

The council's "one thing to do first": run the two tools we just built TOGETHER
on the honest data — the fee-inclusive broker-truth daily P&L (broker_ledger) and
the Newey-West HAC t-stat (quant_utils) — and test outlier-night dependence.

Read-only. Answers two questions the gate hangs on:
  1. Does the HAC t-stat clear ~2 (i.e. is the mean daily P&L distinguishable
     from zero once daily autocorrelation is accounted for)?
  2. Is the result carried by 1-2 outlier nights (jackknife: drop the best /
     worst / both, and see what's left)?

Run: PYTHONPATH=. .venv/bin/python scripts/edge_check.py
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics as st

import backend.app.config  # noqa: F401 — load .env
from backend.app.quant_utils import newey_west_tstat

LOG = os.path.join(os.path.dirname(__file__), "..", "backend", "data", "debrief_log.jsonl")


def _fmt(x):
    return f"+${x:,.2f}" if x >= 0 else f"−${abs(x):,.2f}"


def analyze(name: str, dated: list[tuple[str, float]]):
    """dated = [(date, pnl)] in chronological order."""
    print(f"\n{'='*68}\n{name}  ·  n = {len(dated)} days\n{'='*68}")
    if len(dated) < 2:
        print("  too few days to test."); return
    series = [v for _, v in dated]
    total = sum(series)
    mean = st.mean(series)
    nw = newey_west_tstat(series)
    # per-day contributions, sorted by magnitude of effect on the total
    ranked = sorted(dated, key=lambda kv: kv[1])
    print(f"  total {_fmt(total)} · mean/day {_fmt(mean)} · "
          f"green {sum(1 for v in series if v>0)}/{len(series)}")
    print(f"  HAC t-stat (Newey-West): {nw['t']:+.2f}   (naive {nw['naive_t']:+.2f}, L={nw['lags']})")
    print(f"  per-day:  worst {_fmt(ranked[0][1])} ({ranked[0][0]})  ·  "
          f"best {_fmt(ranked[-1][1])} ({ranked[-1][0]})")
    # --- outlier dependence (jackknife) ---
    best_d, best_v = ranked[-1]
    worst_d, worst_v = ranked[0]
    drop_best = [v for d, v in dated if d != best_d]
    drop_worst = [v for d, v in dated if d != worst_d]
    drop_both = [v for d, v in dated if d not in (best_d, worst_d)]
    pct_from_best = (best_v / total * 100) if total else float("nan")
    print("\n  OUTLIER DEPENDENCE (jackknife):")
    print(f"    single best night = {pct_from_best:.0f}% of the total")
    nb = newey_west_tstat(drop_best)
    print(f"    drop best  ({best_d}): total {_fmt(sum(drop_best))} · "
          f"mean {_fmt(st.mean(drop_best))} · HAC t {nb['t']:+.2f}")
    nw2 = newey_west_tstat(drop_worst)
    print(f"    drop worst ({worst_d}): total {_fmt(sum(drop_worst))} · "
          f"mean {_fmt(st.mean(drop_worst))} · HAC t {nw2['t']:+.2f}")
    if len(drop_both) >= 2:
        nbo = newey_west_tstat(drop_both)
        print(f"    drop both: total {_fmt(sum(drop_both))} · "
              f"mean {_fmt(st.mean(drop_both))} · HAC t {nbo['t']:+.2f}")
    # canonical verdict — SAME function the nightly debrief prints, so the script
    # and the Telegram line can never disagree.
    from backend.app.quant_utils import daily_edge_summary, daily_edge_line
    print("\n  " + daily_edge_line(daily_edge_summary(series)))


def load_meic_nights() -> list[tuple[str, float]]:
    out = []
    if not os.path.exists(LOG):
        return out
    by_date = {}
    for line in open(LOG):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        if (r.get("ic_executed") or 0) > 0 and r.get("ic_real_net") is not None:
            by_date[r["date"]] = float(r["ic_real_net"])
    return [(d, by_date[d]) for d in sorted(by_date)]


async def load_broker_days() -> list[tuple[str, float]]:
    from backend.app.alpaca_trader import AlpacaTrader
    from backend.app import broker_ledger as bl
    t = AlpacaTrader()
    try:
        by_day = await bl.fetch_realized(t, days_back=20)
    finally:
        await t.close()
    # fee-inclusive net, only days with actual fills
    return [(d, r["realized_net"]) for d, r in sorted(by_day.items()) if r.get("fills")]


async def main():
    print("EDGE CHECK — broker-truth daily P&L × Newey-West HAC t-stat × outlier jackknife")
    broker = await load_broker_days()
    analyze("A) BROKER-TRUTH daily net (fee-inclusive, all option fills — the canonical money)", broker)
    meic = load_meic_nights()
    analyze("B) MEIC ic_real_net per night (debrief_log — the '9 nights' reference series)", meic)
    print("\nNote: broker-truth (A) is the honest combined book; MEIC series (B) mixes "
          "real-fill and (older) model nights. Neither is gate-eligible until n≥20.")


if __name__ == "__main__":
    asyncio.run(main())
