#!/usr/bin/env python3
"""VIX-up-at-open filter — does selling only on days VIX gaps UP at the open beat
selling every day? (Hypothesis lifted from OptionsPlay's "#1 backtested 0DTE"
video: VIX up at 9:30 vs prior close = sell, VIX down = skip.)

LOOKAHEAD-SAFE: the signal is VIX_open[today] vs VIX_close[prior] — both knowable
at 09:30 before any entry. We tag every honest-backtest trade with that day's
"VIX up?" flag and compare selling-every-day vs only-VIX-up vs only-VIX-down,
per book (PUT / CALL / COMBINED), on n / win-rate / breach / mean / t-stat AND
worst single DAY (the two criteria VALIDATION_CRITERIA judges on).

Red-team note: the opposite result (VIX-DOWN / calm-grind days are the premium
seller's friend) is fully reported — we are testing the claim, not confirming it.

Run: PYTHONPATH=. .venv/bin/python scripts/vix_up_validation.py
"""
from __future__ import annotations

import math
import statistics as st
from collections import defaultdict

import yfinance as yf

import backend.app.config  # noqa: F401 — loads .env
from backend.app.honest_backtest import run_honest_backtest


def _vix_open_vs_prior_close(start="2022-01-01", end="2026-06-15") -> dict:
    """day -> (vix_open_today, vix_prior_close, gap=open-prior_close). The gap is
    knowable at 09:30 (it uses today's OPEN print and yesterday's CLOSE)."""
    vx = yf.Ticker("^VIX").history(start=start, end=end)
    o = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in vx["Open"].items()}
    c = {ts.strftime("%Y-%m-%d"): float(v) for ts, v in vx["Close"].items()}
    days = sorted(c)
    out = {}
    for i in range(1, len(days)):
        d, pc = days[i], days[i - 1]
        if d in o and pc in c:
            out[d] = (o[d], c[pc], o[d] - c[pc])
    return out


def _stat(ts: list[dict]) -> dict:
    """Per-trade economics + worst single DAY (trades grouped by date)."""
    ps = [t["pnl"] for t in ts]
    n = len(ps)
    if not n:
        return {"n": 0, "txt": f"{'—':>52}"}
    w = sum(1 for p in ps if p > 0)
    br = sum(1 for t in ts if t.get("outcome") in ("breach", "stop_loss"))
    mean = st.mean(ps)
    sd = st.pstdev(ps) if n > 1 else 0.0
    tt = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    by_day = defaultdict(float)
    for t in ts:
        by_day[t["date"][:10]] += t["pnl"]
    worst_day = min(by_day.values())
    return {
        "n": n, "tot": sum(ps), "wr": w / n * 100, "br": br / n * 100,
        "mean": mean, "t": tt, "worst_day": worst_day,
        "txt": (f"n={n:<4} tot={round(sum(ps)):>+7} WR={round(w/n*100):>3}% "
                f"br={round(br/n*100):>3}% mean={mean:>+7.0f} t={tt:>5.2f} "
                f"worstDay={round(worst_day):>+7}"),
    }


