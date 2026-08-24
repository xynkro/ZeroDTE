#!/usr/bin/env python3
"""VOLUME research — can we get 4-5 trades A NIGHT (not a fortnight)?

Levers, tested honestly against the same reality-calibrated engine (0.5 haircut):
  L1 BOTH SIDES   - sell put AND call at each slot when each independently clears the
                    floor (this is what makes MEIC a 4-a-night book: it's a condor)
  L2 DENSE LADDER - up to 10 slots (every 30 min 10:00-15:00)
  L3 FLOOR        - the binding constraint. 10% of width is the risk-owner rule;
                    what does 7% / 5% buy in volume and cost in expectancy?
Reports trades/DAY (the number Caspar cares about) + expectancy + tail.

Run: PYTHONPATH=. .venv/bin/python scripts/wave_volume_backtest.py
"""
from __future__ import annotations
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat
from backend.app.wave_band_live import RunningMedians, BandParams, bollinger
from scripts.wave_band_backtest import _load, _minute, OOS_START

MULT = 100
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
COST, TP, HAIRCUT = 5.0, 40.0, 0.5


def _try_side(side, S0, sma, upper, lower, width, r5, pr, floor_spx, cushion, wing, choppy):
    """Anchor at this side's band, walk toward spot to the floor, enforce cushion."""
    down = side == "sell_put_cs"
    sk = PUT_SK if down else CALL_SK
    tv = bs.total_vol_to_expiry(r5, pr, PM) * sk
    atr = max(1.0, width / 4.0)
    if down:
        k = float(round(lower - (1.5 * atr if choppy else 0.0))); sign = +1.0
    else:
        k = float(round(upper + (1.5 * atr if choppy else 0.0))); sign = -1.0
    def credit(kk):
        lng = kk - wing if down else kk + wing
        return bs.spread_value(side, S0, kk, lng, tv) * MULT, lng
    cr, lng = credit(k); tries = 0
    while cr < floor_spx and tries < 60:
        nk = k + sign * 5.0
        if 100.0 * abs(S0 - nk) / S0 < cushion:   # never inside the cushion
            break
        k = nk; cr, lng = credit(k); tries += 1
    if cr < floor_spx or 100.0 * abs(S0 - k) / S0 < cushion:
        return None
    return side, k, lng, cr, sk, down


def run(slots, both_sides=False, floor_spx=100.0, cushion=0.4, wing=10.0, vol_gate=True):
    bars = _load(); closes = [b[3] for b in bars]
    by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)
    rm = RunningMedians(); p = BandParams(bb_len=14, bb_mult=2.5)
    daily = defaultdict(float); n = 0; sessions = 0
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20: continue
        sessions += 1; recorded = False
        for slot in slots:
            eidx = next((i for i in idxs if _minute(bars[i][0]) >= slot), None)
            if eidx is None: continue
            pre = closes[idxs[0]:eidx + 1]
            if len(pre) < 5: continue
            r5 = bs.realized_5m_std(list(pre))
            pr = _periods_remaining(bars[eidx][0])
            if r5 <= 0 or pr <= 1: continue
            buf = closes[:eidx + 1]
            sma, upper, lower, width = bollinger(buf, p.bb_len, p.bb_mult)
            S0 = closes[eidx]
            med = rm.r5_median() if vol_gate else None
            if not recorded:
                rm.record(r5, width); recorded = True
            if med is not None and r5 <= med: continue      # Schwartz gate
            choppy = rm.width_median() is not None and width > rm.width_median()
            sides = (["sell_put_cs", "sell_call_cs"] if both_sides
                     else ["sell_put_cs" if S0 < sma else "sell_call_cs"])
            for side in sides:
                got = _try_side(side, S0, sma, upper, lower, width, r5, pr,
                                floor_spx, cushion, wing, choppy)
                if not got: continue
                side, k, lng, cr, sk, down = got
                tp_lvl = cr * (1 - TP / 100.0); ev = None
                for i in idxs:
                    if i <= eidx: continue
                    bm = _minute(bars[i][0]); c = closes[i]
                    if bm >= 16 * 60:
                        ev = bs.spread_value(side, c, k, lng, 0.0) * MULT; break
                    tv2 = bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * sk
                    bb = bs.spread_value(side, c, k, lng, tv2) * MULT
                    if bb <= tp_lvl or (down and c <= k) or ((not down) and c >= k):
                        ev = bb; break
                if ev is None:
                    ev = bs.spread_value(side, closes[idxs[-1]], k, lng, 0.0) * MULT
                daily[date] += (cr - ev) * HAIRCUT - COST; n += 1
    return daily, n, sessions


def show(lab, daily, n, sessions):
    days = [v for k, v in sorted(daily.items()) if k >= OOS_START]
    alld = list(daily.values())
    if len(days) < 3: print(f"  {lab:44} too few"); return
    t = newey_west_tstat(days)["t"]
    print(f"  {lab:44} {n/sessions:4.2f} trades/DAY | OOS ${st.mean(days):+6.1f}/d "
          f"t={t:+5.2f} worst=${min(days):+7.0f} | total ${sum(alld):+8.0f}")


L3 = (600, 660, 720)
L5 = (600, 645, 690, 735, 780)
L8 = tuple(600 + 30 * i for i in range(8))     # 10:00-13:30 every 30m
L10 = tuple(600 + 30 * i for i in range(10))   # 10:00-14:30 every 30m

if __name__ == "__main__":
    print("GOAL: 4-5 trades/DAY. (Config B = 0.39/day.) floor 10% width unless stated\n")
    print("L1 — BOTH SIDES (the condor lever):")
    show("Config B: 3 slots, one side", *run(L3))
    show("3 slots, BOTH sides", *run(L3, both_sides=True))
    show("5 slots, BOTH sides", *run(L5, both_sides=True))
    show("8 slots, BOTH sides", *run(L8, both_sides=True))
    show("10 slots, BOTH sides", *run(L10, both_sides=True))
    print("\nL3 — FLOOR (10 slots, both sides) — the binding constraint:")
    for f, lab in ((100.0, "10% width [risk-owner rule]"), (70.0, "7% width"), (50.0, "5% width")):
        show(f"floor {lab}", *run(L10, both_sides=True, floor_spx=f))
    print("\nL3b — no vol gate + looser floor (max volume):")
    show("10 slots, both, 7% floor, gate OFF", *run(L10, both_sides=True, floor_spx=70.0, vol_gate=False))
    show("10 slots, both, 5% floor, gate OFF", *run(L10, both_sides=True, floor_spx=50.0, vol_gate=False))
