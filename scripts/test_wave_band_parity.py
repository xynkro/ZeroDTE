#!/usr/bin/env python3
"""Parity test: the LIVE decision fn (wave_band_live.decide_band_trade) must reproduce
the validated backtest (wave_band_backtest.main, regime='released', bb 14/2.5) to the cent.

Routes ENTRIES through the live function but uses the backtest's own data + exit loop, so a
match proves live == validated spec. Run with cushion_pct=0 to match the headline config
(the cushion filter is an ADD-ON the backtest main() doesn't apply).

Run: cd ~/Documents/Trading/ZeroDTE-Wave && PYTHONPATH=. .venv/bin/python scripts/test_wave_band_parity.py
"""
from __future__ import annotations

import statistics as st

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining
from backend.app.wave_band_live import decide_band_trade, RunningMedians, BandParams
from scripts.wave_band_backtest import (
    _load, _minute, ENTRY_MIN, ET, OOS_START, score, main as bt_main,
)
from collections import defaultdict

MULT = 100
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
COST = 5.0
TP = 40.0


def run_live(cushion_pct=0.0):
    bars = _load()
    closes = [b[3] for b in bars]
    by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)
    rm = RunningMedians()
    p = BandParams(bb_len=14, bb_mult=2.5, wing=10.0, credit_floor=30.0,
                   choppy_push_atr=1.5, min_otm_pct=0.20, cushion_pct=cushion_pct,
                   premium_mult=PM, put_skew=PUT_SK, call_skew=CALL_SK)
    daily = {}
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20:
            continue
        eidx = next((i for i in idxs if _minute(bars[i][0]) >= ENTRY_MIN), None)
        if eidx is None:
            continue
        pre = closes[idxs[0]:eidx + 1]
        if len(pre) < 5:
            continue
        r5 = bs.realized_5m_std(list(pre))
        pr0 = _periods_remaining(bars[eidx][0])
        buf = closes[:eidx + 1]                       # continuous series through entry
        d = decide_band_trade(buf, r5, pr0, rm.r5_median(), rm.width_median(), p)
        # record band-width history every qualifying day (mirror backtest, no lookahead)
        from backend.app.wave_band_live import bollinger
        _, _, _, width = bollinger(buf, p.bb_len, p.bb_mult)
        if r5 > 0:
            rm.record(r5, width)
        if d is None:
            continue
        # ── identical exit loop to the backtest ──
        short, lng, side = d.short_strike, d.long_strike, d.side
        downtrend = side == "sell_put_cs"
        sk = PUT_SK if downtrend else CALL_SK
        credit = d.est_credit
        tp_level = credit * (1 - TP / 100.0)
        ev = None
        for i in idxs:
            if i <= eidx:
                continue
            bm = _minute(bars[i][0]); c = closes[i]
            if bm >= 16 * 60:
                ev = bs.spread_value(side, c, short, lng, 0.0) * MULT; break
            tv = bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * sk
            bbv = bs.spread_value(side, c, short, lng, tv) * MULT
            if bbv <= tp_level:
                ev = bbv; break
            if (downtrend and c <= short) or ((not downtrend) and c >= short):
                ev = bbv; break
        if ev is None:
            ev = bs.spread_value(side, closes[idxs[-1]], short, lng, 0.0) * MULT
        daily[date] = daily.get(date, 0.0) + (credit - ev - COST)
    return daily


if __name__ == "__main__":
    print("PARITY: live decide_band_trade() vs validated backtest (released, BB 14/2.5)\n")
    bt, _ = bt_main(regime="released", bb_len=14, bb_mult=2.5)
    live = run_live(cushion_pct=0.0)
    # compare daily dicts to the cent
    keys = sorted(set(bt) | set(live))
    mism = [k for k in keys if round(bt.get(k, 0.0), 2) != round(live.get(k, 0.0), 2)]
    print(f"backtest trade-days: {len(bt)} | live trade-days: {len(live)} | mismatched days: {len(mism)}")
    if mism[:5]:
        for k in mism[:5]:
            print(f"   {k}: bt {bt.get(k, 0.0):+.2f} vs live {live.get(k, 0.0):+.2f}")
    bo, lo = score(bt, lo=OOS_START), score(live, lo=OOS_START)
    print(f"\nOOS backtest: n={bo['n']} mean${bo['mean']:+.2f} t={bo['t']:.2f} worst${bo['worst']:+.0f}")
    print(f"OOS live    : n={lo['n']} mean${lo['mean']:+.2f} t={lo['t']:.2f} worst${lo['worst']:+.0f}")
    ok = len(mism) == 0 and len(bt) == len(live)
    print("\n" + ("✅ PARITY EXACT — live == validated spec" if ok
                  else f"❌ MISMATCH on {len(mism)} day(s) — fix decide_band_trade before wiring"))
    # also show the cushion-filtered config (what WaveZero will actually run)
    cush = run_live(cushion_pct=0.5)
    co = score(cush, lo=OOS_START)
    print(f"\nWITH cushion 0.5%: OOS n={co['n']} mean${co['mean']:+.2f} t={co['t']:.2f} worst${co['worst']:+.0f} (expect ~+$54, fewer trades)")
