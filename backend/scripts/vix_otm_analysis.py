"""Analyze IC outcomes across %OTM × VIX bucket on 12mo SPX 5m data.

For each session in 2024-11 → 2026-05:
  1. Read 09:45 ET bar close as the IC entry price
  2. Read post-09:45 session H/L/Close
  3. For each candidate %OTM in {0.2, 0.3, 0.5, 0.7, 1.0, 1.5}:
       - Compute short_call/put at entry × (1 ± pct/100)
       - Did session_close fall inside the box?  → GOOD
       - Did either short get tagged intraday but close came back? → ATM
       - Did session_close breach a short? → BAAAAD
  4. Lookup that day's VIX (close from prior day = volatility expectation entering)
  5. Bucket and aggregate

Output: a per-VIX-bucket breach rate / WR table for each %OTM choice.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.predictor import Bar

ET = ZoneInfo("America/New_York")


def load_spx_bars():
    path = ROOT / "data" / "historical" / "SPX_5m_1y.json"
    raw = json.loads(path.read_text())
    bars = []
    for r in raw:
        bars.append(Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        ))
    return bars


def fetch_vix_history(start: str, end: str) -> dict:
    """Daily VIX close keyed by 'YYYY-MM-DD'. Uses prior-day VIX as the
    'expectation entering today' (since today's VIX close is computed at EOD)."""
    print(f"Fetching ^VIX from {start} to {end}...")
    df = yf.download("^VIX", start=start, end=end, interval="1d",
                     progress=False, auto_adjust=False)
    if df is None or df.empty:
        print("WARNING: empty VIX data")
        return {}
    out = {}
    closes = df["Close"].values.flatten()
    for i, idx in enumerate(df.index):
        date_str = idx.strftime("%Y-%m-%d")
        out[date_str] = float(closes[i])
    print(f"  loaded {len(out)} VIX daily closes")
    return out


def vix_for_session(date_str: str, vix_map: dict) -> float | None:
    """Return prior-trading-day VIX (= 'expected vol entering this session').
    Walks back up to 5 days for weekends/holidays."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    for back in range(1, 6):
        prior = (d - timedelta(days=back)).strftime("%Y-%m-%d")
        if prior in vix_map:
            return vix_map[prior]
    return None


def vix_bucket(vix: float) -> str:
    if vix < 13:    return "VIX<13 ultra-low"
    if vix < 15:    return "VIX 13-15 low"
    if vix < 18:    return "VIX 15-18 normal"
    if vix < 22:    return "VIX 18-22 elevated"
    if vix < 28:    return "VIX 22-28 high"
    return "VIX >=28 extreme"


def compute_ic_outcome(short_call: float, short_put: float,
                       long_call: float, long_put: float,
                       sess_high: float, sess_low: float,
                       sess_close: float) -> tuple[str, float]:
    """Return (outcome_label, pnl_per_spread).
    Heuristic credit: 12% of wing per side at 1.0% OTM, scaled by 1/pct_otm
    (closer = more premium).
    """
    if sess_close > short_call:
        return "BAAAAD_call", -1.0  # max loss
    if sess_close < short_put:
        return "BAAAAD_put", -1.0
    if sess_high >= short_call or sess_low <= short_put:
        return "ATM_tagged", +1.0   # still max profit (close inside) but uncomfortable
    return "GOOD", +1.0


def session_outcomes_at_pct(bars_by_day: dict, pct_otm: float, vix_map: dict, wing: float = 10.0):
    """For each session, compute IC outcome at this %OTM. Returns list of dicts."""
    rows = []
    for date_str, day_bars in sorted(bars_by_day.items()):
        # Find 09:45 ET bar (= entry)
        entry_bar = None
        for b in day_bars:
            et = b.time.astimezone(ET) if b.time.tzinfo else b.time
            if et.hour == 9 and et.minute == 45:
                entry_bar = b
                break
        if entry_bar is None:
            continue

        post_bars = []
        for b in day_bars:
            et = b.time.astimezone(ET) if b.time.tzinfo else b.time
            if (et.hour > 9) or (et.hour == 9 and et.minute >= 45):
                if et.hour < 16:
                    post_bars.append(b)
        if not post_bars:
            continue

        entry_price = entry_bar.close
        sess_high = max(b.high for b in post_bars)
        sess_low = min(b.low for b in post_bars)
        sess_close = post_bars[-1].close

        short_call = entry_price * (1 + pct_otm / 100.0)
        short_put = entry_price * (1 - pct_otm / 100.0)
        long_call = short_call + wing
        long_put = short_put - wing

        outcome, pnl_unit = compute_ic_outcome(
            short_call, short_put, long_call, long_put,
            sess_high, sess_low, sess_close,
        )
        # Estimate $ P&L: at 1% OTM = $120 credit, $880 max loss.
        # Scale credit ~ inverse to pct_otm (closer = more premium)
        # Empirical: pct→credit roughly 0.5%→$300, 1.0%→$120, 1.5%→$50
        if pct_otm <= 0.3:
            credit = 450
        elif pct_otm <= 0.5:
            credit = 268
        elif pct_otm <= 0.7:
            credit = 180
        elif pct_otm <= 1.0:
            credit = 120
        elif pct_otm <= 1.5:
            credit = 60
        else:
            credit = 30
        max_loss = wing * 100 - credit
        pnl_dollars = credit if pnl_unit > 0 else -max_loss

        vix = vix_for_session(date_str, vix_map)
        rows.append({
            "date": date_str,
            "vix": vix,
            "vix_bucket": vix_bucket(vix) if vix else "unknown",
            "entry_price": entry_price,
            "short_call": short_call,
            "short_put": short_put,
            "sess_high": sess_high,
            "sess_low": sess_low,
            "sess_close": sess_close,
            "session_move_pct": (sess_close - entry_price) / entry_price * 100,
            "session_range_pct": (sess_high - sess_low) / entry_price * 100,
            "outcome": outcome,
            "pnl": pnl_dollars,
            "credit": credit,
            "max_loss": max_loss,
        })
    return rows


def main():
    print("=" * 80)
    print("VIX × %OTM × IC OUTCOME ANALYSIS")
    print("=" * 80)
    bars = load_spx_bars()

    # Group by ET date
    bars_by_day = {}
    for b in bars:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        date_str = et.strftime("%Y-%m-%d")
        bars_by_day.setdefault(date_str, []).append(b)
    print(f"Loaded {len(bars)} bars across {len(bars_by_day)} sessions")

    # Fetch VIX history covering the same window
    dates_sorted = sorted(bars_by_day.keys())
    vix_map = fetch_vix_history(dates_sorted[0], dates_sorted[-1])

    # Run for each candidate %OTM
    pct_candidates = [0.2, 0.3, 0.5, 0.7, 1.0, 1.5]
    all_results = {}
    for pct in pct_candidates:
        rows = session_outcomes_at_pct(bars_by_day, pct, vix_map)
        all_results[pct] = rows

    # ── Aggregate by VIX bucket × %OTM ──
    print()
    print("=" * 80)
    print("OUTCOME MATRIX: rows = VIX bucket, cols = %OTM")
    print("Each cell: WR% | breach% | avg P&L per session | net per bucket")
    print("=" * 80)
    print()

    bucket_order = [
        "VIX<13 ultra-low",
        "VIX 13-15 low",
        "VIX 15-18 normal",
        "VIX 18-22 elevated",
        "VIX 22-28 high",
        "VIX >=28 extreme",
        "unknown",
    ]
    by_bucket = {b: {} for b in bucket_order}

    for pct in pct_candidates:
        for row in all_results[pct]:
            bucket = row["vix_bucket"]
            by_bucket.setdefault(bucket, {})
            by_bucket[bucket].setdefault(pct, []).append(row)

    # Print header
    header = f"{'VIX BUCKET':<22}" + "  ".join(f"{p}%OTM".center(28) for p in pct_candidates)
    print(header)
    print("-" * len(header))

    for bucket in bucket_order:
        if bucket not in by_bucket or not by_bucket[bucket]:
            continue
        cells = [f"{bucket:<22}"]
        for pct in pct_candidates:
            rows = by_bucket[bucket].get(pct, [])
            if not rows:
                cells.append(f"{'-':^28}")
                continue
            n = len(rows)
            n_good = sum(1 for r in rows if r["outcome"] in ("GOOD", "ATM_tagged"))
            n_bad = n - n_good
            wr = 100 * n_good / n
            avg_pnl = sum(r["pnl"] for r in rows) / n
            total_pnl = sum(r["pnl"] for r in rows)
            cell = f" {wr:>4.0f}% W·{n} | {avg_pnl:+5.0f}/d | net {total_pnl:+6.0f}"
            cells.append(cell.center(28))
        print("  ".join(cells))

    # ── Top "BAAAAD" days analysis (= biggest moves) ──
    print()
    print("=" * 80)
    print("TOP 15 BIGGEST SESSION MOVES — what was VIX entering these days?")
    print("=" * 80)
    biggest = sorted(all_results[0.5], key=lambda r: abs(r["session_move_pct"]), reverse=True)[:15]
    print(f"{'date':>12}  {'vix_in':>7}  {'move%':>7}  {'range%':>7}  {'0.3%OTM':>10}  {'0.5%OTM':>10}  {'1.0%OTM':>10}")
    for row in biggest:
        date_str = row["date"]
        vix = row["vix"] or 0
        move = row["session_move_pct"]
        range_pct = row["session_range_pct"]
        # Find outcomes at different %OTM for this date
        out03 = next((r["outcome"] for r in all_results[0.3] if r["date"] == date_str), "?")
        out05 = next((r["outcome"] for r in all_results[0.5] if r["date"] == date_str), "?")
        out10 = next((r["outcome"] for r in all_results[1.0] if r["date"] == date_str), "?")
        # Shorten labels for display
        def shorten(o):
            return ("GOOD" if o == "GOOD" else
                    "atm" if o == "ATM_tagged" else
                    "BAD" if o.startswith("BAAAAD") else "?")
        print(f"{date_str:>12}  {vix:>7.2f}  {move:>+6.2f}%  {range_pct:>6.2f}%  "
              f"{shorten(out03):>10}  {shorten(out05):>10}  {shorten(out10):>10}")

    # ── Net P&L by bucket × %OTM (= pick the best %OTM per bucket) ──
    print()
    print("=" * 80)
    print("RECOMMENDED %OTM PER VIX BUCKET (highest net P&L wins)")
    print("=" * 80)
    print()
    for bucket in bucket_order:
        if bucket not in by_bucket or not by_bucket[bucket]:
            continue
        best_pct = None
        best_pnl = float('-inf')
        for pct in pct_candidates:
            rows = by_bucket[bucket].get(pct, [])
            if not rows:
                continue
            total = sum(r["pnl"] for r in rows)
            if total > best_pnl:
                best_pnl = total
                best_pct = pct
                best_n = len(rows)
                best_wr = 100 * sum(1 for r in rows if r["outcome"] in ("GOOD", "ATM_tagged")) / len(rows)
        if best_pct:
            print(f"  {bucket:<22}  → use {best_pct}% OTM  ({best_n} sessions, {best_wr:.0f}% WR, net ${best_pnl:+,.0f})")


if __name__ == "__main__":
    main()
