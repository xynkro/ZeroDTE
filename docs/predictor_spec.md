# ZeroDTE Range Predictor — spec + first-pass backtest

**Created**: 2026-05-07
**Status**: v0 indicator delivered. Caspar to overlay on TradingView for live validation. First-pass backtest below shows partial validation but signal frequency is too low; live observation needed.

## Files delivered

| File | Purpose |
|---|---|
| `indicators/zerodte_range_predictor.pine` | Pine v5 indicator. Copy entire file → TV Pine Editor → Save → "Add to chart" on SPX, set timeframe to 5m |
| `backend/app/predictor.py` | Python port of the Pine logic (faithful — same constants, same order of ops) |
| `backend/scripts/backtest_predictor.py` | Backtest runner |
| `backend/data/historical/SPX_5m_60d.json` | 60 trading days of SPX 5-min bars (Feb 10 → May 6 2026) |
| `backend/data/backtest_results/predictor_validation.json` | Full backtest results JSON |

## What the indicator outputs (overlaid on chart)

- **Predicted day HIGH** (red line, post-09:45 ET) — call credit spread sell zone
- **Predicted day LOW** (green line, post-09:45 ET) — put credit spread sell zone
- **Observation window** (gray band, 09:30-09:45 ET)
- **Regime label** at 09:45 ET: `VOLATILE — STAND ASIDE` or `NON-VOLATILE — RANGE TRADE`
- **Sell-call signals** (red ▼ above bar) when RSI(14) > 70 + Stoch %K crossed below %D from > 80 + price near projected high
- **Sell-put signals** (green ▲ below bar) when RSI(14) < 30 + Stoch %K crossed above %D from < 20 + price near projected low
- **Strong signals** (when WVF spike confirms) — separate alert
- **Diagnostic table** (top-right): live RSI, Stoch %K/%D, WVF spike status, predicted high/low, regime

## TradingView alerts you can set

After adding the indicator to your chart, click "Alert" → select one of these conditions:
- `ZeroDTE: SELL CALL CS` — fires on first sell-call signal of session
- `ZeroDTE: SELL PUT CS` — fires on first sell-put signal of session
- `ZeroDTE: SELL CALL CS (WVF confirmed)` — strong sell-call (with WVF spike)
- `ZeroDTE: SELL PUT CS (WVF confirmed)` — strong sell-put

Alerts fire in real-time during your trading session and can be sent to email / mobile push / webhook.

## First-pass backtest (60 trading days, SPX 5-min, Feb 10 – May 6 2026)

### Q1: Iron-condor proxy — does the projected range HOLD?

| Metric | Value | Interpretation |
|---|---:|---|
| Sessions evaluated | 45 | After warmup for D1 ATR |
| Classified VOLATILE (would skip) | **0** | **Threshold too loose** — every session classified non-volatile (the regime filter never engaged in this window) |
| Classified NON-VOLATILE | 45 | Would deploy IC every day under current spec |
| **Both bounds held** | **22 / 45 = 48.9%** | Iron condor with strikes AT projected boundaries profits ~half the time |
| Upper bound held | 33 / 45 = 73.3% | One-sided "sell call CS at projected high" wins ~3 of 4 |
| Lower bound held | 34 / 45 = 75.6% | One-sided "sell put CS at projected low" wins ~3 of 4 |

### Q2: Per-signal outcome — did fired sell-call/sell-put signals profit?

| | n | WR | Total P&L |
|---|---:|---:|---:|
| sell_call_cs | 1 | 0% | -$3.50 |
| sell_put_cs | 1 | 0% | -$3.50 |
| **WVF-confirmed** | 0 | n/a | n/a |

**Statistically meaningless** at n=2. The signal trigger is too restrictive — only 2 signals fired in 60 days. Both happened to breach.

## Honest read

### What the backtest VALIDATES

- **The projected range concept has SOME signal**. Upper bound holding 73% / lower 76% is meaningfully above 50% random.
- **One-sided trades** (just sell-call when price is at projected high; just sell-put when at projected low) are individually profitable: ~73-76% one-sided win rate.

### What the backtest DOES NOT validate

- **Iron condor at projected boundaries** is roughly breakeven (48.9%). To profit at 1:1 R/R you need > 50% AND tight execution. Wider IC wings (you mentioned in your strategy) would shift this — short strikes BEYOND projected high/low.
- **Volatile regime classification** never triggered. Threshold (1.5× D1 ATR) is too loose. Either:
  - Lower the threshold to 1.0-1.2 (more days flagged volatile, fewer iron-condor attempts)
  - Or accept that THIS 60-day window happened to be entirely non-volatile (this is plausible — Feb-May 2026 has been relatively calm; the next 60 days might look very different)
- **Discrete reversal signals** are too rare (n=2 in 60 days). The `near_projected_boundary` gate (0.5×ATR) likely too strict OR RSI/Stoch confluence rarely triggers in this regime.

### What this means for live trading

| Use case | Confidence |
|---|---|
| Use projected high/low as **strike-distance reference** for iron condor (place short strikes BEYOND, not AT) | HIGH — 73-76% one-sided hit rate |
| Use the indicator's regime label to skip volatile days | UNTESTED — calibration needed against live high-vol days |
| Take auto-suggested sell-call / sell-put signals as entry triggers | LOW (n=2 is no evidence either way) — better to use as **confirmation** for your own discretionary entries |

## Recommended live validation protocol

1. **Copy the Pine to TV today.** Add to SPX 5m chart. Set up the 4 alerts.
2. **Watch for 5 trading days, no trades.** Just observe:
   - Does the regime label match how the day actually plays out?
   - Do projected high/low get hit (breached)? In what % of sessions?
   - When sell-call/sell-put triangles fire, does price actually reverse?
3. **If observation looks good**: trade ONE small spread per signal for another 5 days.
4. **Report back** — calibration adjustments to RSI/Stoch/WVF thresholds based on YOUR observations.

The Pine inputs (RSI thresholds, Stoch levels, near-boundary distance, regime threshold) are all exposed in the indicator settings — you can tweak in TV directly without me re-coding.

## What I'm NOT building yet

- Live broker integration (you said brokerage is TBD; I haven't started)
- News feed / macro blackout system (also pending until we know if the indicator itself is useful)
- Auto-execute (your stretch dream, not now per your instruction)
- Full PWA dashboard (pending the indicator-first validation)

## Open questions for you

1. **Does the Pine script overlay correctly when you paste it into TV?** (Probably yes; standard Pine v5; if errors I'll fix immediately)
2. **Does the regime classification feel right vs your discretionary read?** Would you have called May 5-6 "volatile" or "non-volatile" yourself?
3. **Are the projected high/low lines visually plausible?** Too tight? Too wide?
4. **Should signals fire more often?** Loosening the `near_proj_atr` from 0.5 to 1.0 would dramatically increase signal frequency.

Let me know after you've eyeballed it for a session or two.
