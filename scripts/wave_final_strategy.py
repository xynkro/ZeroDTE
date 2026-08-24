#!/usr/bin/env python3
"""DECISIVE head-to-head: which 0DTE structure should the Wave bot run?

Applies the bear case's own falsifiable test (fattail.ai): "if win_rate x avg_win <
loss_rate x avg_loss, the strategy has negative EV". Their math assumes HOLDING to
expiry (max loss); ours uses a breach-stop, so we test it on real trade-level stats.
Reality-calibrated (0.5 model->executable haircut, measured on 7 live priced days).
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
from scripts.wave_volume_backtest import _try_side, L3, L5, L8, L10

MULT, COST, TP, HAIRCUT = 100, 5.0, 40.0, 0.5
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT


def run(slots, both=False, floor=100.0, cushion=0.4, wing=10.0, gate=True, hard_exit=None):
    bars = _load(); closes = [b[3] for b in bars]
    by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)
    rm = RunningMedians(); p = BandParams(bb_len=14, bb_mult=2.5)
    daily = defaultdict(float); trades = []; sessions = 0; peak_conc = 0
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20: continue
        sessions += 1; recorded = False; day_n = 0
        for slot in slots:
            eidx = next((i for i in idxs if _minute(bars[i][0]) >= slot), None)
            if eidx is None: continue
            pre = closes[idxs[0]:eidx + 1]
            if len(pre) < 5: continue
            r5 = bs.realized_5m_std(list(pre)); pr = _periods_remaining(bars[eidx][0])
            if r5 <= 0 or pr <= 1: continue
            buf = closes[:eidx + 1]
            sma, upper, lower, width = bollinger(buf, p.bb_len, p.bb_mult)
            S0 = closes[eidx]; med = rm.r5_median() if gate else None
            if not recorded: rm.record(r5, width); recorded = True
            if med is not None and r5 <= med: continue
            choppy = rm.width_median() is not None and width > rm.width_median()
            sides = (["sell_put_cs", "sell_call_cs"] if both
                     else ["sell_put_cs" if S0 < sma else "sell_call_cs"])
            for side in sides:
                got = _try_side(side, S0, sma, upper, lower, width, r5, pr, floor, cushion, wing, choppy)
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
                    if hard_exit and bm >= hard_exit: ev = bb; break
                    if bb <= tp_lvl or (down and c <= k) or ((not down) and c >= k): ev = bb; break
                if ev is None: ev = bs.spread_value(side, closes[idxs[-1]], k, lng, 0.0) * MULT
                pnl = (cr - ev) * HAIRCUT - COST
                daily[date] += pnl; trades.append(pnl); day_n += 1
        peak_conc = max(peak_conc, day_n)
    return daily, trades, sessions, peak_conc


def report(lab, daily, trades, sessions, peak):
    if len(trades) < 20: print(f"  {lab:34} too few"); return
    days = [v for k, v in sorted(daily.items()) if k >= OOS_START]
    w = [t for t in trades if t > 0]; l = [t for t in trades if t <= 0]
    wr = len(w) / len(trades); aw = st.mean(w) if w else 0; al = abs(st.mean(l)) if l else 0
    exp = wr * aw - (1 - wr) * al
    t = newey_west_tstat(days)["t"]
    verdict = "POSITIVE" if exp > 0 else "NEGATIVE"
    print(f"  {lab:34} {len(trades)/sessions:4.2f}/day | WR {100*wr:4.1f}% "
          f"avgW ${aw:5.0f} avgL ${al:5.0f} payoff {aw/al if al else 0:4.2f} | "
          f"EV/trade ${exp:+6.1f} {verdict} | ${st.mean(days):+6.0f}/d t={t:+5.2f} "
          f"worst ${min(days):+7.0f} | maxconc {peak}")


if __name__ == "__main__":
    print("BEAR-CASE TEST (fattail): is win_rate x avgWin > loss_rate x avgLoss?")
    print("Reality-calibrated. 'maxconc' = most concurrent trades in one day (risk!)\n")
    print("STRUCTURE:")
    report("A one-side 3 slots [Config B]", *run(L3))
    report("B BOTH sides 3 slots", *run(L3, both=True))
    report("C BOTH sides 5 slots", *run(L5, both=True))
    report("D BOTH sides 8 slots", *run(L8, both=True))
    report("E BOTH sides 10 slots", *run(L10, both=True))
    print("\nVOLUME PUSH (gate off = trade most days):")
    report("F BOTH 10 slots gate OFF", *run(L10, both=True, gate=False))
    report("G BOTH 10 slots gateOFF 7%", *run(L10, both=True, gate=False, floor=70.0))
    print("\nBEAR-CASE FIX — hard exit 14:00 (their rec #3):")
    report("H = E + exit 14:00", *run(L10, both=True, hard_exit=840))
    report("I = F + exit 14:00", *run(L10, both=True, gate=False, hard_exit=840))
