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
- **2026-10-30 — CONFIG L v3: exits on REAL quotes; the mechanical 13:00 control arm; model reads demoted;
  Caspar's reads via Telegram. Pre-registered before the first order.**
  WHAT WENT WRONG (Oct 12–30, 15 sessions, 0 broker orders): the probe's 2 submitted trades (Oct 22, 23,
  XSP call spreads at $13/$12 credit) were marked by the Black-Scholes exit model at 2.1–2.6× their real
  credit ON THE ENTRY BAR, booked as −163%/−110% stops, and the broker entry was dropped before any
  order — both would have been winners on the real path. 30/41 reads were refused by the $12 floor
  because XSP pays nothing at ≥0.30% after 13:00 (median $9/$6 at 13–14h, $3/$2 at 14–15h); the
  09:45 slot, the only one where XSP pays ($28/ct at 0.30%), was gated behind confidence ≥ 0.70 which
  the model never emits (max 0.58 in 47 reads). The scheduled model read scored 14/32 = 44% on
  direction: the pre-registered "direction hit rate < 50% → RETIRE" condition is MET for scan-sourced
  reads. A second Wave engine was found running on the Mac (reloaded on login) in shadow, duplicating
  Telegram reads — plists removed from LaunchAgents.
  REAL-QUOTE REPLAY (1,076 five-minute rows, every XSP leg's bid/ask, no model; one entry per side per
  day; stop = buy-back ≥ 2× credit; TP 60%; 15:25 close): every morning cell loses (09:45 −$4/trade,
  10:30 −$4); 12:30–13:30 at 0.20–0.40% with floor $6–20 is a plateau of small positives (+$2 to +$6/trade,
  total +$51 over 15 sessions at 13:00 / 0.30% / $12 — ~$3.40 per session); 13:30+ negative; calls
  carried it (+$11/trade, 6/7 green) while puts lost (−$3) in a three-week down-drift. The 13:00 model
  reads' side beat both-sides on the 6 rows where the floor passed (6/6), n too small to mean anything.
  HONEST READING: there is no demonstrated edge at this size in this tape; the 13:00 cell is the only
  candidate with real-quote support and its expectancy is a few dollars that may be regime.
  CHANGES (effective Fri 2026-10-30 13:00 ET if deployed in time, else Mon 2026-11-02):
    • Exits for explicit-leg trades are computed in backend/app/live_exit.py on live NBBO of the broker
      legs: stop when buy-back ≥ 2× real credit (CALL_STOP_MULT=1.0), TP 60%, time stop 15:25 ET, never
      on the entry bar or before the broker fill. The Black-Scholes model never marks these trades.
    • Control arm (mechanical): once per session at 13:00 ET sell BOTH sides at 0.30% from spot (step-out
      rule applies), XSP, $2 wide, one lot per side, floor $12/ct. CALL_MAX_OPEN=2, ≤2 trades/day.
      Expected: +$3/trade ± noise; worst plausible day −$380 (two stops).
    • Treatment arm (human): Caspar's reads via Telegram `/read up|down <SPX level where wrong> [conf] [note]`,
      same rules; executes only when the market pays the floor. Scored separately by source.
    • Scheduled model reads: retired as a trade source (gate met). One 13:00 read a day continues as a
      logged, scored commentary (advisory only, not pushed per read).
  GATES (unchanged in spirit; per SOURCE now — mech and caspar scored separately): at 25 traded entries
  for a source, RETIRE it if mean real $/trade ≤ 0 or any day ≤ −$480 or DD > 15%; PASS to the IBKR
  discussion if mean > 0 with session-bootstrap P ≥ 0.90, ≥ 70% green sessions, worst ≥ −$480.
  If the mech arm shows ≤ 0 at 25 and the human arm has < 10 reads, the SPY/XSP 0DTE book closes and
  the sampler stays on as the regime detector (re-engage when the ATM straddle implies > 0.35% to
  the close at 13:00, i.e. when premium returns).
  FIRST SESSION 2026-10-30 (ET): call side refused by the floor ($9 < $12 at XSP 762/764 — correct);
  put side accepted at $18/ct (XSP 755/753, stepped out from 756) but the order never reached Alpaca:
  the morning's review fix had indented the whole placement block under the nudge guard (fixed
  588d8fe1 within 5 minutes; AST check now asserts placement is reachable). Re-issued at 13:06 →
  $11 < $12, refused. The pending trade was booked not_filled. A deliberate 1-lot far-OTM XSP SMOKE
  TEST through the real trader code then filled and closed cleanly (749/747P, +$1 entry, −$4 close,
  equity −$5.10 → ~$2 of index-option fees per round-trip lot, which the floor arithmetic must carry:
  $2 on a $12 credit is 17%). Excluded from scoring. First scored order: Mon 2026-11-02 13:00 ET.

