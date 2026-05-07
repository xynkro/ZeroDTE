"""Backtest the ZeroDTE Range Predictor on 60d of SPX 5-min data.

Two questions answered per session:
  Q1: Did the projected range HOLD (= iron condor would profit)?
  Q2: When a sell-call/put signal fired, did the credit spread profit?

Q2 simulates: at signal time, sell a credit spread with short strike at the
projected boundary and long strike 5pts further OTM. Track if price stays
"on the right side" of the short strike for the rest of the session.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.predictor import Bar, run_backtest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_PATH = PROJECT_ROOT / "backend" / "data" / "historical" / "SPX_5m_60d.json"
OUT_PATH = PROJECT_ROOT / "backend" / "data" / "backtest_results" / "predictor_validation.json"

# Credit-spread proxy params (for "did the signal pay?" check)
SPREAD_WIDTH = 5.0  # short strike + 5 long strike (typical 0DTE small wing)
CREDIT_PER_SPREAD = 1.50  # assumed credit collected (mid 0DTE for ~10-delta short, very rough)
PROFIT_TARGET_PCT = 0.30  # close at 30% of credit (= $0.45 buyback if collected $1.50)


def load_bars(path: Path) -> list[Bar]:
    raw = json.loads(path.read_text())
    bars = []
    for r in raw:
        bars.append(Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        ))
    return bars


def compute_d1_atr(bars: list[Bar], period: int = 14) -> dict[str, float]:
    """Pre-compute D1 ATR(14) per session date.

    Aggregates 5-min bars into D1 OHLC per session, then runs Wilder ATR.
    Returns {session_date_iso: prior-day ATR}. Uses the ATR from the COMPLETED
    PRIOR session (no lookahead).
    """
    by_date: dict[str, list[Bar]] = {}
    for b in bars:
        d = b.time.strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)
    sorted_dates = sorted(by_date.keys())
    daily = []
    for d in sorted_dates:
        ses = by_date[d]
        daily.append({
            "date": d,
            "high": max(x.high for x in ses),
            "low": min(x.low for x in ses),
            "close": ses[-1].close,
        })
    # Wilder ATR
    n = len(daily)
    tr = []
    for i in range(n):
        if i == 0:
            tr.append(daily[i]["high"] - daily[i]["low"])
        else:
            tr.append(max(
                daily[i]["high"] - daily[i]["low"],
                abs(daily[i]["high"] - daily[i - 1]["close"]),
                abs(daily[i]["low"]  - daily[i - 1]["close"]),
            ))
    atr_vals = [float("nan")] * n
    if n > period:
        atr_vals[period] = float(np.mean(tr[1:period + 1]))
        for i in range(period + 1, n):
            atr_vals[i] = (atr_vals[i - 1] * (period - 1) + tr[i]) / period
    out = {}
    for i, d in enumerate(daily):
        # Use PRIOR day's ATR for today's classification (no lookahead)
        if i > 0 and not np.isnan(atr_vals[i - 1]):
            out[d["date"]] = atr_vals[i - 1]
    return out


def evaluate_signal_outcome(signal, post_signal_bars: list[Bar]) -> dict:
    """Did this signal's credit spread profit?

    Simulation:
      - sell_call: short strike = proj_high (rounded to nearest 5), long strike = +5
      - sell_put:  short strike = proj_low  (rounded to nearest 5), long strike = -5
      - Outcome:
        * MAX PROFIT if at session close, price is "safe side" of short strike
        * STOPPED if intra-session price BREACHES short strike
        * PARTIAL_PROFIT if price stays safe but doesn't reach 30% target —
          we'll mark as "profit" since 0DTE expiring OTM = full credit kept
    """
    if not post_signal_bars:
        return {"outcome": "no_data", "pnl_per_spread": 0.0}

    if signal.side == "sell_call_cs":
        short_strike = round(signal.proj_high / 5) * 5
        # If short_strike <= entry, recompute to next valid strike above current
        if short_strike <= signal.entry_price:
            short_strike = (int(signal.entry_price / 5) + 1) * 5
        # Breach = any high >= short_strike
        breached = any(b.high >= short_strike for b in post_signal_bars)
        # Hit 30% target = price moved enough away from short_strike that
        # estimated remaining premium <= 70% of original. Approximation:
        # assume linear time decay; if price drops by 1× SPREAD_WIDTH = max profit.
        favorable = post_signal_bars[-1].close < short_strike - 0.3 * SPREAD_WIDTH
        if breached:
            return {"outcome": "BREACH", "pnl_per_spread": -(SPREAD_WIDTH - CREDIT_PER_SPREAD),
                    "short_strike": short_strike}
        elif favorable:
            return {"outcome": "TARGET_HIT_or_OTM", "pnl_per_spread": CREDIT_PER_SPREAD * PROFIT_TARGET_PCT,
                    "short_strike": short_strike}
        else:
            return {"outcome": "OTM_FULL_CREDIT", "pnl_per_spread": CREDIT_PER_SPREAD,
                    "short_strike": short_strike}
    else:  # sell_put_cs
        short_strike = round(signal.proj_low / 5) * 5
        if short_strike >= signal.entry_price:
            short_strike = (int(signal.entry_price / 5)) * 5
        breached = any(b.low <= short_strike for b in post_signal_bars)
        favorable = post_signal_bars[-1].close > short_strike + 0.3 * SPREAD_WIDTH
        if breached:
            return {"outcome": "BREACH", "pnl_per_spread": -(SPREAD_WIDTH - CREDIT_PER_SPREAD),
                    "short_strike": short_strike}
        elif favorable:
            return {"outcome": "TARGET_HIT_or_OTM", "pnl_per_spread": CREDIT_PER_SPREAD * PROFIT_TARGET_PCT,
                    "short_strike": short_strike}
        else:
            return {"outcome": "OTM_FULL_CREDIT", "pnl_per_spread": CREDIT_PER_SPREAD,
                    "short_strike": short_strike}


def main():
    print("=" * 100)
    print("  ZeroDTE Range Predictor — backtest on 60d SPX 5-min")
    print("=" * 100)

    bars = load_bars(DATA_PATH)
    print(f"\nLoaded {len(bars):,} bars: {bars[0].time} → {bars[-1].time}")

    atr_map = compute_d1_atr(bars)
    print(f"Pre-computed D1 ATR for {len(atr_map)} sessions")

    def atr_lookup(sid: str):
        return atr_map.get(sid)

    sessions = run_backtest(bars, atr_lookup)
    print(f"\nProcessed {len(sessions)} sessions with valid observation windows.")

    # ===== Q1: Did projected range HOLD (iron condor proxy) =====
    n_total = len(sessions)
    n_volatile = sum(1 for s in sessions if s.regime == "VOLATILE")
    n_nonvolatile = n_total - n_volatile

    nonvol_sessions = [s for s in sessions if s.regime == "NON-VOLATILE"]
    n_both_held = sum(1 for s in nonvol_sessions if s.both_held)
    n_high_held = sum(1 for s in nonvol_sessions if s.proj_high_held)
    n_low_held = sum(1 for s in nonvol_sessions if s.proj_low_held)

    print("\n##### Q1: PROJECTED RANGE HIT-RATE (iron condor proxy) #####\n")
    print(f"  Total sessions:        {n_total}")
    print(f"  Classified VOLATILE:   {n_volatile} (skipped — would not iron condor)")
    print(f"  Classified NON-VOL:    {n_nonvolatile} (would deploy iron condor)")
    if n_nonvolatile > 0:
        print(f"  Both bounds held:      {n_both_held}/{n_nonvolatile} = {100.0*n_both_held/n_nonvolatile:.1f}%  (IC profits)")
        print(f"  Upper bound held:      {n_high_held}/{n_nonvolatile} = {100.0*n_high_held/n_nonvolatile:.1f}%")
        print(f"  Lower bound held:      {n_low_held}/{n_nonvolatile} = {100.0*n_low_held/n_nonvolatile:.1f}%")

    # ===== Q2: Per-signal outcome (credit spread proxy) =====
    print("\n##### Q2: PER-SIGNAL OUTCOME (credit spread proxy) #####\n")
    sigs_with_outcome = []
    for s in sessions:
        if s.regime == "VOLATILE":
            continue
        # Map session bars by index
        session_bars_full = [b for b in bars if b.time.strftime("%Y-%m-%d") == s.session_date]
        for sig in s.signals:
            after = [b for b in session_bars_full if b.time > sig.time]
            outcome = evaluate_signal_outcome(sig, after)
            sigs_with_outcome.append({
                "date": s.session_date,
                "side": sig.side,
                "time": sig.time.isoformat(),
                "regime": s.regime,
                "rsi": sig.rsi,
                "wvf_spike": sig.wvf_spike,
                "entry_price": sig.entry_price,
                "proj_high": sig.proj_high,
                "proj_low": sig.proj_low,
                "outcome": outcome.get("outcome"),
                "short_strike": outcome.get("short_strike"),
                "pnl_per_spread": outcome.get("pnl_per_spread", 0.0),
            })

    n_signals = len(sigs_with_outcome)
    by_side = defaultdict(list)
    for x in sigs_with_outcome:
        by_side[x["side"]].append(x)

    if n_signals == 0:
        print("  No signals fired in the 60-day window. Indicator may be too restrictive,")
        print("  OR the SPX regime in this window has been mostly trending (volatile classification).")
    else:
        print(f"  Total signals fired: {n_signals}")
        for side in ("sell_call_cs", "sell_put_cs"):
            xs = by_side[side]
            if not xs:
                continue
            outcomes = Counter(x["outcome"] for x in xs)
            total_pnl = sum(x["pnl_per_spread"] for x in xs)
            wins = sum(1 for x in xs if x["pnl_per_spread"] > 0)
            wr = 100.0 * wins / len(xs)
            print(f"\n  {side}: n={len(xs)}, WR={wr:.1f}%, total P&L (per spread × n)=${total_pnl:.2f}")
            for k, v in outcomes.items():
                print(f"     {k}: {v}")

    # ===== STRONG signals (WVF-confirmed) =====
    strong_sigs = [x for x in sigs_with_outcome if x["wvf_spike"]]
    if strong_sigs:
        wins = sum(1 for x in strong_sigs if x["pnl_per_spread"] > 0)
        wr = 100.0 * wins / len(strong_sigs)
        total = sum(x["pnl_per_spread"] for x in strong_sigs)
        print(f"\n  WVF-CONFIRMED signals only: n={len(strong_sigs)}, WR={wr:.1f}%, P&L=${total:.2f}")

    # ===== Save JSON =====
    out = {
        "summary": {
            "n_sessions": n_total,
            "n_volatile": n_volatile,
            "n_nonvolatile": n_nonvolatile,
            "n_both_held": n_both_held,
            "n_high_held": n_high_held,
            "n_low_held": n_low_held,
            "n_signals": n_signals,
        },
        "sessions": [
            {
                "date": s.session_date, "regime": s.regime,
                "proj_high": s.proj_high, "proj_low": s.proj_low,
                "obs_high": s.obs_high, "obs_low": s.obs_low,
                "session_high": s.session_high, "session_low": s.session_low,
                "session_close": s.session_close,
                "proj_high_held": s.proj_high_held,
                "proj_low_held": s.proj_low_held,
                "both_held": s.both_held,
                "n_signals": len(s.signals),
            }
            for s in sessions
        ],
        "signals": sigs_with_outcome,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, indent=2, default=str))
    print(f"\nSaved JSON: {OUT_PATH}")


if __name__ == "__main__":
    sys.exit(main())
