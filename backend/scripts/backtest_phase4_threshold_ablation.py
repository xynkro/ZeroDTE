"""Phase 4 confluence-threshold ablation.

Same Phase 4 setup, varies WAVE_MIN_CONFLUENCE_SCORE from 3 to 6 to find the
sweet spot between trade frequency and per-trade quality.

Usage:
  cd backend && ../.venv/bin/python -m scripts.backtest_phase4_threshold_ablation
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass

sys.path.insert(0, str(ROOT))

# Patch min-confluence in the gospel script's namespace, then re-run
import scripts.backtest_phase1_gospel as bt


def main():
    bt.print = print  # ensure prints flow

    bars = bt.load_spx_bars()
    if not bars:
        print("No bars — aborting")
        return
    dates_sorted = sorted(set(b.time.astimezone(bt.ET).strftime("%Y-%m-%d") for b in bars))
    vix_map = bt.fetch_vix_daily(dates_sorted[0], dates_sorted[-1])
    atr_d1_map = bt.compute_atr_d1(bars)

    print("\n" + "=" * 100)
    print("PHASE 4 CONFLUENCE THRESHOLD ABLATION")
    print("=" * 100)

    results = {}
    for thr in [3, 4, 5]:
        bt.WAVE_MIN_CONFLUENCE_SCORE = thr
        print(f"\nRunning Phase 4 with min confluence = {thr}/6...")
        d = bt.run_one_strategy(bars, atr_d1_map, vix_map, phase=4,
                                label=f"PHASE 4 / min={thr}/6")
        results[thr] = d

    print("\n" + "=" * 100)
    print("THRESHOLD COMPARISON")
    print("=" * 100)
    print(f"{'Min conf':<12}  {'Trades':>7}  {'WR':>6}  {'P&L $':>12}  {'Avg/trade':>12}  {'Sessions':>9}")
    print("-" * 100)
    for thr in [3, 4, 5]:
        d = results[thr]
        print(f"{thr}/6 minimum  {d['n_actual_trades']:>7}  {d['win_rate_pct']:>5}%  "
              f"${d['total_pnl']:>+10,.0f}  ${d['avg_pnl_per_trade']:>+10,.2f}  "
              f"{d['n_sessions']:>9}")
    print()

    # Find sweet spot — best avg per trade with reasonable volume
    best_avg = max(results.items(), key=lambda kv: kv[1]["avg_pnl_per_trade"])
    best_total = max(results.items(), key=lambda kv: kv[1]["total_pnl"])
    print(f"  → Highest avg/trade: min={best_avg[0]}/6 (${best_avg[1]['avg_pnl_per_trade']:+.2f})")
    print(f"  → Highest total $:   min={best_total[0]}/6 (${best_total[1]['total_pnl']:+,.0f})")

    # Scale projections at 10 contracts on active trading days
    print()
    print("Scale to 10 contracts on active trading days:")
    for thr in [3, 4, 5]:
        d = results[thr]
        if d["total_pnl"] > 0 and d["n_sessions"] > 0:
            per_day = d["total_pnl"] / d["n_sessions"]
            scale_10 = per_day * 10
            print(f"  min={thr}/6: ${per_day:>5.0f}/session × 10ct = ${scale_10:>5.0f}/day  "
                  f"({d['n_actual_trades']/d['n_sessions']:.2f} trades/active-day)")


if __name__ == "__main__":
    main()
