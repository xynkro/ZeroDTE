#!/usr/bin/env python3
"""Account-scale simulation: what does the MEIC ladder look like on a funded
Alpaca account (SPY, 1/10 SPX scale — Alpaca offers SPY options only, not SPX/XSP)?

It does three things, honestly separated:
  1. SIZING — given account $ and a peak-buying-power cap, how many contracts/slot.
  2. MODEL projection — scale the HONEST backtest's per-1-contract daily P&L
     distribution (BS-repriced, the good engine) to that contract count: mean/day,
     worst day, max drawdown, annualized, all as % of account.
  3. REALITY CHECK — the same scale applied to the REAL live fills so far. The
     backtest is the optimistic ceiling; the live nights are the only ground truth.
     Scaling amplifies whichever is true, and at N≈9 nights we don't know yet.

Run: PYTHONPATH=. .venv/bin/python scripts/account_sim.py [account_usd]
"""
from __future__ import annotations

import json
import sys
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401 — load .env
from backend.app.config import settings
from scripts.meic_backtest import run_meic

# --- structure (SPY 1/10 scale) ---
WING_SPY = settings.EOD_IC_WING_DOLLARS / 10.0       # $25 SPX → $2.5 SPY
MAX_VALUE = WING_SPY * 100                            # $250 per condor side
EST_CREDIT_SPY = 50.0                                 # ~real fill credit/condor (SPY)
MAX_LOSS = round(MAX_VALUE - EST_CREDIT_SPY)          # ≈ $200 defined risk / condor
SLOTS = len([s for s in settings.MEIC_ENTRY_TIMES_ET.split(",") if s.strip()])
TRADING_DAYS = 252


