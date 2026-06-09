#!/usr/bin/env python3
"""VIX-gate validation — does the VIX>=22 stand-aside line hurt the PUT book?

The directional VIX gate reused the IC builder's stand-aside threshold (VIX>=22),
calibrated on a recent low-vol window. But the put edge is strongest in elevated
fear. This tags every honest-backtest trade with its prior-day VIX (lookahead-safe)
and splits the put/call books by VIX bucket.

Finding (153-trade backtest, skew on): VIX 22-30 is the put book's BEST regime
(+$128/trade, 83% WR, 11% breach); only VIX>=30 is net-negative (50% breach). So
the right gate is VIX>=30, not 22 — see config.WAVE_VIX_STANDASIDE.

Run: PYTHONPATH=. .venv/bin/python scripts/vix_gate_validation.py
"""
from __future__ import annotations

import math
import statistics as st

import yfinance as yf

import backend.app.config  # noqa: F401 — loads .env
from backend.app.honest_backtest import run_honest_backtest


def _vix_by_prior_close(d0: str, d1: str) -> dict:
    """Map each trading day → the PRIOR day's VIX close (known at the open)."""
    vx = yf.Ticker("^VIX").history(start="2022-01-01", end="2026-06-15")
    close = {ts.strftime("%Y-%m-%d"): float(c) for ts, c in vx["Close"].items()}
    days = sorted(close)
    return {days[i]: close[days[i - 1]] for i in range(1, len(days))}


def _stat(ts: list[dict]) -> str:
    ps = [t["pnl"] for t in ts]
    n = len(ps)
    if not n:
        return f"{'—':>30}"
    w = sum(1 for p in ps if p > 0)
    br = sum(1 for t in ts if t["outcome"] in ("breach", "stop_loss"))
    mean = st.mean(ps)
    sd = st.pstdev(ps) if n > 1 else 0
    tt = mean / (sd / math.sqrt(n)) if sd > 0 else 0
    return f"n={n:<3} tot={round(sum(ps)):>+6} WR={round(w / n * 100):>3}% br={round(br / n * 100):>3}% mean={mean:>+6.0f} t={tt:>5.2f}"


def main() -> None:
    r = run_honest_backtest(target_delta=30, final_tp_target=90.0, use_dynamic_stops=False,
                            data_window="max", return_trades=True, skew_enabled=True)
    trades = r["trades"]
    d0, d1 = min(t["date"][:10] for t in trades), max(t["date"][:10] for t in trades)
    vix = _vix_by_prior_close(d0, d1)
    def vix_of(t):
        return vix.get(t["date"][:10])

    print(f"{len(trades)} trades, {d0}..{d1}, skew on, VIX = prior-day close\n")
    for name, key in [("PUT", "sell_put_cs"), ("CALL", "sell_call_cs")]:
        sub = [t for t in trades if t["side"] == key and vix_of(t) is not None]
        print(f"=== {name} BOOK ===")
        for lbl, lo, hi in [("VIX <15", 0, 15), ("VIX 15-22", 15, 22),
                            ("VIX 22-30", 22, 30), ("VIX >=30", 30, 999)]:
            print(f"  {lbl:<10} {_stat([t for t in sub if lo <= vix_of(t) < hi])}")
        print(f"  TRADES (<22)  {_stat([t for t in sub if vix_of(t) < 22])}")
        print(f"  GATED  (>=22) {_stat([t for t in sub if vix_of(t) >= 22])}  <- old gate stood these aside\n")


if __name__ == "__main__":
    main()
