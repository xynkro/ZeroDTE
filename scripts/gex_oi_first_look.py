#!/usr/bin/env python3
"""GEX/OI dealer-positioning FIRST LOOK (scheduled checkpoint, 2026-07-24).

Question (from HANDOFF): do dealer-positioning levels (GEX walls, gamma pins, max-pain,
OI-anchored strikes) separate WAVE winners from losers where the price oscillators
(RSI/Stoch/VWAP/ADX) did NOT? Methodology mirrors scripts/wave_failure_analysis.py:
point-biserial separation + IS/OOS-style filter test. READ-ONLY — no trade path touched.

Data plumbing:
  - Dealer features: backend/data/gex_history.jsonl `oi` blocks, snapshot nearest 10:00 ET.
  - WAVE win/loss: the validated band-anchored config is NOT wired live (debrief_log is
    100% MEIC), so per the fallback we run scripts.wave_band_backtest.main VERBATIM on a
    5m SPX series that COVERS the OI window. The committed SPX_5m_3y.json ends 2026-05-14;
    set WAVE_BARS_JSON to a refreshed SPY->SPX 5m file (2022 -> today) covering the window
    (regenerate with backend/scripts/extend_historical_data.py into a scratch path — do NOT
    overwrite the live file if you want to keep this read-only).

Run:
  WAVE_BARS_JSON=/path/to/SPX_5m_ext.json PYTHONPATH=. .venv/bin/python scripts/gex_oi_first_look.py
"""
from __future__ import annotations
import json, os, sys, statistics as st
from datetime import datetime
from collections import defaultdict
from pathlib import Path
import numpy as np

import backend.app.config  # noqa: F401
from backend.app.config import settings
import scripts.wave_band_backtest as wb

TARGET_MIN = 600  # 10:00 ET
GEX = settings.data_dir / "gex_history.jsonl"


def _load_bars():
    path = Path(os.environ.get("WAVE_BARS_JSON", settings.data_dir / "historical" / "SPX_5m_3y.json"))
    raw = json.loads(path.read_text())
    return [(datetime.fromisoformat(r["datetime"]), r["high"], r["low"], r["close"]) for r in raw]


def run_cfg(**kw):
    diag = []
    wb.main(diag=diag, **kw)
    return {d["date"]: d for d in diag}


def _et_min(s):
    h, m = s["et"].split(":")
    return int(h) * 60 + int(m)


def _pick_10am(rows):
    cand = [r for r in rows if 9 * 60 <= _et_min(r) <= 16 * 60 + 30] or rows
    return min(cand, key=lambda r: abs(_et_min(r) - TARGET_MIN))


def _dealer_features(snap):
    oi = snap["oi"]
    spot = oi.get("spot_at") or snap.get("spot")
    calls = [c["k"] for c in oi.get("hi_oi_calls", []) if c["k"] > spot]
    puts = [p["k"] for p in oi.get("hi_oi_puts", []) if p["k"] < spot]
    negs = oi.get("neg_gamma_strikes") or []
    return dict(spot=spot, max_pain=oi.get("max_pain"),
                call_wall=min(calls) if calls else None,      # nearest hi-OI call ABOVE spot
                put_wall=max(puts) if puts else None,          # nearest hi-OI put BELOW spot
                neg_near=min(negs, key=lambda k: abs(k - spot)) if negs else None,
                gamma_flip=oi.get("gamma_flip"), gamma_reg=snap.get("regime"),
                net_gex_b=snap.get("net_gex_b"), net_ratio=snap.get("net_ratio"),
                et=snap["et"])


def _pbr(feat, loss):
    x = np.array(feat, float); y = np.array(loss, float)
    m = ~np.isnan(x)
    if m.sum() < 3:
        return float("nan"), int(m.sum())
    x, y = x[m], y[m]
    if x.std() == 0 or y.std() == 0:
        return 0.0, int(m.sum())
    return float(np.corrcoef(x, y)[0, 1]), int(m.sum())