def per_day_spy_series():
    """Honest backtest per-day P&L at 1 contract/slot, SPY-scale (SPX/10)."""
    entries = run_meic([s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")],
                       cost_rt=50.0)
    by_day = defaultdict(float)
    for e in entries:
        by_day[e["date"]] += e["pnl"]
    return [v / 10.0 for v in by_day.values()]        # SPX → SPY 1ct


def real_meic_nights(path="backend/data/debrief_log.jsonl"):
    """Real per-night MEIC net from the rolling log (tiny N; mixed-Wave days are
    less reliable due to strike-merging — but it's the only live ground truth)."""
    out = []
    try:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if (r.get("ic_executed") or 0) > 0 and r.get("ic_real_net") is not None:
                out.append((r["date"], float(r["ic_real_net"])))
    except (OSError, ValueError):
        pass
    return out


def metrics(daily):
    """Risk/return metrics for a daily P&L series."""
    n = len(daily)
    mean = st.mean(daily)
    sd = st.stdev(daily) if n > 1 else 0.0
    # max drawdown on the cumulative equity curve
    cum = 0.0
    peak = 0.0
    mdd = 0.0
    for d in daily:
        cum += d
        peak = max(peak, cum)
        mdd = min(mdd, cum - peak)
    sharpe = (mean / sd * (TRADING_DAYS ** 0.5)) if sd > 0 else 0.0
    return {
        "mean": mean, "median": st.median(daily), "sd": sd,
        "worst": min(daily), "best": max(daily),
        "green": sum(1 for d in daily if d > 0) / n * 100,
        "annual": mean * TRADING_DAYS, "mdd": mdd, "sharpe": sharpe,
    }


def contracts_for(account, peak_bp_pct):
    """Max contracts/slot keeping peak buying power ≤ peak_bp_pct of account.
    Peak BP = SLOTS × N × MAX_LOSS (all condors open, defined risk held)."""
    bp_per_contract = SLOTS * MAX_LOSS
    return max(1, int((peak_bp_pct / 100.0 * account) // bp_per_contract))


def main():
    account = float(sys.argv[1]) if len(sys.argv) > 1 else 25000.0
    spy1 = per_day_spy_series()
    base = metrics(spy1)
    kill = settings.DAILY_LOSS_LIMIT_PCT / 100.0 * account

    print(f"=== ACCOUNT SIMULATION · ${account:,.0f} on Alpaca (SPY 1/10 scale) ===")
    print(f"  structure: {SLOTS}-slot ladder · ${WING_SPY:.1f} wing · ~${MAX_LOSS} max loss/condor"
          f" · peak BP/contract-ladder = ${SLOTS*MAX_LOSS}")
    print(f"  daily-loss kill-switch @ {settings.DAILY_LOSS_LIMIT_PCT:.0f}% = ${kill:,.0f}")
    print(f"  backtest (1 ct, SPY): mean ${base['mean']:+.1f}/day · worst ${base['worst']:+.0f} "
          f"· green {base['green']:.0f}% · {len(spy1)} days\n")

    print("  MODEL PROJECTION (scale the honest backtest):")
    print(f"  {'N/slot':>6} {'peakBP':>8} {'%acct':>6} {'$/day':>8} {'worst':>8} {'maxDD':>9} "
          f"{'annual':>9} {'%ret':>6} {'Sharpe':>7}  3-digit?")
    for N in (1, 3, 5, 8, 10):
        peak = SLOTS * N * MAX_LOSS
        m = metrics([d * N for d in spy1])
        three = "✅" if m["mean"] >= 100 else "—"
        kill_note = " ⚠️kill" if abs(m["worst"]) > kill else ""
        print(f"  {N:>6} {peak:>8,} {peak/account*100:>5.0f}% {m['mean']:>+8.0f} "
              f"{m['worst']:>+8.0f} {m['mdd']:>+9.0f} {m['annual']:>+9,.0f} "
              f"{m['annual']/account*100:>5.0f}% {m['sharpe']:>7.1f}  {three}{kill_note}")

    # recommended size at a sane 15% peak-BP cap
    Nrec = contracts_for(account, 15.0)
    mr = metrics([d * Nrec for d in spy1])
    print(f"\n  RECOMMENDED (≤15% peak BP): {Nrec} contracts/slot · "
          f"peak BP ${SLOTS*Nrec*MAX_LOSS:,} ({SLOTS*Nrec*MAX_LOSS/account*100:.0f}%) · "
          f"model ${mr['mean']:+.0f}/day · worst ${mr['worst']:+.0f} ({mr['worst']/account*100:.1f}%)")

    # REALITY CHECK — the live fills
    real = real_meic_nights()
    if real:
        rn = [v for _, v in real]
        clean = [v for d, v in real if d in ("2026-06-15", "2026-06-16", "2026-06-17", "2026-06-18")]
        rmean = st.mean(rn)
        print(f"\n  REALITY CHECK — live MEIC fills (N={len(rn)} nights, 1 ct):")
        print(f"    all nights:        mean ${rmean:+.1f}/night  (cum ${sum(rn):+.0f})")
        if clean:
            print(f"    clean MEIC-only:   mean ${st.mean(clean):+.1f}/night  "
                  f"(Jun15-18, no Wave collision — broker-verified)")
        print(f"    backtest model:    mean ${base['mean']:+.1f}/night")
        print(f"    → at {Nrec} contracts, REAL run-rate ≈ ${rmean*Nrec:+.0f}/day "
              f"vs MODEL ${mr['mean']:+.0f}/day. Live is running "
              f"{'BELOW' if rmean < base['mean'] else 'at/above'} model; N is far too small to trust either.")

    print("\n  Honest read: the DOWNSIDE scales with certainty (worst-day & maxDD are real $ at "
          f"${account:,.0f}); the UPSIDE is a model the live fills have NOT yet confirmed (~breakeven "
          "over ~9 nights). 3-digit/day is structurally reachable here, but it would be scaling an "
          "edge that is still unproven — exactly the council's warning.")


if __name__ == "__main__":
    main()
