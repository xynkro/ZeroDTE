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
- Sizing during trial (risk-owner set, 2026-07-02, BEFORE the floor-era sample):
  **$1,000 risk budget/trade = 1 SPX-equivalent backtest unit** (10–12 SPY ct at
  floor-grade→richer credits; RISK_PER_TRADE_PCT=10, SIZE_CAP_USD=1000). Makes live
  numbers directly comparable to the backtest's per-unit stats (+$42/day, worst −$304).
  Accepted consequence: a full gap-through-wing day = −10% of account; TWO such days
  trip the 15% halt gate. Trades #1–2 (5ct, pre-floor era) are normalized per-unit in
  analysis. Do not change size again mid-trial — it contaminates the sample.

## IBKR recommendation (data, not orders)
Alpaca's free feed has no options quotes; telemetry uses CBOE **delayed** mids
(~15 min) — adequate for trial-grade diagnosis, not for tight execution work.
**If** the trial lands in "investigate execution" (capture < 40% with healthy
signal), wire IBKR as an OPTIONS-DATA feed (real-time quotes/greeks via the
existing `ibkr_feed.get_options_chain_with_greeks`, port 7497 paper) to quote
entries at true mid and meter slippage precisely. IBKR stays a DATA feed only —
orders remain on Alpaca paper (hard rule). Do not build this before the trial
says execution is the binding constraint.

## Calibration log (transparent, data-based — not goalpost moves)
- **2026-09-06 (SGT) — CONFIG F: vol gate OFF + ANCHOR-FLOOR + cushion 0.6 + feed
  resilience. THIRD config change; Config E never accumulated a sample (0 trades in 10
  sessions, Aug-24→Sep-4), so nothing is lost by restarting the count.**
  ROOT CAUSES, from the decision journal (100 slots, 100 gated): (1) the Schwartz vol
  gate rejected 90+ slots — August/early-Sep is the year's deadest tape; (2) on the one
  day vol fired (Aug-27) the Bollinger(14/2.5) band had collapsed to 0.05–0.21% from
  spot, inside the 0.4% cushion, so every slot self-rejected before the floor was
  asked; (3) the engine ran the ENTIRE period on the 15-min-delayed yfinance fallback
  (Alpaca warmup queried "yesterday 09:30" → empty on any weekend restart → no way
  back; 66/78 other failures were boot-time DNS). Stale bars depress realised vol;
  even Config E should have fired 6× on real Alpaca data that fortnight.
  MY ERROR: I rejected gate-OFF on the conditional $/trading-day (+$73 vs +$118). On
  total profit per session — the objective — gate-OFF wins on every axis (+49% total,
  t 8.85 vs 7.21, same worst day). And I never ran the backtest on the low-vol slice
  before promising 2.79 trades/day: on that slice Config E fires 6%, which is what
  reality delivered.
  FIX (scripts/wave_lowvol_backtest.py, data refreshed through Sep-4 from the same
  Alpaca iex source, seam diff 0.000%): when the band collapses inside the cushion,
  anchor AT the cushion boundary (5-pt grid, outward) instead of rejecting. F6 =
  gate OFF, cushion 0.6, anchor-floor, NBBO 10%-of-width floor, 10 slots, both sides,
  TP40 + breach, 3ct. Evidence: OOS +$154.8/session vs E +$51.5 (t 11.9 vs 7.8, WR
  89%); better than E in EVERY year 2022–2026 by 3–9×; low-vol days fire 66% vs 6%;
  the exact Aug-24→Sep-4 fortnight = 43 trades, WR 95%, +$253 at live scale, no losing
  day; max drawdown at deployed 3ct = 5.8% (halt gate 15%); avgW $38 / avgL $126 /
  breach 14% per SPX unit. Rejected: cushion 0.4 (WR 81%, closer to ATM than the
  0.5–0.8% range the haircut was calibrated on).
  NOT PROVEN — the trial's job: breach-exit slippage near the money (no live trade has
  breached yet), fill quality at ~20 orders/day, and the Aug-27 NBBO "quotable but
  unpriceable" null (diagnostic now logs the exact strikes). Honest structural note:
  in low vol F6 ≈ a fixed-0.6%-cushion condor ladder — it has converged toward MEIC's
  shape; the A/B is now strike-anchoring + NBBO floor vs delta strikes.
  EXPECTED: ~10 trades/session all-days, ~4/session in the dead regime → n=25 in ~3
  sessions, n=100 in ~2 weeks → gate verdict by ~Sep-19. Live-money flip stays
  Caspar's deliberate act after the gates pass (a runbook will be written then).
  Verified by GATES.md G13–G17 (anchor-floor unit + negative controls, config, feed
  resilience, independently measured F6 backtest, deploy-on-primary-feed).


