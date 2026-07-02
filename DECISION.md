# MEIC DECISION CHARTER — pre-registered, written before the data exists

**Purpose:** the scale-vs-retire call gets made by THESE rules, mechanically —
not by how last night felt. Written 2026-07-02, before clean night #1, so
neither sunk-cost nor rage-quit can move the goalposts later.
**Read by every future session before proposing strategy changes.**

## The goal (Caspar, 2026-07-02)
> MEIC ZeroDTE trades autonomously using put and call credit spreads —
> correctly, timely, accurately — **or** we retire it because it cannot make
> money consistently without drawdowns that wipe the account.

## What counts as a CLEAN night
A session where the machine behaved as designed — no bug-nights in the sample:
1. Every configured slot **fired or logged a reason** (`meic_slots` ledger).
2. Every submitted entry got its **real fill credit read** (`real_credit_dollars`).
3. Every exit has a **close_reason** (`stop_nbbo` / `assignment_guard` / `expiry`).
4. No scale/mark/anchor bug discovered after the fact.
5. P&L scored from **broker fills** (`reconcile_ledger.py`), never the model.

Bug-nights (Jun 22–Jul 1, all 8 so far) do NOT count toward any gate below.
Clean-night candidate #1 = 2026-07-02.

## The gates (evaluate at N = 20 clean nights, ~4-5 weeks)

### RETIRE MEIC if ANY of:
- **R1 — no edge:** mean/day ≤ $0 (broker-truth, after fees) over the 20.
- **R2 — friction eats the theta:** median entry slippage (real credit vs
  NBBO-mid model) still worse than −25% AFTER the NBBO entry plane. If we
  can't get filled near executable quotes at 1-lot, no tuning fixes that.
- **R3 — structure failure:** any single clean night loses > 15% of the
  account (defined-risk math says ≈9.5% is the 4-condor doomsday; beyond
  that means the machine, not the market, failed).
- **R4 — stop dysfunction persists:** stop-rate > 70% across the 20 while
  median MAE at stop is shallower than −40% of credit (= still stopping
  condors that were never threatened).

### EXTEND (20 more nights, no tuning) if:
- mean/day > 0 but HAC t < 1.5 — promising, unproven. Patience is a position.

### SCALE (via improve_loop gates, which stay authoritative) if ALL of:
- mean/day > 0 with HAC t ≥ 1.5 at N ≥ 20.
- Stop-rate in the healthy band (25–55%) and expiry-rate ≥ 30%.
- Worst clean night ≥ −3× mean/day.
- **Sizing law (permanent):** contracts sized so worst-day ≤ 5% of equity —
  at $10k and ~$240 max-loss/condor × 4 slots that's 1→2 contracts max.
  10 contracts = one bad day = the account. Never.

### The wipeout question, answered with arithmetic
At 1 contract, 4 slots, $2.5 SPY wings: absolute worst day ≈ −$950 = 9.5% of
$10k — and that requires every stop AND the assignment guard to fail AND a
close beyond the wings. **The account cannot be wiped at current sizing.**
Wipeout risk enters ONLY through sizing indiscipline; the sizing law above is
the firewall. "Retire because drawdowns wipe the account" is therefore a
sizing-policy question we control, not a strategy property we must fear.

## Execution plane (the 2026-07-02 rebuild — what made nights cleanable)
- **One data plane:** entries pick strikes by per-strike-IV delta off live
  Alpaca NBBO (`nbbo_chain.py`); stops mark buyback off the same NBBO; the
  stop anchors to the REAL fill credit, always (model never vetoes broker).
- **Free-risk ban:** each side must pay ≥ $100 (SPX-scale) conservative
  credit; the paying side trades alone (one-sided MEIC), both-fail → skip.
- **Assignment guard:** final 15 min, any short within $0.50 of spot is
  force-closed (SPY = physical delivery; the −600-share lesson). Early-close
  aware via the Alpaca calendar.
- **No silent decisions:** per-slot ledger + close reasons + EOD integrity
  line; fail-safe = HOLD on bad marks (wings cap the risk, phantoms don't).

## Options-data upgrade path (IBKR) — recommendation 2026-07-02
Gateway was down on eval day; recommendation from documented properties:
- **Not needed for the paper validation phase.** Alpaca indicative NBBO proved
  sufficient (tight 2-sided quotes, per-strike IV computable in-house) — and
  data-plane unity (price where you fill) beats marginally better quotes from
  a feed we don't trade on.
- **Becomes the move at SCALE decision time,** for the INSTRUMENT more than
  the data: XSP/SPX are European, cash-settled — no assignment risk, no
  guard needed, and §1256 tax treatment on real money. IBKR = data feed ONLY
  (hard rule: never an order path from this codebase).

## Amendment A1 — 2026-07-02, BEFORE clean night #1 counted (shakedown day)
**IC_STOP_BUFFER 1.05 → 1.50** ("disaster stop, not breakeven stop"). Caspar's
challenge ("the key is expiring WORTHLESS — why so trigger-happy?") was tested,
not vibes-adopted: 6-config race + 3-point richness sensitivity on the honest
engine (see .env note + scripts/meic_backtest stop_mode). 1.50× dominates 1.05×
at every R (+23-25% mean/day, expiry 57%→77%, t intact); per-side Chambless
REJECTED (worse tails, no edge); no-stop REJECTED (edge is an R-artifact —
t collapses 28→14 at R=1.0 — and the model can't price gap days; wings + the
assignment guard remain the true catastrophe caps). Because this landed on the
shakedown day, the clean-night counter starts at ZERO from Mon 2026-07-06 with
the config FROZEN: NBBO plane, real anchor, $100 side floor, one-sided on,
buffer 1.50, guard on. No further stop tuning inside the series — R4's
stop-dysfunction gate now reads against the 25-55% *side*-stop band with the
new buffer's expected ~23% stop-rate as the healthy center.

## Standing constraints (unchanged, non-negotiable)
Paper only · never touch CasaaFinance positions · `.env` never committed ·
improve-loop gates not bypassed · WaveZero runs its own account/backend.
