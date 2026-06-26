#!/usr/bin/env python3
"""WAVE structure A/B — put credit spread vs put BROKEN-WING BUTTERFLY.

The research lever (CBOE PUT-vs-PPUT + tail-hedge literature): a credit spread is
already defined-risk, so the real problem isn't an uncapped tail — it's the lousy
risk/reward (risk ~$1,000 to make ~$30). A broken-wing butterfly keeps the "price
stays up = keep credit" profile but improves max-loss-per-credit by financing a
deep-OTM long. This tests it HONESTLY: identical band-anchored entries, identical
exits, BS-priced leg-by-leg (put_price), IS/OOS, worst-day + return/max-DD.

Both = LONG a structure entered for a NET CREDIT (V0<0); profit when price stays up
and the structure decays to ~0. Put fly = +1 put(K+up) −2 put(K) +1 put(K−dn).

Run: PYTHONPATH=. .venv/bin/python scripts/wave_bwb_backtest.py
"""
from __future__ import annotations

import json
import statistics as st
from datetime import datetime

import numpy as np

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app import bs_pricing as bs
from backend.app.honest_backtest import _periods_remaining, ET
from backend.app.quant_utils import newey_west_tstat

MULT = 100
PM = settings.DIRECTIONAL_PREMIUM_MULT
PUT_SK = settings.DIRECTIONAL_SKEW_PUT_MULT
OOS_START = "2024-01-01"
BB_LEN, ENTRY_MIN, COST = 20, 10 * 60, 5.0


def _load():
    path = settings.data_dir / "historical" / "SPX_5m_3y.json"
    return [(datetime.fromisoformat(r["datetime"]), r["close"]) for r in json.loads(path.read_text())]


def _minute(t):
    et = t.astimezone(ET) if t.tzinfo else t
    return et.hour * 60 + et.minute


def _spread_credit(S, Ks, W, tv):           # put credit spread value (cost to close)
    return (bs.put_price(S, Ks, tv) - bs.put_price(S, Ks - W, tv)) * MULT


def _fly_value(S, Kb, up, dn, tv):          # +1 put(Kb+up) −2 put(Kb) +1 put(Kb−dn)
    return (bs.put_price(S, Kb + up, tv) - 2 * bs.put_price(S, Kb, tv)
            + bs.put_price(S, Kb - dn, tv)) * MULT