def merge(cfg, feat_by_day):
    rows = []
    for dt, f in feat_by_day.items():
        if dt not in cfg:
            continue
        w = cfg[dt]; spot = f["spot"]; short = w["short"]; down = w["down"]
        pct = lambda a, b: 100.0 * (a - b) / spot if (a is not None and b is not None) else None
        d_callwall = pct(f["call_wall"], spot) if f["call_wall"] else None
        d_putwall = pct(spot, f["put_wall"]) if f["put_wall"] else None
        short_inside_wall = short_vs_wall_pts = None
        if not down and f["call_wall"]:
            short_inside_wall = 1.0 if short < f["call_wall"] else 0.0
            short_vs_wall_pts = f["call_wall"] - short
        elif down and f["put_wall"]:
            short_inside_wall = 1.0 if short > f["put_wall"] else 0.0
            short_vs_wall_pts = short - f["put_wall"]
        mp = f["max_pain"]; short_beyond_mp = None
        if mp is not None:
            short_beyond_mp = 1.0 if (short > mp if not down else short < mp) else 0.0
        rows.append(dict(date=dt, pnl=w["pnl"], loss=1.0 if w["pnl"] < 0 else 0.0,
            reason=w["reason"], side=w["side"], down=down, short=short, credit=w["credit"],
            worst_through=w["worst_through"], spot=spot,
            d_maxpain=pct(spot, mp), d_callwall=d_callwall, d_putwall=d_putwall,
            short_inside_wall=short_inside_wall, short_vs_wall_pts=short_vs_wall_pts,
            short_beyond_mp=short_beyond_mp, gamma_pos=1.0 if f["gamma_reg"] == "positive" else 0.0,
            net_gex_b=f["net_gex_b"], net_ratio=f["net_ratio"]))
    return rows


def main():
    wb._load = _load_bars  # run the VALIDATED band logic on the window-covering series
    val = run_cfg(regime="released", bb_len=14, bb_mult=2.5)   # validated
    base = run_cfg(regime="all", bb_len=14, bb_mult=2.5)       # un-gated (loss variance)

    snaps = defaultdict(list)
    for line in open(GEX):
        line = line.strip()
        if not line:
            continue
        d = json.loads(line)
        if d.get("oi"):
            snaps[d["date"]].append(d)
    # drop weekend-artifact days (<5 snaps) — keep real trading sessions
    feat_by_day = {dt: _dealer_features(_pick_10am(rows)) for dt, rows in snaps.items() if len(rows) >= 5}

    vr = merge(val, feat_by_day)
    br = merge(base, feat_by_day)
    nlos_v = sum(int(r["loss"]) for r in vr)
    nlos_b = sum(int(r["loss"]) for r in br)

    print(f"OI trading-days: {len(feat_by_day)}")
    print(f"VALIDATED (released, BB14/2.5): traded {len(vr)} | win {len(vr)-nlos_v} | loss {nlos_v} "
          f"| ${sum(r['pnl'] for r in vr):+,.0f}")
    if nlos_v == 0:
        print("  -> separation test UNDEFINED: no loss variance under validated config.")
    print(f"BASELINE  (all days, BB14/2.5): traded {len(br)} | win {len(br)-nlos_b} | loss {nlos_b} "
          f"| ${sum(r['pnl'] for r in br):+,.0f}")
    print(f"  breaches (worst_through>0): {sum(1 for r in br if r['worst_through'] > 0)}")

    print("\nDEALER FEATURE vs LOSS (point-biserial r; baseline set):")
    loss = [r["loss"] for r in br]
    feats = ["d_maxpain", "d_callwall", "d_putwall", "short_inside_wall",
             "short_vs_wall_pts", "short_beyond_mp", "gamma_pos", "net_gex_b", "net_ratio"]
    for ar, fk, r, n in sorted(((abs(x) if x == x else -1, fk, x, n)
                                for fk, (x, n) in ((fk, _pbr([row[fk] for row in br], loss))
                                                   for fk in feats)), reverse=True):
        print(f"  {fk:18} r={r:+.3f} (n={n})")
    if nlos_b <= 1:
        print(f"\n  CAVEAT: n_loss={nlos_b} -> every r is driven by <=1 day. Not interpretable.")
        print("  Verdict: (b) nothing yet — keep logging; re-check when loss-days accrue (~40+ sessions).")


if __name__ == "__main__":
    sys.exit(main())
