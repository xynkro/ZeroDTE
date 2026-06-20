#!/usr/bin/env python3
"""MEIC MAE/MFE excursion analysis — the honest, dataset-driven version of the
"maximum adverse / favorable exposure" idea, measured on BS-repriced condors
over the REALIZED holding window of the LIVE config (per-condor breakeven stop,
ride-to-expiry otherwise).

It answers, with distributions instead of a guessed parameter:
  • MFE — how much profit was on the table at the peak (as % of credit)?
  • Give-back — how much of that peak did riding-to-expiry hand back?
  • MAE — how deep toward breach did the eventual WINNERS dip (is the breakeven
    stop too tight — knocking out condors that would have recovered)?
  • The MAE × MFE matrix — mean realized P&L by adverse-decile × favorable-decile,
    the terminal version of the video's visual matrix.

This is the excursion lens on the same TP question scripts/meic_tp_timestop_sweep.py
answers by brute force — here you see WHY a TP does or doesn't help.

Run: PYTHONPATH=. .venv/bin/python scripts/meic_excursion.py
"""
from __future__ import annotations

import statistics as st

import backend.app.config  # noqa: F401 — load .env
from backend.app.config import settings
from scripts.meic_backtest import run_meic


def _pct(xs: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in 0..100)."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def _dist_line(label: str, xs: list[float], unit: str = "%") -> str:
    if not xs:
        return f"  {label:<22} (none)"
    return (f"  {label:<22} p10 {_pct(xs,10):+6.1f}{unit} | p25 {_pct(xs,25):+6.1f}{unit} | "
            f"med {_pct(xs,50):+6.1f}{unit} | p75 {_pct(xs,75):+6.1f}{unit} | "
            f"p90 {_pct(xs,90):+6.1f}{unit} | mean {st.mean(xs):+6.1f}{unit}")


def analyze(entries: list[dict], label: str):
    n = len(entries)
    if not n:
        print(f"{label}: NO ENTRIES")
        return
    # Normalize excursions to % of credit so condors of different richness compare.
    mfe_pct = [e["mfe"] / e["credit"] * 100.0 for e in entries]
    mae_pct = [e["mae"] / e["credit"] * 100.0 for e in entries]
    gross_pct = [e["gross"] / e["credit"] * 100.0 for e in entries]

    winners = [e for e in entries if e["gross"] > 0]
    losers = [e for e in entries if e["gross"] <= 0]

    print(f"=== {label} ===")
    print(f"  condors {n} | winners {len(winners)} ({len(winners)/n*100:.0f}%) | "
          f"losers {len(losers)} ({len(losers)/n*100:.0f}%)")
    print()
    print("  EXCURSION (% of entry credit, over realized holding window):")
    print(_dist_line("MFE peak-profit", mfe_pct))
    print(_dist_line("MAE peak-drawdown", mae_pct))
    print(_dist_line("realized gross P&L", gross_pct))
    print()

    # GIVE-BACK: among winners, how much of the peak profit did ride-to-expiry
    # hand back? capture = realized/MFE. Low capture ⇒ a take-profit recoups it.
    cap = [e["gross"] / e["mfe"] * 100.0 for e in winners if e["mfe"] > 1e-9]
    giveback = [(e["mfe"] - e["gross"]) / e["credit"] * 100.0 for e in winners if e["mfe"] > 1e-9]
    if cap:
        print("  GIVE-BACK (winners that saw positive MFE):")
        print(f"    capture ratio (realized/MFE):  med {_pct(cap,50):.0f}% | mean {st.mean(cap):.0f}%")
        print(f"    profit handed back (% credit): med {_pct(giveback,50):.1f}% | mean {st.mean(giveback):.1f}%")
        # What a clean take-profit at the peak's typical level would have locked.
        for tp in (40, 50, 60, 70, 80):
            # condors whose MFE reached ≥ tp% of credit would have filled a TP there
            hit = [e for e in entries if e["mfe"] / e["credit"] * 100.0 >= tp]
            print(f"    MFE reached ≥{tp:>2}% credit: {len(hit)/n*100:4.0f}% of condors")
        print()

    # MAE on WINNERS — did the eventual winners dip deep toward breach first?
    # If many winners hit a deep MAE, a tight breakeven stop knocks them out early.
    w_mae = [e["mae"] / e["credit"] * 100.0 for e in winners]
    if w_mae:
        print("  WINNER MAE (how deep winners dipped before recovering):")
        print(_dist_line("winner MAE", w_mae))
        deep = sum(1 for x in w_mae if x <= -100.0)  # dipped to ≥ full-credit buyback
        print(f"    {deep}/{len(winners)} winners ({deep/len(winners)*100:.0f}%) dipped to ≤ -100% "
              f"credit (breakeven-stop territory) yet still settled green")
        print()

    _matrix(entries)
    print()


def _matrix(entries: list[dict]):
    """Terminal MAE×MFE matrix: mean realized P&L ($SPX) per adverse×favorable cell."""
    # Quintile edges on the two excursion axes.
    mae_vals = sorted(e["mae"] / e["credit"] * 100.0 for e in entries)
    mfe_vals = sorted(e["mfe"] / e["credit"] * 100.0 for e in entries)

    def edges(vals):
        return [_pct(vals, q) for q in (20, 40, 60, 80)]

    mae_e, mfe_e = edges(mae_vals), edges(mfe_vals)

    def bucket(v, e):
        for i, t in enumerate(e):
            if v <= t:
                return i
        return len(e)

    grid: dict[tuple[int, int], list[float]] = {}
    for e in entries:
        r = bucket(e["mae"] / e["credit"] * 100.0, mae_e)   # 0=most adverse
        c = bucket(e["mfe"] / e["credit"] * 100.0, mfe_e)   # 0=least favorable
        grid.setdefault((r, c), []).append(e["pnl"])

    print("  MAE × MFE MATRIX — mean realized P&L ($SPX-scale) per cell, n in (·):")
    print("    rows = MAE quintile (top = most adverse) · cols = MFE quintile (right = most favorable)")
    hdr = "        " + "".join(f"  MFE-Q{c+1} " for c in range(5))
    print(hdr)
    for r in range(5):
        cells = []
        for c in range(5):
            xs = grid.get((r, c))
            if xs:
                cells.append(f"{st.mean(xs):+6.0f}({len(xs):>3})")
            else:
                cells.append("    -     ")
        print(f"    MAE-Q{r+1} " + " ".join(cells))


if __name__ == "__main__":
    ladder = [s.strip() for s in settings.MEIC_ENTRY_TIMES_ET.split(",")]
    print(f"MEIC excursion analysis | live config: {settings.EOD_IC_SHORT_DELTA:.2f}Δ "
          f"${settings.EOD_IC_WING_DOLLARS:.0f}-wing | breakeven stop, ride-to-expiry\n")
    # LIVE config = breakeven stop (buffer from .env), no TP, no time-stop.
    buf = getattr(settings, "IC_STOP_BUFFER", 1.0)
    entries = run_meic(ladder, cost_rt=50.0, stop_buffer=buf)
    analyze(entries, f"LIVE LADDER {ladder} (stop ×{buf})")
