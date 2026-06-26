#!/usr/bin/env python3
"""WAVE failure analysis — WHY do the losing days lose, and what would have flagged them?

Runs the TUNED config (Schwartz vol-released gate + Bollinger 14/2.5) with per-day
diagnostics, then computes — at ENTRY, no lookahead — the indicators Caspar reads on the
chart: VWAP (true, volume-weighted), RSI(14), Stochastic %K(14), Williams VIX Fix(22),
ADX(14) trend-strength, Bollinger position, morning range, strike distance. It splits
winners vs losers, ranks which feature SEPARATES them (point-biserial r), classifies HOW
each loser died (exit reason + how far price ran through the short), and dumps the worst
days. Outcome features (day move, breach depth) are for attribution only — never filters.

Continuous-series indicators match a 5m TradingView chart (they don't reset daily).
Run: PYTHONPATH=. .venv/bin/python scripts/wave_failure_analysis.py
"""
from __future__ import annotations

import json
import math
import statistics as st
from collections import defaultdict
from datetime import datetime

import numpy as np

import backend.app.config  # noqa: F401
from backend.app.config import settings
from backend.app.honest_backtest import ET
from scripts.wave_band_backtest import main, OOS_START, _minute, ENTRY_MIN


def _load_ohlcv():
    raw = json.loads((settings.data_dir / "historical" / "SPX_5m_3y.json").read_text())
    t = [datetime.fromisoformat(r["datetime"]) for r in raw]
    o = np.array([r["open"] for r in raw], float)
    h = np.array([r["high"] for r in raw], float)
    l = np.array([r["low"] for r in raw], float)
    c = np.array([r["close"] for r in raw], float)
    v = np.array([r["volume"] for r in raw], float)
    return t, o, h, l, c, v


def _rsi(c, i, n=14):
    if i < n:
        return 50.0
    d = np.diff(c[i - n:i + 1])
    up = d[d > 0].sum() / n
    dn = -d[d < 0].sum() / n
    if dn == 0:
        return 100.0
    rs = up / dn
    return 100.0 - 100.0 / (1.0 + rs)


def _stoch(c, h, l, i, n=14):
    if i < n:
        return 50.0
    hh = h[i - n + 1:i + 1].max()
    ll = l[i - n + 1:i + 1].min()
    return 100.0 * (c[i] - ll) / (hh - ll) if hh > ll else 50.0


def _wvf(c, l, i, n=22):
    if i < n:
        return 0.0
    hc = c[i - n + 1:i + 1].max()
    return 100.0 * (hc - l[i]) / hc if hc > 0 else 0.0


def _adx(h, l, c, n=14):
    """Wilder ADX on the continuous 5m series (trend strength)."""
    N = len(c)
    tr = np.zeros(N); pdm = np.zeros(N); mdm = np.zeros(N)
    for i in range(1, N):
        up = h[i] - h[i - 1]; dn = l[i - 1] - l[i]
        pdm[i] = up if (up > dn and up > 0) else 0.0
        mdm[i] = dn if (dn > up and dn > 0) else 0.0
        tr[i] = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))

    def wild(x):
        out = np.zeros(N)
        if N <= n:
            return out
        out[n] = x[1:n + 1].sum()
        for i in range(n + 1, N):
            out[i] = out[i - 1] - out[i - 1] / n + x[i]
        return out

    atr = wild(tr); pdi_s = wild(pdm); mdi_s = wild(mdm)
    pdi = 100 * np.divide(pdi_s, atr, out=np.zeros(N), where=atr != 0)
    mdi = 100 * np.divide(mdi_s, atr, out=np.zeros(N), where=atr != 0)
    dx = 100 * np.divide(np.abs(pdi - mdi), pdi + mdi, out=np.zeros(N), where=(pdi + mdi) != 0)
    adx = np.zeros(N)
    if N > 2 * n:
        adx[2 * n] = dx[n + 1:2 * n + 1].mean()
        for i in range(2 * n + 1, N):
            adx[i] = (adx[i - 1] * (n - 1) + dx[i]) / n
    return adx, pdi, mdi


