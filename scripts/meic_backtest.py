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


def run_meic(slots: list[str], cost_rt: float = 50.0, data_window: str = "max",
             stop_buffer: float = 1.0, take_profit_pct: float | None = None,
             time_stop_min: int | None = None,
             credit_richness: float = 1.0, stop_anchor: str = "real"):
    """stop_buffer: stop when buy-back >= buffer×credit (1.0=breakeven, ∞=no stop).
    take_profit_pct: close early when buy-back <= (1-tp/100)×credit (None=no TP).
    time_stop_min: close N minutes after entry at the current mark (None=ride to expiry).

    credit_richness (R): live Alpaca fills land R× richer than the BS model credit
    (measured ~1.5-1.9× over Jun nights). We scale BOTH the collected credit AND the
    intraday CBOE-marked buy-back by R; the EXPIRY intrinsic is NOT scaled (richness is
    time-premium, gone at settlement). R=1.0 reproduces the pure-model backtest.
    stop_anchor: which credit the stop threshold uses —
      'real'  → buffer × (R×credit): a true breakeven on the credit actually collected.
      'model' → buffer × credit (un-scaled BS): RECREATES THE LIVE BUG, where the
                threshold is anchored to the model while the buy-back marks off the
                richer CBOE chain — firing the stop at ~buffer/R of real credit."""
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
            model_credit = credit_ps * MULT          # BS model credit (gate + 'model' anchor)
            if model_credit / (WING * MULT) * 100.0 < MIN_CREDIT_PCT:
                continue  # thin-premium gate (live skips these) — gates on model credit, like live
            R = credit_richness
            real_credit = model_credit * R           # credit actually collected (rich fills)
            # Stop threshold: 'real' anchors to collected credit (true breakeven);
            # 'model' anchors to the un-scaled BS credit (recreates the live bug).
            stop_thresh = (real_credit if stop_anchor == "real" else model_credit) * stop_buffer

            eb_et = eb.time.astimezone(ET) if eb.time.tzinfo else eb.time
            eb_min = eb_et.hour * 60 + eb_et.minute
            outcome, exit_val = "expiry", None
            # Excursion over the realized holding window; entry mark ≈ real_credit
            # (unrealized P&L ≈ 0). Intraday buy-backs are CBOE-rich (×R); expiry is not.
            min_bb = max_bb = real_credit
            for b in sb:
                if b.time <= eb.time:
                    continue
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bm = et.hour * 60 + et.minute
                if bm >= 16 * 60:
                    iv_c = bs.spread_value("sell_call_cs", b.close, cs, cl, 0.0)
                    iv_p = bs.spread_value("sell_put_cs", b.close, ps, pl, 0.0)
                    exit_val = (iv_c + iv_p) * MULT     # intrinsic — no richness at settlement
                    outcome = "expiry"
                    break
                pr = _periods_remaining(b.time)
                tv = bs.total_vol_to_expiry(r5, pr, PM)
                bb = (bs.spread_value("sell_call_cs", b.close, cs, cl, tv * CALL_SK)
                      + bs.spread_value("sell_put_cs", b.close, ps, pl, tv * PUT_SK)) * MULT * R
                min_bb = min(min_bb, bb)
                max_bb = max(max_bb, bb)
                # exit priority each bar: take-profit (winner) → stop (loser) → time exit
                if take_profit_pct is not None and bb <= real_credit * (1 - take_profit_pct / 100.0):
                    exit_val = bb
                    outcome = "tp"
                    break
                if bb >= stop_thresh:  # stop at threshold (anchor-dependent)
                    exit_val = bb
                    outcome = "stop"
                    break
                if time_stop_min is not None and (bm - eb_min) >= time_stop_min:
                    exit_val = bb
                    outcome = "time"
                    break
            if exit_val is None:
                # Held to session end without stopping. The data ends ~15:55 ET (no
                # 16:00 print, so the bm>=16:00 branch above NEVER fires) — settle at
                # REAL intrinsic at the last bar, NOT 0. The old `exit_val = 0.0`
                # fake-won EVERY held condor (worst day went POSITIVE, 100% green),
                # grossly inflating wide-/no-stop configs in the buffer sweep.
                lc = sb[-1].close
                exit_val = (bs.spread_value("sell_call_cs", lc, cs, cl, 0.0)
                            + bs.spread_value("sell_put_cs", lc, ps, pl, 0.0)) * MULT
                outcome = "expiry"
            # Fold the realized exit into the excursion window so a same-bar
            # stop/expiry that never traversed an intraday repricing still scores.
            min_bb = min(min_bb, exit_val)
            max_bb = max(max_bb, exit_val)
            mfe = real_credit - min_bb       # peak unrealized profit available ($)
            mae = real_credit - max_bb       # deepest unrealized drawdown ($, ≤0)
            pnl = real_credit - exit_val - cost_rt
            entries.append({"date": str(date), "slot": slot, "credit": real_credit,
                            "outcome": outcome, "pnl": pnl,
                            "gross": real_credit - exit_val, "mfe": mfe, "mae": mae})
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
    # HAC (Newey-West) t-stat — daily 0DTE P&L is serially correlated (regime
    # clustering; the ladder shares a day's tape), so the naive t overstates
    # significance. The gate reads the NW t; naive shown in parens for contrast.
    from backend.app.quant_utils import newey_west_tstat
    nw = newey_west_tstat(days)
    t = nw["t"]
    avg_credit = st.mean(e["credit"] for e in entries)
    print(f"{label}")
    print(f"  entries {len(entries)} | WIN RATE {wins/len(entries)*100:.1f}% | stop rate {stops/len(entries)*100:.1f}% | avg credit ${avg_credit:.0f}")
    print(f"  per-entry: mean ${st.mean(pnls):+.1f} | avg win ${st.mean([p for p in pnls if p>0]):+.0f} | avg loss ${st.mean([p for p in pnls if p<=0]):+.0f}")
    print(f"  per-DAY ({len(days)}d): ${mean_d:+.1f} SPX-scale | worst ${min(days):+.0f} | green {green:.0f}% | t={t:.2f} (NW L={nw['lags']}, naive {nw['naive_t']:.2f})")
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
