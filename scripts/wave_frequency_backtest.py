#!/usr/bin/env python3
"""FREQUENCY research — how do we get enough valid trades to validate in WEEKS not months?

Calibrated to reality: live data (7 priced days, Jul-Aug 2026) shows the flat-IV model
overstates EXECUTABLE credit by ~2x (model $26.7/ct-equiv -> real $12; model $15.7 -> real
$7-9). So we apply HAIRCUT to both the floor test and the P&L. A backtest that ignores this
predicts a 40% fire rate; reality delivered 13%.

Theories tested:
  T1 ENTRY LADDER   - multiple independent entries/day (the biggest frequency lever)
  T2 WING WIDTH     - wider wings = more $ credit; does %-of-width improve?
  T3 VOL GATE       - it rejects 57% of days live. What does relaxing it cost?
  T4 CUSHION        - 0.5% is the validated filter; what does 0.4/0.6 do to freq vs tail?

Run: PYTHONPATH=. .venv/bin/python scripts/wave_frequency_backtest.py
"""
from __future__ import annotations
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat
from backend.app.wave_band_live import decide_band_trade, RunningMedians, BandParams, bollinger
from scripts.wave_band_backtest import _load, _minute, OOS_START

MULT = 100
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
COST = 5.0
TP = 40.0
HAIRCUT = 0.5          # model -> executable credit (calibrated on 7 live priced days)
FLOOR_REAL_SPX = 100.0 # $10/ct SPY = $100 SPX/ct = 10% of the $1,000 wing (risk-owner rule)


def run(slots=(600,), wing=10.0, cushion=0.5, use_vol_gate=True, floor_real=FLOOR_REAL_SPX):
    bars = _load()
    closes = [b[3] for b in bars]
    by_day = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)
    rm = RunningMedians()
    # model floor = real floor grossed up by the haircut (require model >= 2x real target)
    p = BandParams(bb_len=14, bb_mult=2.5, wing=wing, credit_floor=floor_real / HAIRCUT,
                   cushion_pct=cushion, premium_mult=PM, put_skew=PUT_SK, call_skew=CALL_SK)
    daily = defaultdict(float)
    n_trades = 0
    sessions = 0
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20:
            continue
        sessions += 1
        # per-day vol stats recorded once (mirrors live)
        recorded = False
        for slot in slots:
            eidx = next((i for i in idxs if _minute(bars[i][0]) >= slot), None)
            if eidx is None:
                continue
            pre = closes[idxs[0]:eidx + 1]
            if len(pre) < 5:
                continue
            r5 = bs.realized_5m_std(list(pre))
            if r5 <= 0:
                continue
            pr = _periods_remaining(bars[eidx][0])
            if pr <= 1:
                continue
            buf = closes[:eidx + 1]
            med = rm.r5_median() if use_vol_gate else None
            d = decide_band_trade(buf, r5, pr, med, rm.width_median(), p)
            if not recorded:
                _, _, _, w = bollinger(buf, p.bb_len, p.bb_mult)
                rm.record(r5, w)
                recorded = True
            if d is None:
                continue
            # exit sim (identical to the validated engine)
            short, lng, side = d.short_strike, d.long_strike, d.side
            down = side == "sell_put_cs"
            sk = PUT_SK if down else CALL_SK
            credit = d.est_credit
            tp_lvl = credit * (1 - TP / 100.0)
            ev = None
            for i in idxs:
                if i <= eidx:
                    continue
                bm = _minute(bars[i][0]); c = closes[i]
                if bm >= 16 * 60:
                    ev = bs.spread_value(side, c, short, lng, 0.0) * MULT; break
                tv = bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * sk
                bb = bs.spread_value(side, c, short, lng, tv) * MULT
                if bb <= tp_lvl or (down and c <= short) or ((not down) and c >= short):
                    ev = bb; break
            if ev is None:
                ev = bs.spread_value(side, closes[idxs[-1]], short, lng, 0.0) * MULT
            daily[date] += (credit - ev) * HAIRCUT - COST   # haircut the realised edge
            n_trades += 1
    return daily, n_trades, sessions


