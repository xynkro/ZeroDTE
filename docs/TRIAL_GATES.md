# WaveZero 25-Trade Trial — PRE-REGISTERED GATES

**Registered 2026-06-30, BEFORE the first live trade of the band config.**
These criteria were fixed while the live sample was zero. Do not move them after
data arrives — moving goalposts after seeing results is how every retail account
convinces itself a dead strategy is alive. The nightly debrief prints the
scoreboard against these gates (`debrief.trial_status`).

## What is on trial
The validated band-anchored config, live on WaveZero (own $10K paper account
`PA34H0BS75JB`, branch `wavezero`, armed 2026-06-30):
- Band-anchored strikes (Bollinger 14/2.5 extreme, walk to $30 floor)
- Schwartz vol-released gate (trade only when morning r5 > expanding median)
- Cushion filter (skip < 0.5% OTM)
- TP40 + breach-stop, one time-triggered entry/day ~10:00 ET, ~40% of days
- Backtest benchmark: OOS +$41.68/day (t=5.85), cushion +$54, worst −$304
  per $1,000 SPX risk. **Absolute $ overstate — the trial measures the
  capture ratio, not whether we hit +$42.**

## The gates (trial = first 25 closed trades with REAL fill P&L)
| Condition | Action |
|---|---|
| Real drawdown > **15%** of account, any time | **HALT immediately**, review before any restart |
| Single loss > modeled max-loss that root-causes to *strategy* (not a fixable bug) | **HALT + review** |
| After 25 real-fill trades: real mean/trade **≤ $0** | **RETIRE WaveZero.** No renegotiation. |
| After 25: positive but capture **< 40%** of model | **Investigate execution** (telemetry decomposes it) before touching strategy or size |
| After 25: capture **≥ 60%** AND real t-stat ≥ 2 | **Scale**: step risk up, ceiling = half-Kelly |

Notes:
- "Real" = `broker_realized_pnl` from Alpaca fills. Model P&L never grades itself.
- Expected accrual: ~40% of ~21 sessions/mo ≈ 8–9 trades/mo → 25 trades ≈ end-Sep 2026.
- Fill-quality telemetry (entry/exit CBOE mid on every trade) splits any gap:
  model-vs-mid = SIGNAL problem; mid-vs-fill = EXECUTION problem.
- Sizing during trial: existing DIRECTIONAL sizing (~6ct ≈ $600 ≈ 6% max-loss/trade).
  Do not change size mid-trial — it contaminates the sample.

## IBKR recommendation (data, not orders)
Alpaca's free feed has no options quotes; telemetry uses CBOE **delayed** mids
(~15 min) — adequate for trial-grade diagnosis, not for tight execution work.
**If** the trial lands in "investigate execution" (capture < 40% with healthy
signal), wire IBKR as an OPTIONS-DATA feed (real-time quotes/greeks via the
existing `ibkr_feed.get_options_chain_with_greeks`, port 7497 paper) to quote
entries at true mid and meter slippage precisely. IBKR stays a DATA feed only —
orders remain on Alpaca paper (hard rule). Do not build this before the trial
says execution is the binding constraint.

## What was already fixed before the trial started (execution integrity)
- One-attempt-per-day marker persists across restarts (no double-entry).
- Exit: single mleg close; late (≥15:55 ET) fully-OTM spreads book
  `expired_otm` with real P&L instead of thrashing canceled orders; one bounded
  retry on transient close failure; loud `UNMANAGED POSITION RISK` log on any
  close_error.
- EOD summary no longer raise-spams on the token-less (Telegram-silent) instance.
- Calendar: thisweek feed live; dead nextweek URL demoted to debug.

## Verdict procedure
At trade 25 (or a HALT trigger): run the scoreboard, write the verdict in
HANDOFF.md, and act per the table. If RETIRE: flip `WAVE_BAND_STRATEGY_ENABLED=false`,
stop the instance, write the post-mortem. The deal that makes running this
rational is that the gates actually bind.