- **2026-10-10 — CONFIG L v2 (instrument + geometry), decided by WaveZero at Caspar's instruction
  "you decide which will make us money". Sample so far: 0 trades (every read refused by the $20 floor).**
  Evidence: the real-quote grid on the probe's own read windows (4 sessions Oct 6–9, $2-wide, stop ≈
  touch): the v1 geometry (floor $20, TP 40%, any distance, 09:45+13:00) is −$1.5 to −$7 per trade
  before any read skill because strikes that pay $20 sit 0.1–0.2% from spot and get touched 29–41%
  of the time (breakeven < 29%). Every cell with a $10–15 floor, ≥0.25–0.30% distance and TP 50–60%
  on the 13:00/14:00 windows was positive (+$8 to +$11/trade, 0 touches in the sample); AM entries
  were negative in every variant. Sample is 4 sessions: the numbers are direction, not proof.
  CHANGES (effective Mon 2026-10-12, 09:30 ET):
    • Instrument: XSP (SPX/10, cash-settled, European — no assignment, no Alpaca 15:45 sweep),
      1 contract, $2 wide; broker legs carried on the trade and reversed exactly at exit; engine
      marks on SPY×10 bars; read levels mapped to XSP strikes with the live option-implied ratio,
      rounded AWAY from spot. All Telegram levels in SPX points.
    • Executable-credit floor: $12/ct (6% of width; Caspar's 10% rule relaxed by this entry).
    • Minimum distance: the invalidation must be ≥ 0.30% from spot (else refused as too tight).
    • Take-profit 60% of credit (was 40%); stop −100% of credit, 15:25 ET close unchanged.
    • Read slots 09:45, 13:00, 14:00 ET; the 09:45 read needs confidence ≥ 0.70, the PM reads ≥ 0.55.
    • Unchanged: ONE open position, ≤ 3 trades/day, day halt −$480, drawdown halt 15%.
  GATES unchanged from the Config L entry (n = 25 traded reads or 14 sessions, whichever is later).
  The v1 reads (Oct 9, all refused) stay in the journal as advisory rows; the scored sample starts
  with the first XSP fill.

- **2026-10-09 — CONFIG L: the LIVE-READ PROBE (Caspar's option 2). PRE-REGISTERED before the
  first trade.** Purpose: test whether discretionary intraday reads — Caspar's, Claude's in-session,
  or the engine's scheduled Claude read (claude-sonnet-5, slots 09:45 and 13:00 ET) — make money
  on PAPER with real fills, after the mechanical configs (E/F, MEIC) did not. Caspar's own
  benchmark: a 28-session winning streak ended by one unscheduled geopolitical shock; the design
  goal is therefore that one bad day cannot take the month.
  Mechanics (backend/app/live_call.py, POST /api/call): a read = lean (up/down/neutral) +
  confidence + invalidation level (SPY). Non-neutral reads with confidence ≥ 0.55 become ONE SPY
  0DTE credit spread, short strike AT the invalidation level, $2 wide, executable credit
  (short.bid − long.ask, live NBBO) ≥ 10% of width ($0.20/share), else no trade. ONE contract,
  ONE open position, no entry after 15:00 ET. Managed by the existing engine: NBBO entry ladder,
  real-fill capture, TP 40% of credit, breach stop at the short strike, forced close 15:25 ET
  (ahead of Alpaca's 15:45 expiry sweep). Scan-sourced reads stand aside on high-impact event days;
  manual reads may override (journaled as such). Every read is journaled in live_calls.jsonl and
  scored twice: direction vs the session close, and broker dollars (scripts/score_calls.py, posted
  to Telegram after each close). Max loss per trade ≈ $180; worst plausible day ≈ −$180 (1.9%).
  Account at start: $9,482.
  GATES (n = 25 scored reads that traded, or 14 sessions, whichever is later):
    RETIRE: mean real $/trade ≤ 0, OR direction hit rate < 50%, OR any day ≤ −$480 (5%),
            OR drawdown > 15% at any time (HALT immediately).
    INVESTIGATE: mean > 0 but < 50% of credit captured on average.
    PASS (→ IBKR discussion, Caspar's deliberate act): mean real $/trade > 0 with session-bootstrap
          P(mean > 0) ≥ 0.90, ≥ 70% of traded sessions green, worst session ≥ −$480, and
          direction hit rate ≥ 55%. "Green every day" is NOT a gate: a probe that must win 14/14 is
          selected for by luck, not edge; the gate that protects the 28-day streak is the worst-day
          bound, not the streak itself.
  Changes to any of the above require a new entry here BEFORE they take effect.

- **2026-10-06 — CONFIG F RETIRED at n=14 (−$243 real); MEASUREMENT-ONLY phase pre-registered.**
  Verdict from the real-credit evaluation (docs/DECISION_2026-10-06_real_credit_evaluation.md):
  the n=25 retire gate (mean ≤ $0) is already unreachable — the next 11 trades would have to
  average > +$22.1 while F's mean winner is +$21.6 (best +$45); bootstrap from F's own fills
  gives P(mean > 0 at n=25) = 0.2%. The 15% halt was not near (account $9,572). Disarmed via
  `WAVE_BAND_STRATEGY_ENABLED=false` on 2026-10-06 (one gated slot, zero orders that session).
  Also booked: the Oct-1 10:30 call spread that Alpaca's expiry liquidation closed at 15:45 ET
  for −$164 (our 15:45 close raced it); time stop moved to 15:25 ET; the ledger merge no
  longer lets an in-memory blank overwrite a real broker number.
  NOTHING QUALIFIES FOR DEPLOYMENT on 7 sessions: no structure in the premium-targeted,
  touch-stop, iron-fly, breakeven-condor or time-of-day families was both net positive and
  positive in ≥5 of 6 evaluable sessions on executable credits with the T-15 forced close.
  NEXT = MEASUREMENT ONLY: the surface sampler (per-leg bid/ask, through 16:01 ET; hosted on
  GitHub Actions every 5 min → branch `surface-data`, plus the Mac copy) for 30 DENSE sessions
  (~mid-Nov 2026). GATE, fixed now: a structure simulated on executable credits with the T-15
  close qualifies only if total > 0, ≥70% of sessions positive, worst session at 1 ct ≤ $480
  (5% of $9,572), session-bootstrap P(mean > 0) ≥ 0.90, EV/trade ≥ 3× measured quote-to-fill
  slippage ($2.7/ct in F). Then a 25-trade PROBE: 1 ct, 1 open position, exits anchored to
  broker_realized_credit (not model), same-SPY-strike guard, daily halt −$480.
    Retire: mean/trade ≤ $0 at n=25, OR two sessions ≤ −$480, OR DD > 15%.
    Investigate: mean > 0 but < 50% of simulated EV.
    Scale: capture ≥ 60%, t ≥ 2 → 2 ct, max 5%/position.
  If nothing passes the gate at 30 sessions: the SPY 0DTE book closes.
  NOTE 2026-10-09: the legacy DIRECTIONAL_SPREAD path kept trading after the band disarm (Oct-7
  −$124, Oct-8 +$36; ledger rows tagged legacy_directional_zombie — excluded from every sample).
  Both order paths are now off and PAPER_BROKER=none. Config F's final sample stays 14 / −$243.
  Host moved to a GCP VM the same day (deploy/vm/README.md).

- **2026-09-22 — CONFIG F FIRST-FORTNIGHT CHECKPOINT (NO config change; F sample stays 2/25).**
  Sessions Sep-8→22 (10 trading days): 2 trades (Sep-11 10:00 put 7615/7605 5ct; 11:00 put
  7595/7585 4ct), both TP, +$57 real (executable est. $12/$10 per ct, filled $11/$5), acct $9,871.
  Why so few: (1) the MacBook slept on battery — Sep-14→17 produced zero decisions on BOTH
  books (pmset maintenance-sleep cycling from Sep-15 22:32 SGT; MEIC's GEX log dark too),
  Sep-18 woke for 2 slots, reboot Sep-21 14:16 SGT. Sessions lost to sleep do NOT count as
  sessions. (2) When awake, 66/68 slot-sides gated on the $10 executable floor: NBBO best
  credit at ≥0.6% OTM median $0–3.5/ct, max $8 (G12 PASS, ≥54 NBBO-priced sides). Live
  surface Sep-22 10:28 ET: $10/ct only at ~0.2% OTM. Real-path touch rates after a 10:00
  entry, last 20 sessions, per side: 0.2% 50–60%, 0.4% 15–20%, 0.6% 0–5%. TP40+breach
  breakeven at $10–12 credit needs P(touch) < ~10–24% → no strike satisfies the 10%-of-width
  floor AND positive EV in this regime. The backtest's 0.5 haircut overstated credit ~5–10× at
  0.6% OTM; treat the journal's best_real_ct as the calibration from now on, not model×0.5.
  Gates UNCHANGED. Decision for Caspar: stand armed for vol (F trades correctly when r5 ≈
  median — Sep-11) or pre-register a NEW structure as a new entry here. An always-on host is
  a prerequisite for any verdict.

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
  sessions, n=100 in ~2 weeks → gate verdict by ~Sep-19. First Config F session = Tue 2026-09-08
  (Mon Sep-7 is Labor Day — closed; confirmed via the Alpaca calendar). Live-money flip stays
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

- **2026-11-02 — CORRECTION to the 2026-10-30 replay expectation (no rule, gate or setting changed).**
  The "+$51 over 15 sessions, +$3/trade" for 13:00 / 0.30% / $12 reproduces exactly, but that replay took
  no fees, used the NEAREST strike (often under 0.30%) and retried for 15 minutes. With the engine's
  actual strike logic, one shot and the $2 fee (`scripts/real_quote_replay.py`, strikes match the live
  2026-10-30 order): 13:00 is −$22 on 9 trades (21 sides refused by the floor), +$13 if a stop needs two
  5-minute rows, −$28 on 5 trades using the row nearest 13:01. Treat the mech arm's expectation as about $0,
  not +$3. Mornings stay worse at 0.30%: 09:45 −$224 on 24 (−$107 with two rows), 09:35 −$179, 10:30 −$156.
  11:00 is about as flat as 13:00 (−$3 on 17), so 13:00 is the least-bad cell tested with no demonstrated
  edge. 15 sessions in a down-drift (13:00 calls +$52, puts −$74). At 9 trades per 15 sessions the 25-trade
  gate takes about 42 sessions, so the 14-day trial will not settle the mech arm.

- **2026-11-01 — CONFIG L v3.1: the mech arm stands aside on LOUD mornings. Pre-registered Sat
  22:50 ET / Sun 10:50 SGT, before the first scored order (Mon 2026-11-02 13:00 ET).**
  Caspar asked whether "bad day → don't trade" is the rule, and whether a bad day means "sell calls
  because we know it is going down". Measured on SPY 2020-07 → 2026-10 (1,575 days / 1,561 sessions,
  Alpaca iex bars, `scripts/gap_vs_intraday.py`, `scripts/morning_vs_afternoon.py`, write-up in
  `docs/RESEARCH_2026-11-01_direction_vs_size.md`): DIRECTION is not forecastable (corr gap→day −0.03,
  corr morning→afternoon +0.03; after a >1% gap down the day continues down 45% and the high still
  runs >0.30% above the open 79% of the time), SIZE is (corr morning range→afternoon range +0.50).
  For the 13:00 pair at 0.30% the afternoon touches a side 42% of the time on the quietest-quarter
  mornings and 98% on the loudest tenth; at ≈$12 credit vs ≈$30 stop a side needs ~71% to break
  even, so mornings with range ≥ ~1.0% are negative by construction.
  CHANGE: `CALL_MECH_MAX_AM_RANGE_PCT=1.0` — when (high−low)/open of the 09:30→13:00 bars exceeds
  1.0% the pair is skipped, journaled as `stand_aside` with the range, and pinged. Skip-only: the gate
  never adds a trade, never picks a side (the arm stays two-sided), never touches Caspar's /read
  trades. Expected to skip ~30% of sessions. VERIFY after the trial: compare skipped vs traded
  sessions on the NBBO surface (real credits), since loud days also pay more credit. If the skipped
  cell was in fact positive on real credits, set the key to 0 and say so here.

- **2026-11-01 — CAPS: `CALL_MAX_OPEN` 2→3 and `CALL_MAX_TRADES_PER_DAY` 2→3, at Caspar's instruction. Set Sun
  09:08 ET / 22:08 SGT, before the first scored order (Mon 2026-11-02 13:00 ET / Tue 02:00 SGT).**
  With 2/2 the 13:00 pair used the whole allowance: a /read still open at 13:00 made the pair stand aside,
  and a live pair refused later reads. Both keys had to move, because the pair needs two free OPEN slots as
  well as two free DAY slots. Room is now one read plus the pair. Two reads before 13:00 still make the pair
  stand aside (1 day slot left). The arms are scored by source, so the gates are unchanged.
  RISK: the arithmetic worst day goes from -$376 to -$564 (3 x $188 max loss), past the -$480 day line,
  because the halt only blocks new entries and does not close open ones. Reaching it needs all three
  stops to fail on a gap or fast move.
  CLOCKS: US daylight saving ended 2026-11-01, so until 2027-03-14 SGT = ET + 13h: 13:00 ET = 02:00 SGT,
  15:25 ET = 04:25 SGT, close = 05:00 SGT. Entry and exit gates use zoneinfo ET and are unaffected.

- **2026-11-02 — CLOSE-CALL TEST, pre-registered at Caspar's "go", 07:58 ET / 20:58 SGT, before the first call.**
  QUESTION (Caspar): can Claude call the SPY close from the start of the day and macro news alone? No trades
  attach to it and no trading setting changes.
  CALL: every session by 09:40 ET, made from the 09:30–09:35 ET bars and macro news. It states a direction
  (close above or below the 09:30 open) and a closing price. Neutral is not allowed and no session may be skipped.
  No call by 09:40 ET counts as a miss. A close equal to the open counts as a miss.
  SCORE: Alpaca `1Day` SIP SPY open and close, unadjusted, the source of the baselines. Log:
  `backend/data/close_calls.jsonl`, written before the outcome is known. Each row names its caller.
  GATE AT 14 SESSIONS (2 Nov to Thu 19 Nov ET / Fri 20 Nov SGT), both required:
  (1) direction ≥ 11 of 14, which beats the 52.3% base rate at p = 0.042; (2) median |close − called close| as a
  percentage of the open under 0.38%, the median miss of "close = open" since 2024.
  9 or 10 right: extend to 42 sessions, needing 28 right. 8 or fewer: the test ends as failed. Direction ≥ 11 with
  the price test failed is not a pass; it extends to 42 sessions on the same terms.
  LIMITS: 14 sessions is harsh. A true 70% caller clears 11 of 14 about 35% of the time, and a coin at the base rate
  about 4%. Five minutes of the session are known at the call, so the caller holds a small advantage over the
  open-to-close base rate; the 09:35 price is logged so the result can also be read against "close = 09:35".
  A PASS buys a paper test of the call as a side filter on Caspar's wave rule (oversold put, overbought call). It
  does not put wave on the engine and does not touch the Config L gates.
  NIGHT 1 (2 Nov, by hand): called down / 757.40 at 09:36 ET. SIP open 759.45, close 752.10: direction right, price miss 0.70% ("close = 09:35" missed 0.97%). AUTOMATED from Tue 3 Nov on the VM, same caller (claude-opus-5-5): `scripts/close_call.py`, `wavezero-closecall.timer` 09:35:20 ET, `wavezero-closescore.timer` 16:25 ET. News input is now Finnhub headlines plus the ForexFactory calendar; night 1 had no same-day headlines. The VM log is now the record. Scoring must end the SIP query over 15 minutes back (the free data plan returned 403 otherwise). Rules unchanged.