def main() -> None:
    r = run_honest_backtest(target_delta=30, final_tp_target=90.0, use_dynamic_stops=False,
                            data_window="max", return_trades=True, skew_enabled=True)
    trades = r["trades"]
    vix = _vix_open_vs_prior_close()

    def up_of(t):
        v = vix.get(t["date"][:10])
        return None if v is None else (v[2] > 0)  # gap > 0 → VIX up at open

    tagged = [t for t in trades if up_of(t) is not None]
    miss = len(trades) - len(tagged)
    d0, d1 = min(t["date"][:10] for t in trades), max(t["date"][:10] for t in trades)

    # Qualifying fraction (of distinct trade-DAYS, how many were VIX-up)
    day_up = {t["date"][:10]: up_of(t) for t in tagged}
    n_days = len(day_up)
    n_up_days = sum(1 for v in day_up.values() if v)

    print(f"\n{len(trades)} trades, {d0}..{d1}, 30Δ/TP90/skew-on, VIX_open vs prior close")
    print(f"tagged {len(tagged)} (missing VIX {miss}) | trade-days {n_days}: "
          f"{n_up_days} VIX-UP ({n_up_days/n_days*100:.0f}%), {n_days-n_up_days} VIX-DOWN\n")

    rows = [("PUT", "sell_put_cs"), ("CALL", "sell_call_cs"), ("COMBINED", None)]
    for name, key in rows:
        sub = [t for t in tagged if key is None or t["side"] == key]
        allp = _stat(sub)
        up = _stat([t for t in sub if up_of(t)])
        dn = _stat([t for t in sub if not up_of(t)])
        print(f"=== {name} BOOK ===")
        print(f"  ALL days   {allp['txt']}")
        print(f"  VIX-UP     {up['txt']}   <- OptionsPlay says SELL these")
        print(f"  VIX-DOWN   {dn['txt']}   <- OptionsPlay says SKIP these")
        # Decision: does gating to VIX-up improve BOTH t-stat AND worst-day?
        if up["n"] and dn["n"]:
            dt = up["t"] - allp["t"]
            dworst = up["worst_day"] - allp["worst_day"]
            verdict = ("GATE HELPS (t↑ and worst-day↑)" if dt > 0 and dworst > 0
                       else "GATE HURTS (t↓ or worst-day↓)" if dt < 0 or dworst < 0
                       else "neutral")
            print(f"  → gate-to-VIX-up vs ALL: Δt={dt:+.2f}, Δworst-day={dworst:+.0f}  ⇒ {verdict}")
        print()

    # ── OUT-OF-SAMPLE robustness: does VIX-up beat VIX-down in BOTH halves? ──
    # Chronological 50/50 split. If the gap only shows in-sample (first half) it's
    # curve-fit; if it holds in the held-out second half it's a real regime effect.
    by_date = sorted({t["date"][:10] for t in tagged})
    cut = by_date[len(by_date) // 2]
    print("=== OUT-OF-SAMPLE (combined book, chronological halves) ===")
    print(f"  split at {cut}")
    for half, lo, hi in [("IN-SAMPLE  (1st half)", by_date[0], cut),
                         ("OUT-SAMPLE (2nd half)", cut, "9999")]:
        seg = [t for t in tagged if lo <= t["date"][:10] < hi]
        up = _stat([t for t in seg if up_of(t)])
        dn = _stat([t for t in seg if not up_of(t)])
        holds = "✓ VIX-up still wins" if up["n"] and dn["n"] and up["mean"] > dn["mean"] else "✗ does NOT hold"
        print(f"  {half}")
        print(f"    VIX-UP    {up['txt']}")
        print(f"    VIX-DOWN  {dn['txt']}   {holds}")
    print()

    # ── Disentangle CHANGE (VIX-up) from LEVEL (VIX prior-close, already gated) ──
    # If VIX-up only separates in the LOW-level cell, it's incremental to your level
    # gate; if it vanishes once you condition on level, it's just a vol-regime proxy.
    pc = {d: v[1] for d, v in vix.items()}          # prior-close VIX per day
    lv = sorted(pc[t["date"][:10]] for t in tagged)
    med = lv[len(lv) // 2]
    print(f"=== LEVEL × CHANGE 2×2 (combined; prior-close VIX median={med:.1f}; small cells) ===")
    for lname, llo, lhi in [(f"LEVEL <{med:.0f}", 0, med), (f"LEVEL ≥{med:.0f}", med, 999)]:
        cell = [t for t in tagged if llo <= pc[t["date"][:10]] < lhi]
        up = _stat([t for t in cell if up_of(t)])
        dn = _stat([t for t in cell if not up_of(t)])
        print(f"  {lname}")
        print(f"    VIX-UP    {up['txt']}")
        print(f"    VIX-DOWN  {dn['txt']}")
    print()


if __name__ == "__main__":
    main()