def _pbr(feat, loss):
    """point-biserial correlation between a feature and the binary loss flag."""
    x = np.array(feat, float); y = np.array(loss, float)
    if x.std() == 0 or y.std() == 0:
        return 0.0
    return float(np.corrcoef(x, y)[0, 1])


def run():
    t, o, h, l, c, v = _load_ohlcv()
    by_day = defaultdict(list)
    for i, ti in enumerate(t):
        et = ti.astimezone(ET) if ti.tzinfo else ti
        by_day[et.strftime("%Y-%m-%d")].append(i)
    adx, pdi, mdi = _adx(h, l, c)

    diag = []
    main(regime="released", bb_len=14, bb_mult=2.5, diag=diag)

    rows = []
    for d in diag:
        date = d["date"]; eidx = d["eidx"]; idxs = by_day[date]
        pre = [j for j in idxs if j <= eidx]
        # true session VWAP (open -> entry)
        tp = (h[pre] + l[pre] + c[pre]) / 3.0
        vol = v[pre]
        vwap = float((tp * vol).sum() / vol.sum()) if vol.sum() > 0 else float(c[eidx])
        # morning range so far (no lookahead)
        mhi = float(h[pre].max()); mlo = float(l[pre].min())
        S0 = d["S0"]
        down = d["down"]
        # entry features (all known at entry)
        feats = dict(
            rsi=_rsi(c, eidx), stoch=_stoch(c, h, l, eidx), wvf=_wvf(c, l, eidx),
            adx=float(adx[eidx]),
            di_with_trade=float(mdi[eidx] if down else pdi[eidx]),  # momentum in OUR adverse dir
            bb_pos=(S0 - d["lower"]) / (d["upper"] - d["lower"]) if d["upper"] > d["lower"] else 0.5,
            morn_range_pct=100.0 * (mhi - mlo) / S0,
            ext_vwap_pct=100.0 * (S0 - vwap) / S0,             # +above / -below VWAP
            ext_sma_pct=100.0 * (S0 - d["sma"]) / d["sma"],
            pct_otm=100.0 * abs(S0 - d["short"]) / S0,
            choppy=1.0 if d["choppy"] else 0.0,
        )
        # outcome / attribution (NOT filter features)
        out = dict(
            pnl=d["pnl"], loss=1.0 if d["pnl"] < 0 else 0.0, reason=d["reason"],
            day_move_pct=100.0 * (d["last"] - S0) / S0,
            adverse_pct=100.0 * ((S0 - d["day_lo"]) if down else (d["day_hi"] - S0)) / S0,
            through_pts=d["worst_through"], side=d["side"],
        )
        rows.append({"date": date, **feats, **out})
    return rows


