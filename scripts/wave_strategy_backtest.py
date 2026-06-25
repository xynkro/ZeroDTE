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


def _spread_trade(day_bars, sig, delta, tp_pct):
    """Honest single-side credit-spread P&L for ONE signal with YOUR TP rule."""
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
        if bm >= TIME_STOP_MIN:                    # time exit
            exit_val = bb; break
    if exit_val is None:
        exit_val = bs.spread_value(side, post[-1].close, cs, cl, 0.0) * MULT
    return credit - exit_val - SPREAD_COST


def run(sessions, by_day, delta, tp_pct):
    daily = defaultdict(float)
    n_trades = 0
    for s in sessions:
        if not s.signals:
            continue
        db = by_day.get(s.session_date)
        if not db:
            continue
        for sig in s.signals:
            p = _spread_trade(db, sig, delta, tp_pct)
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
    # best by OOS mean among positive-OOS
    cand = [r for r in rows if r[3] and r[3]["mean"] > 0]
    cand.sort(key=lambda r: r[3]["t"], reverse=True)
    print("=" * 90)
    if cand:
        d, tp, _, oos = cand[0]
        print(f"BEST OOS by t-stat: {d*100:.0f}Δ / TP{tp}%  →  {_f(oos)}")
        print("…but remember: absolute $ overstate (engine). Judge the RANKING + tail,")
        print("and nothing is real until it survives broker-truth on clean live fills.")
    else:
        print("NO positive-OOS config. Your strategy, honestly priced, has no edge in 3y SPX.")
    print("\nNote: 90% TP ≈ the current bot; 25-50% ≈ what you actually traded.")
