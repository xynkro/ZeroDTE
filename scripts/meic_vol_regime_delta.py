#!/usr/bin/env python3
"""Regime-conditional delta: 10Δ on VOLATILE days, 16Δ on CALM days.

"Volatile" is defined ONLY with info available at entry (no lookahead): the
MORNING realized vol — the 5-min log-return stdev up to the first ladder slot
(11:00). Live you'd proxy this with VIX1D at 11:00. We split days at the median
morning vol (and also test the top-tercile threshold), then on volatile days run
all that day's condors at 10Δ (wider/safer) and on calm days at 16Δ (more credit).

Compared head-to-head with fixed-16Δ and fixed-10Δ, FULL sample and OOS (2024+),
because conditioning on a vol signal is exactly the kind of thing that looks great
in-sample and decays out-of-sample (cf. the VIX-up-at-open filter, t=4.25→0.74 OOS).

Run: PYTHONPATH=. .venv/bin/python scripts/meic_vol_regime_delta.py
"""
from __future__ import annotations

import statistics as st
from collections import defaultdict

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _prepare, ET
import scripts.meic_backtest as mb
from backend.app.quant_utils import newey_west_tstat

LADDER = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
_h, _m = LADDER[0].split(":")
FIRST_MIN = int(_h) * 60 + int(_m)


def morning_vol_by_day():
    """Realized 5m vol up to the first ladder slot, per day (entry-available)."""
    bars, by_date, *_ = _prepare("max")
    out = {}
    for date, sb in by_date.items():
        sb = sorted(sb, key=lambda x: x.time)
        eb = None
        for b in sb:
            et = b.time.astimezone(ET) if b.time.tzinfo else b.time
            bm = et.hour * 60 + et.minute
            if FIRST_MIN <= bm <= FIRST_MIN + 25:
                eb = b
                break
        if eb is None:
            continue
        pre = [b.close for b in sb if b.time <= eb.time]
        if len(pre) < 5:
            continue
        r5 = bs.realized_5m_std(pre)
        if r5 > 0:
            out[str(date)] = r5
    return out


def run_idx(delta):
    mb.SHORT_DELTA = delta
    return {(x["date"], x["slot"]): x for x in mb.run_meic(LADDER, cost_rt=50.0)}


def stats(entries, since=None):
    e = [x for x in entries if since is None or x["date"] >= since]
    if not e:
        return None
    byday = defaultdict(float)
    for x in e:
        byday[x["date"]] += x["pnl"]
    days = [v / 10.0 for v in byday.values()]   # SPY scale
    return {
        "entries": len(e), "ndays": len(days),
        "mean": st.mean(days), "worst": min(days),
        "green": sum(1 for d in days if d > 0) / len(days) * 100,
        "t": newey_west_tstat(days)["t"],
    }


def blend(d16, d10, morn, thr):
    """Volatile day (morning vol > thr) → 10Δ; else 16Δ."""
    days = sorted(set(k[0] for k in d16) | set(k[0] for k in d10))
    out = []
    nvol = 0
    for date in days:
        v = morn.get(date)
        use10 = v is not None and v > thr
        nvol += use10
        src = d10 if use10 else d16
        for slot in LADDER:
            x = src.get((date, slot))
            if x:
                out.append(x)
    return out, nvol, len(days)


def line(name, s):
    if not s:
        print(f"  {name:<26} (no entries)")
        return
    print(f"  {name:<26} {s['entries']:>6} {s['mean']:>+8.1f} {s['worst']:>+9.0f} "
          f"{s['green']:>6.0f}% {s['t']:>6.1f}")


if __name__ == "__main__":
    morn = morning_vol_by_day()
    med = st.median(morn.values())
    terc = sorted(morn.values())[int(len(morn) * 2 / 3)]   # top-third threshold
    d16, d10 = run_idx(0.16), run_idx(0.10)

    fixed16 = list(d16.values())
    fixed10 = list(d10.values())
    blend_med, nvol_m, ntot = blend(d16, d10, morn, med)
    blend_terc, nvol_t, _ = blend(d16, d10, morn, terc)

    print(f"=== Regime-conditional delta | ladder {LADDER} | $25 wing | $50 RT ===")
    print(f"  'volatile' = morning realized 5m vol > threshold (entry-available; live ≈ VIX1D@11:00)")
    print(f"  median thr → {nvol_m}/{ntot} days volatile (10Δ) · top-third thr → {nvol_t}/{ntot} (10Δ)\n")

    for label, since in (("FULL SAMPLE", None), ("OOS 2024+", "2024-01-01")):
        print(f"  --- {label} ---")
        print(f"  {'config':<26} {'entr':>6} {'$/day':>8} {'worst':>9} {'green':>7} {'t':>6}")
        line("fixed 16Δ (live)", stats(fixed16, since))
        line("fixed 10Δ", stats(fixed10, since))
        line("regime 10/16 (median)", stats(blend_med, since))
        line("regime 10/16 (top-third)", stats(blend_terc, since))
        print()

    print("  Verdict test: does regime-switch beat fixed-16Δ on BOTH t-stat AND worst-day,")
    print("  AND hold up OOS? If it only wins in-sample, it's another overfit vol filter.")
