#!/usr/bin/env python3
"""ANCHOR-FLOOR research: when the Bollinger band collapses inside the cushion (dead
tape), anchor the short AT the cushion boundary instead of rejecting the slot.
Reality-calibrated (0.5 haircut). Reports ALL / OOS / per-year / LOW-VOL-only so a
one-afternoon 4x improvement has to survive out-of-sample before anyone believes it.
Run: PYTHONPATH=. .venv/bin/python scripts/wave_lowvol_backtest.py [--from YYYY-MM-DD]
"""
from __future__ import annotations
import sys, statistics as st
from collections import defaultdict
import backend.app.config  # noqa
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat
from backend.app.wave_band_live import RunningMedians, BandParams, bollinger
from scripts.wave_band_backtest import _load, _minute, OOS_START
from scripts.wave_volume_backtest import L10

MULT, COST, TP, HAIRCUT = 100, 5.0, 40.0, 0.5
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT


def try_side(side, S0, upper, lower, width, r5, pr, floor, cushion, wing, choppy, anchor_floor):
    down = side == "sell_put_cs"; sk = PUT_SK if down else CALL_SK
    tv = bs.total_vol_to_expiry(r5, pr, PM) * sk; atr = max(1.0, width / 4.0)
    cd = S0 * cushion / 100.0
    if down: k = float(round(lower - (1.5 * atr if choppy else 0))); sign = +1.0
    else:    k = float(round(upper + (1.5 * atr if choppy else 0))); sign = -1.0
    if anchor_floor:
        if down and k > S0 - cd:
            k = float(round((S0 - cd) / 5.0) * 5.0); k -= 5.0 if k > S0 - cd else 0.0
        elif (not down) and k < S0 + cd:
            k = float(round((S0 + cd) / 5.0) * 5.0); k += 5.0 if k < S0 + cd else 0.0
    def credit(kk):
        lng = kk - wing if down else kk + wing
        return bs.spread_value(side, S0, kk, lng, tv) * MULT, lng
    cr, lng = credit(k); tries = 0
    while cr < floor and tries < 60:
        nk = k + sign * 5.0
        if 100.0 * abs(S0 - nk) / S0 < cushion: break
        k = nk; cr, lng = credit(k); tries += 1
    if cr < floor or 100.0 * abs(S0 - k) / S0 < cushion: return None
    return side, k, lng, cr, sk, down


def run(slots, gate, cushion, anchor_floor, floor=100.0, wing=10.0, since=None):
    bars = _load(); closes = [b[3] for b in bars]; by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]; by_day[et.strftime("%Y-%m-%d")].append(i)
    rm = RunningMedians(); p = BandParams(bb_len=14, bb_mult=2.5)
    daily = defaultdict(float); trades = []; dayvol = {}
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20: continue
        recorded = False
        for slot in slots:
            eidx = next((i for i in idxs if _minute(bars[i][0]) >= slot), None)
            if eidx is None: continue
            pre = closes[idxs[0]:eidx + 1]
            if len(pre) < 5: continue
            r5 = bs.realized_5m_std(list(pre)); pr = _periods_remaining(bars[eidx][0])
            if r5 <= 0 or pr <= 1: continue
            buf = closes[:eidx + 1]; sma, upper, lower, width = bollinger(buf, p.bb_len, p.bb_mult); S0 = closes[eidx]
            med = rm.r5_median()
            if not recorded:
                dayvol[date] = (r5, med); rm.record(r5, width); recorded = True
            if since and date < since: continue
            if gate and med is not None and r5 <= med: continue
            choppy = rm.width_median() is not None and width > rm.width_median()
            for side in ("sell_put_cs", "sell_call_cs"):
                got = try_side(side, S0, upper, lower, width, r5, pr, floor, cushion, wing, choppy, anchor_floor)
                if not got: continue
                side, k, lng, cr, sk, down = got; tp_lvl = cr * (1 - TP / 100.0); ev = None
                for i in idxs:
                    if i <= eidx: continue
                    bm = _minute(bars[i][0]); c = closes[i]
                    if bm >= 16 * 60: ev = bs.spread_value(side, c, k, lng, 0.0) * MULT; break
                    tv2 = bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * sk
                    bb = bs.spread_value(side, c, k, lng, tv2) * MULT
                    if bb <= tp_lvl or (down and c <= k) or ((not down) and c >= k): ev = bb; break
                if ev is None: ev = bs.spread_value(side, closes[idxs[-1]], k, lng, 0.0) * MULT
                pnl = (cr - ev) * HAIRCUT - COST; daily[date] += pnl; trades.append((date, pnl))
    return daily, trades, dayvol


def report(lab, daily, trades, dayvol, days):
    days = set(days); n_s = len(days)
    tr = [p for d, p in trades if d in days]; dd = {d: v for d, v in daily.items() if d in days}
    if not tr or n_s == 0: print(f"  {lab:32} {n_s:4d} sess | 0 trades"); return
    w = [t for t in tr if t > 0]; l = [t for t in tr if t <= 0]; wr = len(w) / len(tr)
    ev = wr * st.mean(w) - (1 - wr) * (abs(st.mean(l)) if l else 0)
    series = [dd.get(d, 0.0) for d in sorted(days)]
    t = newey_west_tstat(series)["t"] if len(series) > 5 else float("nan")
    print(f"  {lab:32} {n_s:4d} sess | {len(tr)/n_s:5.2f} tr/sess fire {100*len(dd)/n_s:3.0f}% | "
          f"WR {100*wr:4.1f}% EV ${ev:+5.1f} | ${sum(dd.values())/n_s:+6.1f}/sess t={t:5.2f} | worst ${min(dd.values()):+6.0f}")


CFGS = [("E: gate ON, cush .4",            True,  0.4, False),
        ("gate OFF, cush .4",              False, 0.4, False),
        ("F4: gate OFF, cush .4, ANCHOR",  False, 0.4, True),
        ("F6: gate OFF, cush .6, ANCHOR",  False, 0.6, True),
        ("F8: gate OFF, cush .8, ANCHOR",  False, 0.8, True)]

if __name__ == "__main__":
    since = sys.argv[sys.argv.index("--from") + 1] if "--from" in sys.argv else None
    res = [(lab,) + run(L10, g, c, a, since=since) for lab, g, c, a in CFGS]
    alld = sorted(res[0][3].keys())
    if since: alld = [d for d in alld if d >= since]
    oos = [d for d in alld if d >= OOS_START]
    low = [d for d in alld if res[0][3][d][1] is not None and res[0][3][d][0] < 0.6 * res[0][3][d][1]]
    for title, days in (("ALL", alld), ("OUT-OF-SAMPLE (2024+)", oos), ("LOW-VOL ONLY (r5<0.6x median)", low)):
        print(f"\n=== {title}: {len(days)} sessions ===")
        for lab, d, t, v in res: report(lab, d, t, v, days)
    print("\n=== PER YEAR: $/session (F6 vs E) ===")
    for y in sorted({d[:4] for d in alld}):
        ys = [d for d in alld if d.startswith(y)]
        e = sum(res[0][1].get(d, 0) for d in ys) / len(ys); f6 = sum(res[3][1].get(d, 0) for d in ys) / len(ys)
        f6w = min([res[3][1].get(d, 0) for d in ys] or [0])
        print(f"  {y}: {len(ys):3d} sess | E ${e:+6.1f}/sess | F6 ${f6:+6.1f}/sess (worst ${f6w:+.0f})")