def backtest(W=10.0, up=20.0, dn=10.0, tp_pct=40.0, floor=30.0, min_otm=0.20):
    bars = _load()
    closes = np.array([b[1] for b in bars], float)
    sma = np.array([closes[max(0, i - BB_LEN + 1):i + 1].mean() for i in range(len(closes))])
    std = np.array([closes[max(0, i - BB_LEN + 1):i + 1].std() for i in range(len(closes))])
    lower = sma - 2 * std
    by_day = {}
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day.setdefault(et.strftime("%Y-%m-%d"), []).append(i)

    cs_daily, bwb_daily = {}, {}
    cs_maxloss, bwb_maxloss = [], []
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20:
            continue
        eidx = next((i for i in idxs if _minute(bars[i][0]) >= ENTRY_MIN), None)
        if eidx is None:
            continue
        S0 = closes[eidx]
        pre = closes[idxs[0]:eidx + 1]
        if len(pre) < 5:
            continue
        r5 = bs.realized_5m_std(list(pre))
        if r5 <= 0:
            continue
        pr0 = _periods_remaining(bars[eidx][0])
        if pr0 <= 1:
            continue
        tv0 = bs.total_vol_to_expiry(r5, pr0, PM) * PUT_SK

        # band-anchored short, walked UP toward spot until the spread clears the floor
        Ks = float(round(lower[eidx]))
        cap = S0 * (1 - min_otm / 100.0)
        tries = 0
        while _spread_credit(S0, Ks, W, tv0) < floor and tries < 80:
            if Ks + 5 > cap:
                break
            Ks += 5; tries += 1
        if _spread_credit(S0, Ks, W, tv0) < floor:
            continue

        def reprice(i):
            return closes[i], bs.total_vol_to_expiry(r5, _periods_remaining(bars[i][0]), PM) * PUT_SK

        # ── credit spread ──
        cs_cr = _spread_credit(S0, Ks, W, tv0)
        cs_tp = cs_cr * (1 - tp_pct / 100.0)
        ev = None
        for i in idxs:
            if i <= eidx:
                continue
            bm = _minute(bars[i][0]); c = closes[i]
            if bm >= 16 * 60:
                ev = (max(0.0, bs.put_price(c, Ks, 0.0) - bs.put_price(c, Ks - W, 0.0))) * MULT; break
            cc, tv = reprice(i)
            bb = max(0.0, bs.put_price(cc, Ks, tv) - bs.put_price(cc, Ks - W, tv)) * MULT
            if bb <= cs_tp or c <= Ks:
                ev = bb; break
        if ev is None:
            c = closes[idxs[-1]]; ev = max(0.0, bs.put_price(c, Ks, 0.0) - bs.put_price(c, Ks - W, 0.0)) * MULT
        cs_pnl = cs_cr - ev - COST
        cs_daily[date] = cs_daily.get(date, 0.0) + cs_pnl
        cs_maxloss.append(W * MULT - cs_cr)

        # ── broken-wing butterfly (long the fly, entered for a credit) ──
        V0 = _fly_value(S0, Ks, up, dn, tv0)        # <0 ⇒ credit
        bwb_cr = -V0
        # theoretical max loss of the structure (scan the downside grid at expiry)
        ml = max(0.0, -min((_fly_value(S0 * (1 - x / 100.0), Ks, up, dn, 0.0) - V0)
                           for x in (0, 0.5, 1, 1.5, 2, 3, 4, 5)))
        bwb_maxloss.append(ml)
        bwb_tp = abs(bwb_cr) * (tp_pct / 100.0)
        ev2 = None
        for i in idxs:
            if i <= eidx:
                continue
            bm = _minute(bars[i][0]); c = closes[i]
            if bm >= 16 * 60:
                ev2 = _fly_value(c, Ks, up, dn, 0.0) - V0; break
            cc, tv = reprice(i)
            pnl_now = _fly_value(cc, Ks, up, dn, tv) - V0
            if pnl_now >= bwb_tp or c <= Ks:         # TP, or body breached → bail
                ev2 = pnl_now; break
        if ev2 is None:
            ev2 = _fly_value(closes[idxs[-1]], Ks, up, dn, 0.0) - V0
        bwb_daily[date] = bwb_daily.get(date, 0.0) + (ev2 - COST)

    return cs_daily, bwb_daily, cs_maxloss, bwb_maxloss


def score(d, lo=None, hi=None):
    days = [v for k, v in d.items() if (lo is None or k >= lo) and (hi is None or k < hi)]
    if len(days) < 2:
        return None
    return {"n": len(days), "mean": st.mean(days), "t": newey_west_tstat(days)["t"],
            "worst": min(days), "best": max(days),
            "green": sum(1 for v in days if v > 0) / len(days) * 100}


def _f(s):
    return (f"n={s['n']:>4} mean${s['mean']:+6.0f} t={s['t']:+5.2f} "
            f"worst${s['worst']:+7.0f} best${s['best']:+6.0f} green{s['green']:>3.0f}%") if s else "(none)"


def _row(name, d, ml):
    oos = score(d, lo=OOS_START)
    rr = (oos['mean'] / abs(oos['worst'])) if (oos and oos['worst'] < 0) else float('nan')
    print(f"{name:28} maxloss${st.mean(ml):>6,.0f} | OOS {_f(oos)} | mean/|worst| {rr:+.3f}")


if __name__ == "__main__":
    print("WAVE structure A/B — put credit spread vs broken-wing flies (identical band entries)\n")
    print(f"{'structure':28} {'avg risk':>13} | {'OUT-OF-SAMPLE':^52} | geometry")
    print("  " + "-" * 110)
    cs, _, csml, _ = backtest(up=20, dn=10)
    _row("CREDIT SPREAD (10-wide)", cs, csml)
    for up, dn in ((10, 30), (20, 20), (30, 10)):
        _, bwb, _, bwbml = backtest(up=up, dn=dn)
        tag = "credit" if up < dn else ("debit/pin" if up > dn else "balanced")
        _row(f"BWB up{up}/dn{dn} ({tag})", bwb, bwbml)
    print("\n" + "=" * 88)
    print("Read: 'credit' BWBs (up<dn) keep the income profile; 'debit/pin' (up>dn) has a tiny")
    print("risk but is a different bet (pays only if price pins, not pure income). Watch avg-risk")
    print("AND mean/|worst|: a structure only wins if it cuts the risk WITHOUT killing the edge.")
    print("In-model; 4-leg flies also eat MORE slippage than spreads live — that gap matters.")