def report(rows):
    win = [r for r in rows if r["pnl"] >= 0]
    los = [r for r in rows if r["pnl"] < 0]
    n = len(rows)
    print(f"TUNED config — {n} trade-days | winners {len(win)} ({len(win)/n*100:.0f}%) | "
          f"losers {len(los)} ({len(los)/n*100:.0f}%)")
    print(f"  total ${sum(r['pnl'] for r in rows):+,.0f} | "
          f"win avg ${st.mean([r['pnl'] for r in win]):+.0f} | loss avg ${st.mean([r['pnl'] for r in los]):+.0f}")

    # how did losers vs winners EXIT?
    print("\nEXIT REASON  (winners | losers):")
    for rs in ("tp", "settle", "breach", "expiry", "hard", "2x"):
        w = sum(1 for r in win if r["reason"] == rs); ll = sum(1 for r in los if r["reason"] == rs)
        if w or ll:
            print(f"  {rs:7} {w:4d} | {ll:4d}")

    # which ENTRY feature separates winners from losers?
    feats = ["rsi", "stoch", "wvf", "adx", "di_with_trade", "bb_pos",
             "morn_range_pct", "ext_vwap_pct", "ext_sma_pct", "pct_otm", "choppy"]
    loss = [r["loss"] for r in rows]
    print("\nENTRY FEATURE  win-mean  loss-mean   point-biserial r (|r| ranks the separator):")
    ranked = []
    for f in feats:
        wm = st.mean([r[f] for r in win]); lm = st.mean([r[f] for r in los])
        r = _pbr([row[f] for row in rows], loss)
        ranked.append((abs(r), f, wm, lm, r))
    for ar, f, wm, lm, r in sorted(ranked, reverse=True):
        print(f"  {f:14} {wm:8.2f} {lm:9.2f}    r={r:+.3f}")

    # attribution: are losers trend days?
    print("\nATTRIBUTION (outcome, not a filter):")
    print(f"  |day move|  win {st.mean([abs(r['day_move_pct']) for r in win]):.2f}%  "
          f"loss {st.mean([abs(r['day_move_pct']) for r in los]):.2f}%")
    print(f"  adverse move win {st.mean([r['adverse_pct'] for r in win]):.2f}%  "
          f"loss {st.mean([r['adverse_pct'] for r in los]):.2f}%")
    print(f"  breached short: win {sum(1 for r in win if r['through_pts']>0)/len(win)*100:.0f}%  "
          f"loss {sum(1 for r in los if r['through_pts']>0)/len(los)*100:.0f}%")

    print("\nWORST 15 DAYS:")
    print(f"  {'date':11} {'pnl':>6} {'reason':7} {'side':12} {'adx':>5} {'rsi':>5} {'stoch':>6} "
          f"{'extVWAP':>8} {'daymove':>8} {'thru':>6}")
    for r in sorted(rows, key=lambda x: x["pnl"])[:15]:
        print(f"  {r['date']:11} {r['pnl']:+6.0f} {r['reason']:7} {r['side']:12} "
              f"{r['adx']:5.1f} {r['rsi']:5.1f} {r['stoch']:6.1f} {r['ext_vwap_pct']:+8.2f} "
              f"{r['day_move_pct']:+8.2f} {r['through_pts']:6.1f}")
    return ranked


def test_filter(rows, key, grid, label):
    """Post-hoc: skip days where feature < threshold, re-score IS/OOS. Discipline:
    a real missing piece IMPROVES both samples' worst-day AND mean/t — not just trades less."""
    from backend.app.quant_utils import newey_west_tstat
    print(f"\n{label} (skip days where {key} < X) — does cutting them actually help net?")
    print(f"  {'X':>5} {'kept':>5} | {'IS mean':>7} {'IS t':>6} {'IS worst':>8} | "
          f"{'OOS mean':>8} {'OOS t':>6} {'OOS worst':>9}")
    for X in grid:
        kept = [r for r in rows if r[key] >= X]
        byd = defaultdict(float)
        for r in kept:
            byd[r["date"]] += r["pnl"]
        isd = [v for k, v in sorted(byd.items()) if k < OOS_START]
        ood = [v for k, v in sorted(byd.items()) if k >= OOS_START]
        if len(isd) < 2 or len(ood) < 2:
            print(f"  {X:5.2f} {len(kept):5d} | too few"); continue
        it = newey_west_tstat(isd); ot = newey_west_tstat(ood)
        tag = "  <- baseline" if X == grid[0] else ""
        print(f"  {X:5.2f} {len(kept):5d} | {st.mean(isd):+7.0f} {it['t']:+6.2f} {min(isd):+8.0f} | "
              f"{st.mean(ood):+8.0f} {ot['t']:+6.2f} {min(ood):+9.0f}{tag}")


if __name__ == "__main__":
    rows = run()
    report(rows)
    test_filter(rows, "pct_otm", [0.0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 1.0], "MIN-CUSHION FILTER")
    test_filter(rows, "adx", [0.0, 10, 15, 20, 25, 30], "ADX-FLOOR FILTER (secondary)")
