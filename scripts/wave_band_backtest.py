#!/usr/bin/env python3
"""WAVE — Caspar's REAL strategy, honestly backtested (v1).

His method (as described 2026-06-25): ~10:00 ET, read trend; SELL the premium at the
projected Bollinger-band extreme on the trend side (put at lower band in a downtrend,
call at upper band in an uptrend); push FURTHER OTM on choppy days; WALK the strike
toward spot until the credit clears a floor; ~$1,000 risk per position; take 30-50%
profit (here: TP40%) else stop on breach; ride to expiry otherwise.

Honest engine: real SPX 5m 3y data + Black-Scholes repricing (bs_pricing). Choppy =
Bollinger-band WIDTH above its rolling median (the same bands he trades off — and
backtestable, unlike GEX which has no history). IS pre-2024 / OOS 2024+; Newey-West t.

MODELING NOTES (v1 approximations, flag for refinement):
  - Kalman white line ≈ the Bollinger band (Kalman refinement later).
  - Direction = price vs 20-bar mid-band (proxy for his RSI/BB/Stoch/Kalman read).
  - $1,000 risk = 10-pt SPX wing × 1 ct. Strike walks toward spot until credit≥floor.
  - Exit TP40% / breach-stop / expiry (proxy for "reverse + 30-50%, else stop").
ALL absolute $ OVERSTATE (engine) — read OOS ranking + worst-day; confirm clean-live.

Run: PYTHONPATH=. .venv/bin/python scripts/wave_band_backtest.py
"""
from __future__ import annotations

import json
import statistics as st
from collections import defaultdict
from datetime import datetime

import numpy as np

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat

MULT = 100
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
OOS_START = "2024-01-01"
BB_LEN = 20
ENTRY_MIN = 10 * 60          # ~10:00 ET (30 min after open)
COST = 5.0                   # commissions per spread RT


def _load():
    path = settings.data_dir / "historical" / "SPX_5m_3y.json"
    raw = json.loads(path.read_text())
    return [(datetime.fromisoformat(r["datetime"]), r["high"], r["low"], r["close"]) for r in raw]


def _minute(t):
    et = t.astimezone(ET) if t.tzinfo else t
    return et.hour * 60 + et.minute


