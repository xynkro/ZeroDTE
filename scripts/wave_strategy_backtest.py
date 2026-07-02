#!/usr/bin/env python3
"""WAVE — backtest YOUR actual strategy, honestly.

The live WAVE bot ≠ what you described. You traded: stoch oversold → put spread,
overbought → call spread, take 25-50% profit then close (else hold). The bot rides
to 90% / expiry. This tests YOUR rule, faithfully:

  - REAL signal: predictor.run_backtest (the exact live stoch-cross + trend filter),
    not a proxy.
  - HONEST pricing: Black-Scholes reprice each bar (bs_pricing), NOT the
    run_wave_backtest proxy that assumes you hit your TP cleanly (which would flatter
    a TP strategy — the whole question here).
  - Each signal = one single-side spread, its own TP (your "check the put for 25-50%
    then close" = an independent per-leg target). Stop on close-through-short-strike.
  - IS pre-2024 / OOS 2024+; scored on Newey-West t-stat + worst-day + win%.

Grid: short delta ∈ {16, 20, 30} × take-profit ∈ {25, 50, 90}% of credit.
Absolute $ still OVERSTATE (the engine does); read OOS + relative ranking + tail.

Run: PYTHONPATH=. .venv/bin/python scripts/wave_strategy_backtest.py
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from datetime import datetime

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.predictor import Bar, run_backtest as predictor_run
from backend.app.backtest_api import _wilder_atr   # daily-ATR-map form predictor_run wants
from backend.app.quant_utils import newey_west_tstat

PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
WING = 25.0
MULT = 100
SPREAD_COST = 25.0
OOS_START = "2024-01-01"
TIME_STOP_MIN = 16 * 60 - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE   # 30m before close


def _load_bars():
    path = settings.data_dir / "historical" / "SPX_5m_3y.json"
    raw = json.loads(path.read_text())
    return [Bar(time=datetime.fromisoformat(r["datetime"]), open=r["open"], high=r["high"],
                low=r["low"], close=r["close"], volume=r.get("volume", 0) or 0) for r in raw]


def _minute(t):
    et = t.astimezone(ET) if t.tzinfo else t
    return et.hour * 60 + et.minute


def _spread_trade(day_bars, sig, delta, tp_pct, exit_by_min=TIME_STOP_MIN):
    """Honest single-side credit-spread P&L for ONE signal with YOUR TP rule.
    exit_by_min = hard time exit (minute-of-day ET); earlier = less afternoon risk."""
    # bars at/after the signal time, same session
    post = [b for b in day_bars if b.time >= sig.time]
    pre = [b.close for b in day_bars if b.time <= sig.time]
    if len(post) < 2 or len(pre) < 5:
        return None
    r5 = bs.realized_5m_std(pre)
    if r5 <= 0:
        return None
    eb = post[0]
    pr0 = _periods_remaining(eb.time)
    if pr0 <= 1:
        return None
    S0 = eb.close
    side = sig.side
    sk = CALL_SK if side == "sell_call_cs" else PUT_SK
    tv0 = bs.total_vol_to_expiry(r5, pr0, PM) * sk
    if side == "sell_call_cs":
        cs = bs.strike_for_call_delta(S0, tv0, delta); cl = cs + WING
    else:
        cs = bs.strike_for_put_delta(S0, tv0, delta); cl = cs - WING
    credit = bs.spread_value(side, S0, cs, cl, tv0) * MULT
    if credit <= 0:
        return None
    tp_level = credit * (1 - tp_pct / 100.0)     # buyback ≤ this ⇒ captured tp_pct%
    exit_val = None
    for b in post[1:]:
        bm = _minute(b.time)
        if bm >= 16 * 60:
            exit_val = bs.spread_value(side, b.close, cs, cl, 0.0) * MULT; break
        # stop: price closed THROUGH the short strike (the live rule)
        through = (side == "sell_call_cs" and b.close >= cs) or (side == "sell_put_cs" and b.close <= cs)
        tv = bs.total_vol_to_expiry(r5, _periods_remaining(b.time), PM) * sk
        bb = bs.spread_value(side, b.close, cs, cl, tv) * MULT
        if bb <= tp_level:                        # YOUR take-profit
            exit_val = bb; break
        if through:                               # stop
            exit_val = bb; break
        if bm >= exit_by_min:                      # hard time exit (the lever)
            exit_val = bb; break
    if exit_val is None:
        exit_val = bs.spread_value(side, post[-1].close, cs, cl, 0.0) * MULT
    return credit - exit_val - SPREAD_COST


def _calm_map(by_day):
    """Classify each day calm/choppy by early-session (first hour) realized 5m vol vs
    its EXPANDING median (no lookahead). calm = below median = sit IN; else sit OUT."""
    out, hist = {}, []
    for d in sorted(by_day):
        bars = by_day[d]
        early = [b.close for b in bars if _minute(b.time) <= 10 * 60 + 30]
        rv = bs.realized_5m_std(early) if len(early) >= 5 else None
        med = st.median(hist) if len(hist) >= 20 else None
        out[d] = True if (med is None or rv is None) else (rv < med)   # default trade until median forms
        if rv is not None:
            hist.append(rv)
    return out


def run(sessions, by_day, delta, tp_pct, calm=None, calm_only=False, exit_by_min=TIME_STOP_MIN):
    daily = defaultdict(float)
    n_trades = 0
    for s in sessions:
        if not s.signals:
            continue
        if calm_only and calm is not None and not calm.get(s.session_date, True):
            continue   # choppy day — sit it out
        db = by_day.get(s.session_date)
        if not db:
            continue
        for sig in s.signals:
            # don't open a trade with <30 min to run before the hard exit
            if _minute(sig.time) >= exit_by_min - 30:
                continue
            p = _spread_trade(db, sig, delta, tp_pct, exit_by_min)
            if p is not None:
                daily[s.session_date] += p
                n_trades += 1
    return daily, n_trades


def score(daily, lo=None, hi=None):
    days = [v for d, v in daily.items() if (lo is None or d >= lo) and (hi is None or d < hi)]
    if len(days) < 2:
        return None
    nw = newey_west_tstat(days)
    return {"n": len(days), "mean": st.mean(days), "t": nw["t"],
            "worst": min(days), "green": sum(1 for v in days if v > 0) / len(days) * 100}


def _f(s):
    return (f"n={s['n']:>4} mean${s['mean']:+6.0f} t={s['t']:+5.2f} "
            f"worst${s['worst']:+6.0f} green{s['green']:>3.0f}%") if s else "(none)"


if __name__ == "__main__":
    print("Loading 3y SPX 5m + generating REAL stoch signals…")
    bars = _load_bars()
    atr_map = _wilder_atr(bars, 14)
    sessions = predictor_run(bars, lambda d: atr_map.get(d))
    by_day = defaultdict(list)
    for b in bars:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        by_day[et.strftime("%Y-%m-%d")].append(b)
    total_sig = sum(len(s.signals) for s in sessions)
    print(f"{len(sessions)} sessions · {total_sig} real signals · IS pre-{OOS_START} / OOS {OOS_START}+\n")
    print("YOUR strategy (stoch cross → single-side spread, per-leg take-profit):\n")
    print(f"{'delta':>6} {'TP%':>4} | {'OUT-OF-SAMPLE':^42} | {'(in-sample)':^30}")
    print("  " + "-" * 88)
    rows = []
    for delta in (0.16, 0.20, 0.30):
        for tp in (25, 50, 90):
            daily, nt = run(sessions, by_day, delta, tp)
            oos, is_ = score(daily, lo=OOS_START), score(daily, hi=OOS_START)
            rows.append((delta, tp, is_, oos))
            print(f"{delta*100:>5.0f}Δ {tp:>3}% | {_f(oos):<42} | {_f(is_)}")
        print()
    # ── SIT OUT CHOPPY DAYS — your idea: trade only calm-open days ──────────
    calm = _calm_map(by_day)
    print("=" * 90)
    print("SIT OUT CHOPPY DAYS — trade only calm-open days (early-RV below running median):\n")
    print(f"{'delta':>6} {'TP%':>4} {'days':>10} | {'OUT-OF-SAMPLE':^42}")
    print("  " + "-" * 76)
    for delta in (0.20,):
        for tp in (50, 90):
            d_all, _ = run(sessions, by_day, delta, tp)
            d_calm, _ = run(sessions, by_day, delta, tp, calm=calm, calm_only=True)
            o_all, o_calm = score(d_all, lo=OOS_START), score(d_calm, lo=OOS_START)
            print(f"{delta*100:>5.0f}Δ {tp:>3}% {'ALL':>10} | {_f(o_all)}")
            print(f"{'':>6} {'':>4} {'CALM-only':>10} | {_f(o_calm)}")
            print()
    print("=" * 90)
    print("Read: calm-OPEN filter did little for WAVE — its blowups are AFTERNOON moves on")
    print("days that look calm at 10:30. The tail lever is exit SPEED, not day-selection.\n")

    # ── HARD AFTERNOON EXIT — your idea: close everything by ~1:30-2:30pm ───
    print("=" * 90)
    print("HARD AFTERNOON EXIT — 20Δ / TP50, close ALL by the given ET time (cut afternoon risk):\n")
    print(f"{'exit by':>9} {'days':>5} | {'OUT-OF-SAMPLE':^42} | {'(in-sample)':^30}")
    print("  " + "-" * 86)
    for hh, mm in ((13, 0), (13, 30), (14, 0), (14, 30), (15, 0), (15, 50)):
        ebm = hh * 60 + mm
        d, nt = run(sessions, by_day, 0.20, 50, exit_by_min=ebm)
        oos, is_ = score(d, lo=OOS_START), score(d, hi=OOS_START)
        tag = "  (baseline)" if (hh, mm) == (15, 50) else ""
        print(f"{hh:02d}:{mm:02d} ET  {len(d):>4} | {_f(oos):<42} | {_f(is_)}{tag}")
    print("\n" + "=" * 90)
    print("Read: if an earlier hard exit SHRINKS the worst-day vs the 15:50 baseline, the")
    print("tail really is afternoon-made and exit-speed is the lever. Watch worst-day, not")
    print("mean (mean overstates). Nothing is real until clean broker-truth confirms it.")
    print("\nNote: 90% TP ≈ the current bot; 25-50% ≈ what you actually traded.")