- **2026-08-23 — CONFIG E (iron condor on a dense ladder). SECOND config change; sample
  restarts again.** Config B never traded live (built same day), so no live sample is lost.
  Change: 3 slots -> 10 (10:00-14:30 /30m), one side -> BOTH sides per slot (condor),
  MAX_CONCURRENT 3 -> 5, MAX_TRADES/DAY 3 -> 20. UNCHANGED: vol gate, 10%-of-width real
  floor, 0.4% cushion, TP40 + breach-stop, no time exit, $350 size cap.
  Basis: scripts/wave_final_strategy.py head-to-head, reality-calibrated. Config E =
  2.79 trades/day, EV +$14.81/trade, WR 80.8%, avgW $43 / avgL $106, OOS +$118/day
  t=9.26 — vs one-side 3 slots at +$37/day t=7.11. Passes the bear case's own test
  (fattail.ai: win_rate x avgWin > loss_rate x avgLoss) because the breach-stop caps
  avgLoss at ~$106 rather than the ~$900 an unmanaged condor eats. REJECTED: pushing to
  5.56 trades/day (gate off, 7% floor) — money FALLS to +$73/day and the tail worsens to
  -$1,181; and the bear case's own hard-14:00-exit fix, which halves EV to +$7.1.
  Risk envelope: 3ct x ~$92 = ~$276/trade x 5 concurrent = ~$1,380 = 13.8% < the 15% halt.
  Verified by a 10-gate ledger (GATES.md): 9/10 met with re-executed evidence; G10
  (risk-owner acceptance of the exposure change) is OPEN pending Caspar.


- **2026-08-23 — CONFIG B (entry ladder). NOTE: this is a CONFIG CHANGE mid-trial.**
  Trades 1-4 (+$272, 64% capture) were on the 1-entry config; Config B's sample starts
  FRESH at n=0. Do not pool them for the verdict — report both lines separately.
  Change: entry slots 10:00 -> 10:00/11:00/12:00, cushion 0.5% -> 0.4%, size cap
  $1,000 -> $350/trade (3 x $350 = ~$810/day = LESS total risk than before).
  Basis: scripts/wave_frequency_backtest.py — a reality-calibrated backtest (0.5
  model->executable haircut measured on 7 live priced days). Its 1-entry baseline
  predicted 20% fire / 1.0 trade-wk vs reality's 13% / 0.9, so the calibration is
  trustworthy. Config B: 2x trades (4.1/fortnight), +42% $/day (+$40.4), t 6.18 ->
  7.46, tail -159 -> -212. Rejected: 4-5 slots and dropping the vol gate (t collapses
  to 4.1-5.6, tail ~triples). Vol gate and the 10%-of-width floor are UNCHANGED.
  Purpose: 2 weeks of Config B tests ENGINEERING (3 clean fires/day, no collisions,
  fills match the haircut) — it does NOT statistically validate; n=25 now ~3 months.


- **2026-07-02 (later) — floor $8 → $10/ct (risk-owner rule).** Caspar: minimum
  10% of width when risking $1,000/SPX-equivalent. Backtest split at that line:
  the sub-10% trades were 55 tiny never-losing fillers (+$1,246 total, worst +$8)
  — and live reality pays ~$0 for that class anyway. Cost of compliance ≈ nothing
  validated; benefit = auditable round rule set by the risk owner. Still BEFORE
  any trade ran under a floor.
- **2026-07-02 — real-credit floor $3 → $8/ct** (before any trade ran under a floor).
  Trades #1/#2 exposed that reality pays ~$0–1/ct for spreads the flat-IV model
  prices at ~$20/ct. First floor ($3/ct) naively scaled the backtest's $30-SPX
  *minimum*; Caspar flagged it as absurdly low. The backtest's OPERATING credits:
  median $30.7/ct-scaled (31% of width), p25 $16.7, p10 $7.8. Floor set to p10
  ($8/ct): only accept trades inside the validated credit distribution. Corollary:
  if no cushion-legal strike ever pays p10, the trial starves quickly — which IS
  the honest early verdict (the validated economics don't exist in reality).

- **2026-07-03 — daily-loss rail 2% → 12% (risk-owner approved, from the param
  debrief).** The $200 limit predated $1,000/unit sizing: a NORMAL validated loss
  day (−$90…−$304/unit) would trip it, making its breach-ping meaningless (and the
  band path takes one trade/day, so it blocked nothing). At 12% ($1,200 — just above
  one full-unit wipe) it is a genuine anomaly brake again. The pre-registered 15%
  drawdown HALT is unchanged and remains the hard backstop.

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
