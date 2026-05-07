# ZeroDTE

Decision-support tooling for 0DTE put/call credit spread trading on SPX.

**Status**: v0 indicator + first-pass backtest delivered. Live validation phase.

## Origin

Pivot from `~/Documents/Trading/CryptoTrader/` (paused). Caspar has a documented 28-day track record on 0DTE credit spreads ($1k/night). The strategy works; the gap is execution support and macro-news awareness (a Trump→Iran headline blew up the account when iron condors were exposed mid-day). This project builds tools to support that strategy, not replace your judgment.

## What's in here now

```
ZeroDTE/
├── README.md                                      (this file)
├── research/exploration_plan.md                   (strategy codification + roadmap)
├── docs/predictor_spec.md                         (indicator spec + first-pass backtest)
├── indicators/zerodte_range_predictor.pine        (PINE V5 — copy to TradingView)
├── backend/
│   ├── app/predictor.py                           (Python port of Pine logic)
│   ├── scripts/backtest_predictor.py              (backtest runner)
│   └── data/
│       ├── historical/SPX_5m_60d.json             (60d of SPX 5-min bars)
│       └── backtest_results/predictor_validation.json
└── .venv/                                         (reused from CryptoTrader)
```

## How to use the indicator (live, no broker needed)

1. Open TradingView, navigate to SPX (^GSPC or SPX) on 5-minute timeframe
2. Open Pine Editor (bottom panel)
3. Copy the entire contents of `indicators/zerodte_range_predictor.pine`
4. Paste into Pine Editor → Save (give it any name, e.g. "ZeroDTE-RP")
5. Click "Add to chart" — projected high/low lines + signal triangles overlay your SPX chart
6. (Optional) Right-click chart → Add Alert → choose one of: `ZeroDTE: SELL CALL CS`, `SELL PUT CS`, `SELL CALL CS (WVF confirmed)`, `SELL PUT CS (WVF confirmed)`

## How to run the backtest

```bash
cd ~/Documents/Trading/ZeroDTE/backend
../.venv/bin/python -m scripts.backtest_predictor
```

Refresh data first by re-running the TV history fetch (TBD: write fetch script).

## Pre-registration discipline

Same as CryptoTrader's `LESSONS.md`:
- Pre-register bars before testing
- No spec parameter retries after a kill
- Friction-shocked costs
- PF > 1.0 over WR > X
- Re-baseline before defining bars

ZeroDTE-specific addendum: this isn't a "find an edge" project (Caspar already has one). It's "build tools that make the existing edge safer + more efficient." So 'kill verdicts' don't apply the same way — partial validation + live observation is the right calibration.

## Frozen reference projects

- `~/Documents/Trading/FXTrader/` — frozen at `fx-final-2026-05-07`
- `~/Documents/Trading/CryptoTrader/` — paused; recommend tag `crypto-paused-2026-05-07`
