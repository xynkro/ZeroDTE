#!/usr/bin/env python3
"""Honest MEIC backtest — multiple-entry iron condors on the same BS kernel +
SPX 5-min data as the validated wave backtest.

Models the LIVE rules: at each ladder slot, sell a 16Δ call spread + 16Δ put
spread ($25 SPX wings), credit gated at EOD_IC_MIN_CREDIT_PCT of wing, per-condor
BREAKEVEN stop (buyback >= credit -> close at modeled buyback), else settle at
intrinsic at the close. Skew pricing per side (live config).

Honesty notes: BS-mid pricing (live builds use real CBOE credits — typically a
bit richer than flat BS); stop marks at bar close (live marks CBOE mids); NO
regime gate (live skips volatile days, which are the WORST IC days, so this
understates the live config — conservative). Costs: $50 SPX-scale RT per condor
(2 spreads x $25) baseline, stressed at $80.

Run: PYTHONPATH=. .venv/bin/python scripts/meic_backtest.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401 — load .env
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _prepare, _periods_remaining, ET

SHORT_DELTA = settings.EOD_IC_SHORT_DELTA          # 0.16
WING = settings.EOD_IC_WING_DOLLARS                # $25 SPX
MIN_CREDIT_PCT = getattr(settings, "EOD_IC_MIN_CREDIT_PCT", 5.0)
PM = settings.DIRECTIONAL_PREMIUM_MULT             # 1.2 IV/RV
PUT_SK = settings.DIRECTIONAL_SKEW_PUT_MULT        # 1.15
CALL_SK = settings.DIRECTIONAL_SKEW_CALL_MULT      # 0.90
MULT = 100


def run_meic(slots: list[str], cost_rt: float = 50.0, data_window: str = "max"):
    ctx = _prepare(data_window)
    if ctx is None:
        raise SystemExit("no data")
    bars, by_date, sessions, _, _, _ = ctx
    slot_mins = []
    for s in slots:
        hh, mm = s.split(":")
        slot_mins.append((s, int(hh) * 60 + int(mm)))

    entries = []
    for date, sb in by_date.items():
        sb = sorted(sb, key=lambda x: x.time)
        if len(sb) < 20:
            continue
        for slot, smin in slot_mins:
            # entry = first bar at/after the slot (within 25 min, like live)
            eb = None
            for b in sb:
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bm = et.hour * 60 + et.minute
                if smin <= bm <= smin + 25:
                    eb = b
                    break
            if eb is None:
                continue
            pre = [b.close for b in sb if b.time <= eb.time]
            if len(pre) < 5:
                continue
            r5 = bs.realized_5m_std(pre)
            if r5 <= 0:
                continue
            pr0 = _periods_remaining(eb.time)
            if pr0 <= 1:
                continue
            S0 = eb.close
            tv0 = bs.total_vol_to_expiry(r5, pr0, PM)
            c_tv0, p_tv0 = tv0 * CALL_SK, tv0 * PUT_SK
            cs = bs.strike_for_call_delta(S0, c_tv0, SHORT_DELTA)
            cl = cs + WING
            ps = bs.strike_for_put_delta(S0, p_tv0, SHORT_DELTA)
            pl = ps - WING
            credit_ps = (bs.spread_value("sell_call_cs", S0, cs, cl, c_tv0)
                         + bs.spread_value("sell_put_cs", S0, ps, pl, p_tv0))
            credit = credit_ps * MULT
            if credit / (WING * MULT) * 100.0 < MIN_CREDIT_PCT:
                continue  # thin-premium gate (live skips these)

            outcome, exit_val = "expiry", None
            for b in sb:
                if b.time <= eb.time:
                    continue
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bm = et.hour * 60 + et.minute
                if bm >= 16 * 60:
                    iv_c = bs.spread_value("sell_call_cs", b.close, cs, cl, 0.0)
                    iv_p = bs.spread_value("sell_put_cs", b.close, ps, pl, 0.0)
                    exit_val = (iv_c + iv_p) * MULT
                    outcome = "expiry"
                    break
                pr = _periods_remaining(b.time)
                tv = bs.total_vol_to_expiry(r5, pr, PM)
                bb = (bs.spread_value("sell_call_cs", b.close, cs, cl, tv * CALL_SK)
                      + bs.spread_value("sell_put_cs", b.close, ps, pl, tv * PUT_SK)) * MULT
                if bb >= credit:  # breakeven stop
                    exit_val = bb
                    outcome = "stop"
                    break
            if exit_val is None:
                exit_val = 0.0
            pnl = credit - exit_val - cost_rt
            entries.append({"date": str(date), "slot": slot, "credit": credit,
                            "outcome": outcome, "pnl": pnl})
    return entries


def report(entries, label):
    if not entries:
        print(f"{label}: NO ENTRIES")
        return
    pnls = [e["pnl"] for e in entries]
    wins = sum(1 for p in pnls if p > 0)
    stops = sum(1 for e in entries if e["outcome"] == "stop")
    by_day = defaultdict(float)
    for e in entries:
        by_day[e["date"]] += e["pnl"]
    days = list(by_day.values())
    green = sum(1 for d in days if d > 0) / len(days) * 100
    mean_d = st.mean(days)
    sd_d = st.stdev(days) if len(days) > 1 else 0
    t = mean_d / (sd_d / math.sqrt(len(days))) if sd_d > 0 else 0
    avg_credit = st.mean(e["credit"] for e in entries)
    print(f"{label}")
    print(f"  entries {len(entries)} | WIN RATE {wins/len(entries)*100:.1f}% | stop rate {stops/len(entries)*100:.1f}% | avg credit ${avg_credit:.0f}")
    print(f"  per-entry: mean ${st.mean(pnls):+.1f} | avg win ${st.mean([p for p in pnls if p>0]):+.0f} | avg loss ${st.mean([p for p in pnls if p<=0]):+.0f}")
    print(f"  per-DAY ({len(days)}d): ${mean_d:+.1f} SPX-scale | worst ${min(days):+.0f} | green {green:.0f}% | t={t:.2f}")
    print(f"  at SPY x1ct/slot: ${mean_d/10:+.2f}/day | worst day ${min(days)/10:+.1f}")
    era = {d: v for d, v in by_day.items() if d >= "2024-01-01"}
    if era:
        ed = list(era.values())
        print(f"  2024+ ({len(ed)}d): ${st.mean(ed):+.1f}/day SPX | worst ${min(ed):+.0f} | green {sum(1 for x in ed if x>0)/len(ed)*100:.0f}%")
    print()


if __name__ == "__main__":
    ladder = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
    print(f"=== MEIC honest backtest | {SHORT_DELTA:.2f}Δ ${WING:.0f}-wing | skew {CALL_SK}/{PUT_SK} | breakeven stop ===\n")
    e = run_meic(ladder, cost_rt=50.0)
    report(e, f"LIVE LADDER {ladder} @ $50 RT cost")
    report(run_meic(ladder, cost_rt=80.0), f"LIVE LADDER @ $80 RT cost (stress)")
    report(run_meic(["10:15"], cost_rt=50.0), "SINGLE 10:15 (old config) @ $50")
    report(run_meic(["11:00", "12:00", "13:00", "14:00"], cost_rt=50.0), "LATER LADDER 11/12/13/14 @ $50")
    # per-slot decomposition of the live ladder
    print("=== per-slot (live ladder, $50) ===")
    for s in ladder:
        report(run_meic([s], cost_rt=50.0), f"slot {s}")