def score(daily, n_trades, sessions, label):
    days = [v for k, v in sorted(daily.items()) if k >= OOS_START]
    alld = [v for k, v in sorted(daily.items())]
    if len(days) < 3:
        print(f"  {label:38} — too few trades ({n_trades})"); return
    t = newey_west_tstat(days)["t"]
    fire = 100.0 * len(alld) / sessions
    per_wk = n_trades / (sessions / 5.0)
    print(f"  {label:38} trades={n_trades:4d} fire={fire:4.0f}%  {per_wk:4.1f}/wk | "
          f"OOS ${st.mean(days):+6.1f}/d t={t:+5.2f} worst=${min(days):+7.0f} | total ${sum(alld):+8.0f}")


if __name__ == "__main__":
    print(f"REALITY-CALIBRATED (haircut {HAIRCUT}, real floor ${FLOOR_REAL_SPX:.0f}/ct = 10% width)\n")
    print("T1 — ENTRY LADDER (the frequency lever):")
    for slots, lab in (((600,), "1 entry  10:00 [current]"),
                       ((600, 690), "2 entries 10:00,11:30"),
                       ((600, 660, 720), "3 entries 10:00,11:00,12:00"),
                       ((600, 660, 720, 780), "4 entries 10:00-13:00"),
                       ((600, 645, 690, 735, 780), "5 entries 10:00-13:00 /45m")):
        score(*run(slots=slots), lab)
    print("\nT2 — WING WIDTH (4-entry ladder):")
    for w in (10.0, 20.0, 25.0):
        score(*run(slots=(600, 660, 720, 780), wing=w, floor_real=w * 10), f"wing {w:.0f}pt (floor 10% = ${w*10:.0f})")
    print("\nT3 — VOL GATE (4-entry ladder):")
    for g, lab in ((True, "vol gate ON [validated]"), (False, "vol gate OFF")):
        score(*run(slots=(600, 660, 720, 780), use_vol_gate=g), lab)
    print("\nT4 — CUSHION (4-entry ladder):")
    for c in (0.4, 0.5, 0.6, 0.8):
        score(*run(slots=(600, 660, 720, 780), cushion=c), f"cushion {c}%")

def combos():
    print("\n=== COMBINED CONFIGS — can we validate in 2 WEEKS? ===")
    cfgs = [
        ((600,), True, 0.5, "CURRENT: 1 entry, gate ON, cush 0.5"),
        ((600, 660, 720), True, 0.5, "A: 3 entries, gate ON, cush 0.5"),
        ((600, 660, 720), True, 0.4, "B: 3 entries, gate ON, cush 0.4"),
        ((600, 660, 720), False, 0.5, "C: 3 entries, gate OFF, cush 0.5"),
        ((600, 660, 720, 780), False, 0.4, "D: 4 entries, gate OFF, cush 0.4"),
        ((600, 645, 690, 735, 780), False, 0.4, "E: 5 entries, gate OFF, cush 0.4"),
    ]
    for slots, gate, cush, lab in cfgs:
        daily, n, sess = run(slots=slots, use_vol_gate=gate, cushion=cush)
        days = [v for k, v in sorted(daily.items()) if k >= OOS_START]
        alld = list(daily.values())
        t = newey_west_tstat(days)["t"]
        per_wk = n / (sess / 5.0)
        wins = sum(1 for v in alld if v > 0)
        print(f"  {lab:38} {per_wk:4.1f} trades/wk -> {per_wk*2:4.1f} in 2wks | "
              f"OOS ${st.mean(days):+6.1f}/d t={t:+5.2f} worst=${min(days):+7.0f} | "
              f"green {100*wins/len(alld):.0f}% | total ${sum(alld):+8.0f}")

combos()