def main(wing=10.0, credit_floor=30.0, tp_pct=40.0, choppy_push_atr=1.5,
         min_otm_pct=0.20, risk_label="$1,000 (10-wide SPX ×1ct)"):
    bars = _load()
    closes = np.array([b[3] for b in bars], float)
    # rolling Bollinger(20,2) over the continuous 5m series
    sma = np.convolve(closes, np.ones(BB_LEN) / BB_LEN, mode="full")[:len(closes)]
    sma[:BB_LEN] = closes[:BB_LEN]
    std = np.array([closes[max(0, i - BB_LEN + 1):i + 1].std() for i in range(len(closes))])
    upper, lower = sma + 2 * std, sma - 2 * std
    width = (upper - lower)
    # group bar indices by ET day
    by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)

    # rolling median band-width for the "choppy" classifier (expanding, no lookahead)
    daily = {}
    skipped = 0
    width_hist = []
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20:
            continue
        # entry = first bar at/after 10:00
        eidx = next((i for i in idxs if _minute(bars[i][0]) >= ENTRY_MIN), None)
        if eidx is None:
            continue
        S0 = closes[eidx]
        day_width = width[eidx]
        med_w = st.median(width_hist) if len(width_hist) >= 20 else None
        width_hist.append(day_width)
        choppy = (med_w is not None and day_width > med_w)
        atr = max(1.0, day_width / 4.0)   # rough intraday ATR proxy from band width

        pre = closes[idxs[0]:eidx + 1]
        if len(pre) < 5:
            continue
        r5 = bs.realized_5m_std(list(pre))
        if r5 <= 0:
            continue
        pr0 = _periods_remaining(bars[eidx][0])
        if pr0 <= 1:
            continue
        downtrend = S0 < sma[eidx]
        side = "sell_put_cs" if downtrend else "sell_call_cs"
        sk = PUT_SK if downtrend else CALL_SK
        tv0 = bs.total_vol_to_expiry(r5, pr0, PM) * sk

        # base strike = the projected band extreme; push further OTM on choppy days
        if downtrend:
            short = lower[eidx] - (choppy_push_atr * atr if choppy else 0.0)
            short = float(round(short))
            step, sign = 5.0, +1.0   # walk UP toward spot for more credit
            cap = S0 * (1 - min_otm_pct / 100.0)
        else:
            short = upper[eidx] + (choppy_push_atr * atr if choppy else 0.0)
            short = float(round(short))
            step, sign = 5.0, -1.0   # walk DOWN toward spot
            cap = S0 * (1 + min_otm_pct / 100.0)

        def credit_at(s):
            lng = s - wing if downtrend else s + wing
            return bs.spread_value(side, S0, s, lng, tv0) * MULT, lng

        cr, lng = credit_at(short)
        tries = 0
        while cr < credit_floor and tries < 60:
            ns = short + sign * step
            if (downtrend and ns > cap) or ((not downtrend) and ns < cap):
                break    # would get too close to spot — stop walking
            short = ns
            cr, lng = credit_at(short)
            tries += 1
        if cr < credit_floor:
            skipped += 1
            continue   # can't get paid enough without taking on too much risk — skip

        credit = cr
        tp_level = credit * (1 - tp_pct / 100.0)
        exit_val = None
        for i in idxs:
            if i <= eidx:
                continue
            bm = _minute(bars[i][0])
            c = closes[i]
            if bm >= 16 * 60:
                exit_val = bs.spread_value(side, c, short, lng, 0.0) * MULT
                break
            through = (downtrend and c <= short) or ((not downtrend) and c >= short)
            tv = bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * sk
            bb = bs.spread_value(side, c, short, lng, tv) * MULT
            if bb <= tp_level:                 # take 30-50%
                exit_val = bb; break
            if through:                        # breach stop
                exit_val = bb; break
        if exit_val is None:
            exit_val = bs.spread_value(side, closes[idxs[-1]], short, lng, 0.0) * MULT
        daily[date] = daily.get(date, 0.0) + (credit - exit_val - COST)

    return daily, skipped


def score(daily, lo=None, hi=None):
    days = [v for d, v in daily.items() if (lo is None or d >= lo) and (hi is None or d < hi)]
    if len(days) < 2:
        return None
    nw = newey_west_tstat(days)
    return {"n": len(days), "mean": st.mean(days), "t": nw["t"],
            "worst": min(days), "best": max(days),
            "green": sum(1 for v in days if v > 0) / len(days) * 100}


def _f(s):
    return (f"n={s['n']:>4} mean${s['mean']:+6.0f} t={s['t']:+5.2f} "
            f"worst${s['worst']:+6.0f} best${s['best']:+6.0f} green{s['green']:>3.0f}%") if s else "(none)"


if __name__ == "__main__":
    print("WAVE — your real strategy (band-anchored strikes, ~$1,000 risk), honest BS, 3y SPX\n")
    for wing, label in ((10.0, "$1,000 risk (10-wide ×1ct)"), (25.0, "$2,500 risk (25-wide)"),
                        (50.0, "$5,000 risk (50-wide)")):
        daily, skipped = main(wing=wing)
        oos, is_ = score(daily, lo=OOS_START), score(daily, hi=OOS_START)
        print(f"WING {wing:.0f}pt — {label} · traded {len(daily)} days · skipped {skipped} (thin credit)")
        print(f"   OOS: {_f(oos)}")
        print(f"   IS : {_f(is_)}\n")
    print("=" * 86)
    print("Read: this is the FIRST honest test of YOUR method — band-anchored strikes, choppy")
    print("(band-width) push-out, $30 credit floor, TP40 + breach-stop. Watch OOS worst-day vs")
    print("the −$4k strawman: wider/further-OTM = thinner tail. Absolute $ overstate; clean-live decides.")
    print("Note: 'skipped' = days the band strike couldn't clear $30 without getting too close to spot.")
