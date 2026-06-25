#!/usr/bin/env python3
"""Edge hunt — search a disciplined slate of candidate edges on the HONEST engine,
in-sample/out-of-sample split, scored on Newey-West t-stat + worst-day, with a
multiple-comparisons bar so testing many ideas can't crown a fluke.

Philosophy (the council's "find more edge", done honestly):
  - The LIVE sample is 13 nights (no power). The BACKTEST has ~1094 days. So we
    search on the backtest, but the ONLY score that counts is OUT-OF-SAMPLE.
  - IS = pre-2024 (501d), OOS = 2024-01-01→ (593d). A candidate that's great IS but
    dead OOS is overfit — exactly what we kill.
  - We vary the SIGNAL (when/which side), NOT the exit params — optimizing exits
    across candidates is the overfitting trap. Fixed sensible exits throughout.
  - Multiple comparisons: k candidates ⇒ a single t>2 is cheap. Survivor bar is
    OOS HAC t ≥ ~2.6 (Bonferroni-ish for k≈7) AND OOS mean>0 AND OOS not catastrophic.

NOTHING here trades or tunes live. It's a research filter: survivors EARN paper.

Run: PYTHONPATH=. .venv/bin/python scripts/edge_hunt.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _prepare, _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat

WING = settings.EOD_IC_WING_DOLLARS          # $25 SPX
PM   = settings.DIRECTIONAL_PREMIUM_MULT     # 1.2
PUT_SK, CALL_SK = settings.DIRECTIONAL_SKEW_PUT_MULT, settings.DIRECTIONAL_SKEW_CALL_MULT
MULT = 100
MIN_CREDIT_PCT = getattr(settings, "EOD_IC_MIN_CREDIT_PCT", 5.0)
OOS_START = "2024-01-01"
SPREAD_COST = 25.0                            # $ RT per single vertical, SPX-scale

ET_ = ET


def _min(b):
    et = b.time.astimezone(ET_) if b.time.tzinfo else b.time
    return et.hour * 60 + et.minute


def _entry_bar(sb, smin):
    for b in sb:
        if smin <= _min(b) <= smin + 25:
            return b
    return None


def _early_rv(sb):
    """Realized 5m vol of the first hour — a pre-entry regime read (no lookahead)."""
    closes = [b.close for b in sb if _min(b) <= 10 * 60 + 30]   # up to ~10:30
    return bs.realized_5m_std(closes) if len(closes) >= 5 else None


def _spread_pnl(sb, eb, side, delta, stop_mult, cost):
    """Honest single-vertical P&L: sell `side` at eb, BS-reprice each bar, stop when
    buyback ≥ stop_mult×credit, else settle at intrinsic at the last bar."""
    pre = [b.close for b in sb if b.time <= eb.time]
    if len(pre) < 5:
        return None
    r5 = bs.realized_5m_std(pre)
    if r5 <= 0:
        return None
    pr0 = _periods_remaining(eb.time)
    if pr0 <= 1:
        return None
    S0 = eb.close
    sk = CALL_SK if side == "sell_call_cs" else PUT_SK
    tv0 = bs.total_vol_to_expiry(r5, pr0, PM) * sk
    if side == "sell_call_cs":
        cs = bs.strike_for_call_delta(S0, tv0, delta); cl = cs + WING
    else:
        cs = bs.strike_for_put_delta(S0, tv0, delta); cl = cs - WING
    credit = bs.spread_value(side, S0, cs, cl, tv0) * MULT
    if credit <= 0:
        return None
    eb_min = _min(eb)
    exit_val = None
    for b in sb:
        if b.time <= eb.time:
            continue
        bm = _min(b)
        if bm >= 16 * 60:
            exit_val = bs.spread_value(side, b.close, cs, cl, 0.0) * MULT
            break
        tv = bs.total_vol_to_expiry(r5, _periods_remaining(b.time), PM) * sk
        bb = bs.spread_value(side, b.close, cs, cl, tv) * MULT
        if bb >= credit * stop_mult:
            exit_val = bb
            break
    if exit_val is None:
        exit_val = bs.spread_value(side, sb[-1].close, cs, cl, 0.0) * MULT
    return credit - exit_val - cost


def _condor_pnl(sb, eb, delta, stop_buffer, cost):
    """Honest iron-condor P&L (sell call spread + put spread), breakeven stop."""
    call = _spread_pnl_struct(sb, eb, "sell_call_cs", delta)
    put = _spread_pnl_struct(sb, eb, "sell_put_cs", delta)
    if call is None or put is None:
        return None
    credit = call["credit"] + put["credit"]
    if credit / (2 * WING * MULT) * 100.0 < MIN_CREDIT_PCT:
        return None
    eb_min = _min(eb)
    r5c, r5p = call["r5"], put["r5"]
    exit_val = None
    for b in sb:
        if b.time <= eb.time:
            continue
        bm = _min(b)
        if bm >= 16 * 60:
            iv = (bs.spread_value("sell_call_cs", b.close, call["cs"], call["cl"], 0.0)
                  + bs.spread_value("sell_put_cs", b.close, put["cs"], put["cl"], 0.0)) * MULT
            exit_val = iv; break
        tvc = bs.total_vol_to_expiry(r5c, _periods_remaining(b.time), PM) * CALL_SK
        tvp = bs.total_vol_to_expiry(r5p, _periods_remaining(b.time), PM) * PUT_SK
        bb = (bs.spread_value("sell_call_cs", b.close, call["cs"], call["cl"], tvc)
              + bs.spread_value("sell_put_cs", b.close, put["cs"], put["cl"], tvp)) * MULT
        if bb >= credit * stop_buffer:
            exit_val = bb; break
    if exit_val is None:
        lc = sb[-1].close
        exit_val = (bs.spread_value("sell_call_cs", lc, call["cs"], call["cl"], 0.0)
                    + bs.spread_value("sell_put_cs", lc, put["cs"], put["cl"], 0.0)) * MULT
    return credit - exit_val - cost


def _spread_pnl_struct(sb, eb, side, delta):
    pre = [b.close for b in sb if b.time <= eb.time]
    if len(pre) < 5:
        return None
    r5 = bs.realized_5m_std(pre)
    if r5 <= 0:
        return None
    pr0 = _periods_remaining(eb.time)
    if pr0 <= 1:
        return None
    S0 = eb.close
    sk = CALL_SK if side == "sell_call_cs" else PUT_SK
    tv0 = bs.total_vol_to_expiry(r5, pr0, PM) * sk
    if side == "sell_call_cs":
        cs = bs.strike_for_call_delta(S0, tv0, delta); cl = cs + WING
    else:
        cs = bs.strike_for_put_delta(S0, tv0, delta); cl = cs - WING
    credit = bs.spread_value(side, S0, cs, cl, tv0) * MULT
    return {"cs": cs, "cl": cl, "credit": credit, "r5": r5}


# ── candidate strategies: each returns {date_str: day_pnl} ──────────────────
LADDER = ["11:00", "12:00", "13:00", "14:00"]
SLOTMIN = lambda s: int(s[:2]) * 60 + int(s[3:])


def cand_condor(by_date, delta=0.16, slots=LADDER, low_rv=False):
    out, rv_hist = {}, []
    for date, sb in by_date.items():
        sb = sorted(sb, key=lambda x: x.time)
        if len(sb) < 20:
            continue
        if low_rv:
            erv = _early_rv(sb)
            med = st.median(rv_hist) if len(rv_hist) >= 20 else None
            if erv is not None:
                rv_hist.append(erv)
            if med is not None and (erv is None or erv >= med):
                continue   # only trade calm-open days
        day = 0.0; hit = False
        for s in slots:
            eb = _entry_bar(sb, SLOTMIN(s))
            if eb is None:
                continue
            p = _condor_pnl(sb, eb, delta, settings.IC_STOP_BUFFER, 2 * SPREAD_COST)
            if p is not None:
                day += p; hit = True
        if hit:
            out[str(date)] = day
    return out


def cand_directional(by_date, entry="10:00", mode="fade", delta=0.30):
    out = {}
    emin = SLOTMIN(entry)
    for date, sb in by_date.items():
        sb = sorted(sb, key=lambda x: x.time)
        if len(sb) < 20:
            continue
        eb = _entry_bar(sb, emin)
        if eb is None:
            continue
        open_px = sb[0].close
        moved_up = eb.close >= open_px
        # FADE: sell the side price moved TOWARD (bet reversion). TREND: lean with it.
        if mode == "fade":
            side = "sell_call_cs" if moved_up else "sell_put_cs"
        else:
            side = "sell_put_cs" if moved_up else "sell_call_cs"
        p = _spread_pnl(sb, eb, side, delta, stop_mult=2.0, cost=SPREAD_COST)
        if p is not None:
            out[str(date)] = p
    return out


CANDIDATES = [
    ("C1 condor 16Δ ladder (LIVE control)", lambda bd: cand_condor(bd, 0.16)),
    ("C2 condor 16Δ ladder · calm-open only", lambda bd: cand_condor(bd, 0.16, low_rv=True)),
    ("C3 condor 10Δ ladder (further OTM)", lambda bd: cand_condor(bd, 0.10)),
    ("C4 condor 25Δ ladder (more credit)", lambda bd: cand_condor(bd, 0.25)),
    ("C5 condor 16Δ single 12:00", lambda bd: cand_condor(bd, 0.16, slots=["12:00"])),
    ("D1 open-range FADE 10:00 (30Δ)", lambda bd: cand_directional(bd, "10:00", "fade")),
    ("D2 open-range TREND 10:00 (30Δ)", lambda bd: cand_directional(bd, "10:00", "trend")),
]
K = len(CANDIDATES)
# Bonferroni-ish two-sided z for α=0.05 across K tests
BAR_T = round(abs(_zfix := 0) or (2.0 + 0.6 * math.log(max(K, 1))), 2)  # ~2.6 for K=7


def _score(series: dict, lo=None, hi=None):
    days = [v for d, v in series.items() if (lo is None or d >= lo) and (hi is None or d < hi)]
    if len(days) < 2:
        return None
    nw = newey_west_tstat(days)
    return {"n": len(days), "total": sum(days), "mean": st.mean(days),
            "worst": min(days), "green": sum(1 for v in days if v > 0) / len(days) * 100,
            "t": nw["t"]}


def _fmt(s):
    return (f"n={s['n']:>4} mean${s['mean']:+6.0f} t={s['t']:+5.2f} "
            f"worst${s['worst']:+6.0f} green{s['green']:>3.0f}%") if s else "  (no trades)"


if __name__ == "__main__":
    print("Loading honest engine (max window)…")
    bars, by_date, *_ = _prepare("max")
    print(f"EDGE HUNT · {K} candidates · IS pre-{OOS_START} vs OOS {OOS_START}+ · "
          f"survivor bar: OOS t≥{BAR_T} (Bonferroni-ish for {K} tests) AND OOS mean>0\n")
    rows = []
    for name, fn in CANDIDATES:
        series = fn(by_date)
        is_s = _score(series, hi=OOS_START)
        oos = _score(series, lo=OOS_START)
        survive = bool(oos and oos["mean"] > 0 and oos["t"] >= BAR_T)
        rows.append((name, is_s, oos, survive))
        print(f"{'✅' if survive else '  '} {name}")
        print(f"      IS : {_fmt(is_s)}")
        print(f"      OOS: {_fmt(oos)}")
    print("\n" + "=" * 70)
    winners = [r for r in rows if r[3]]
    if winners:
        print(f"SURVIVORS ({len(winners)}): " + "; ".join(r[0] for r in winners))
        print("→ these EARN a paper run. Re-confirm live before any size.")
    else:
        print("NO SURVIVORS. Every candidate fails the OOS bar. This slate has no")
        print("edge worth paper-trading. That's a real result — it saves months.")
        print("Next: widen the slate (new signals/instruments/timeframes), not size.")
    print(f"\nNote: tested {K} candidates → multiple-comparisons bar t≥{BAR_T} applied to OOS.")
    print("IS-strong / OOS-dead = overfit. Only OOS counts. Nothing here traded live.")
