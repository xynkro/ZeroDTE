# ZeroDTE — Strategy & Engine Flaws (Quant Audit, Jun 2026)

_Companion to `ZeroDTE_Quant_Audit.pptx`. Findings are code-verified and
adversarially reviewed by a multi-agent audit, then re-checked by hand. Where the
swarm and the code disagreed, the code wins (noted inline)._

## The one-paragraph verdict

The architecture is sound; the **evidence** is not. Three things quietly
invalidate the "we are validating an edge" story: **(1)** you are live-trading the
**call** book while the validated +$5,479 is **~96% puts**; **(2)** every live P&L
number is a **Black-Scholes mid simulation** — the broker's real fill is never read,
so the model is grading its own homework; and **(3)** the edge is **statistically
marginal** (t = 1.85) and **cost-fragile** (negative at ~$45/spread in recent
years; 84% of profit is a 2022–23 artifact). Live proof so far: **4 trades, all
calls, all losing (−$699).** Do not scale. Fix the measurement first.

---

## Flaws by category

### STRATEGY
1. **[CRITICAL] You trade the unvalidated side.** Side is chosen by a stochastic
   reversal trigger + a trend filter (`predictor.py:388-413`): a **call** fires on
   an overbought cross-down unless `trend=="up"`; a **put** on an oversold cross-up
   unless `trend=="down"`; inside the ±0.05% EMA10–EMA30 "flat" band **both** fire.
   In an up-drift the stoch reaches **overbought far more often than oversold**, so
   the trigger manufactures call signals, and a gentle grind reads "flat" so the
   filter never blocks them. Result: live = sell-calls-on-rallies (counter-trend,
   6 backtested trades) while the edge = sell-puts-on-dips (147 trades, +$5,357).
   _(The audit's synthesis stated the mechanism backwards — "uptrend suppresses
   puts." Verified against code: an uptrend suppresses **calls**; the call flood is
   from trigger asymmetry + the flat band, not put-suppression.)_
   **Fix:** report per-side (done); stage `DIRECTIONAL_SUPPRESS_CALLS` (done, OFF);
   reframe as a put-driven strategy.
2. **[HIGH] Flat-vol pricing has no skew.** `tv = realized 5m σ × 1.20` is
   symmetric, but real 0DTE puts carry rich skew and calls are cheap — a flat
   surface **over-credits calls** (the side you shouldn't sell) and under-credits
   puts. **Fix:** add a two-parameter skew, re-validate.

### COST / VALIDATION
3. **[CRITICAL] The live P&L is the model, not the market.** Every
   `pt.pnl = estimated_credit × exit_pct − $25`, both legs from `bs.spread_value()`
   (theoretical mid). A full grep confirms `filled_avg_price`/`avg_price` are
   **never read** — the Alpaca order is used only for its id. "Shadow validation"
   and the backtest are the **same kernel**; slippage is invisible.
   **Fix:** read per-leg fills → `broker_realized_pnl` (model fields added);
   judge validation on broker-realized only.
4. **[HIGH] MARKET multi-leg orders** (`alpaca_trader.py`) cross the full bid/ask on
   0DTE wings, entry AND exit — while the model assumes mid on both. **Fix:**
   marketable-limit at mid; market only as time-stop fallback.

### BACKEND LOGIC
5. **[HIGH] Exits run on the DEVELOPING bar → phantom wins.** The feed re-dispatches
   the still-forming 5m bar every ~60s and `_check_open_wave_trades` ran on every
   dispatch with no closed-bar guard; `honest_backtest` acts only on closed bars.
   **Fix:** `EXIT_ON_CLOSED_BAR_ONLY` flag added (OFF) — defers to the closed bar.
6. **[HIGH] Volatility frozen at entry.** `bs_realized_std` is captured once; only
   time-to-expiry shrinks. An afternoon vol-pop is under-priced — the model shows
   −60% while you're +110% and breaching. Mis-prices exactly the tail days.
   **Fix:** conservative vol-floor ratchet (lift tv, never cut a loss estimate).
7. **[HIGH] Restart/outage holes.** In-memory signal keys + no bar-age guard → a
   mid-session restart can re-book phantom duplicates (burning the 3/day cap,
   injecting fake P&L); prior-day 0DTE isn't force-settled before the day-gate
   discards it. **Fix:** persist keys + bar-age guard; append-only ledger;
   force-settle on startup. (Wall-clock open-time field added.)

### MACRO / TIMING
8. **[HIGH] The backtest and the live system are different animals.**
   `honest_backtest` has **zero** event awareness; live enforces a hard macro
   blackout. 22% of validated exits land on FOMC/CPI/NFP/OPEX days and were *more*
   profitable in-sample — so live **blocks the days that built the edge.** The
   headline number doesn't describe how you actually trade. **Fix:** re-run with an
   event overlay (event-included vs event-blocked P&L). _Highest-value missing number._
9. **[HIGH] GEX is collected but never acted on — and is inert anyway.** Negative
   dealer-gamma = breach-prone, yet the system trades full size into it; the only
   response is a size-trim that **cannot fire at 1 contract.** **Fix:** measure
   breach rate by GEX regime, then use GEX as a **stand-aside** gate, not a trim.
10. **[MED] Event-day exit costs unmodeled.** Both engines charge a flat $25 on
    FOMC as on a quiet Tuesday; open positions exit into 3–5× wider spreads at the
    modeled mid (a 3× haircut → t→1.56; 5× → t→1.28). Blackout also depends on
    impact-string labels that can drift. **Fix:** event-day cost multiplier; morning
    log of classified events.

### STATISTICAL
11. **[HIGH] The edge is inside the error bar.** Per-trade mean +$35.8, σ $240,
    **t = 1.85 (< 1.96).** ~173 clean trades needed; ~4 exist. 84% of P&L is
    2022–23; the recent regime contributed ~$678. Negative at $45/spread in
    2024–26. **Fix:** treat as UN-validated until it survives $50+/spread **and**
    ~100 clean broker-realized trades. Publish the cost curve, don't scale.

### FREQUENCY (minor)
12. **[LOW] No cross-direction spacing; `COOLDOWN_BARS=6` hardcoded** (not config).
    A call and a put can open on adjacent bars. **Fix:** lift to config; optional
    no-opposing-open-while-live rule.

---

## What changed tonight (safe, deployed) vs staged

**Deployed (no edge change):**
- Put-book vs call-book split in the debrief + EOD Telegram (`debrief.py`).
- Cost-sensitivity curve constants surfaced (`COST_CURVE`).
- Broker-realized P&L + wall-clock model fields (`models.py`) — scaffold for fills.
- Two config flags added, **defaulting to current behaviour**.

**Staged (flag, OFF by default — your decision):**
- `DIRECTIONAL_SUPPRESS_CALLS` — stand aside on the call book. Wired into the entry
  gate; flip to `true` in `.env` to enable.
- `EXIT_ON_CLOSED_BAR_ONLY` — fix the developing-bar exits. **Validate session-end
  (time-stop/EOD) behaviour before enabling.**

**Not auto-changed (needs your sign-off + live testing):** skew pricing, vol-floor
ratchet, marketable-limit execution, event-overlay backtest, GEX gating, the live
fill-read hook. Ramming these into a live engine overnight, untested, is exactly
what a disciplined desk does **not** do.

---

## If you do one thing

Wire **real fills** into the ledger and judge everything on **broker-realized P&L,
per side**. Until then you are measuring the model with the model — and the model
already told you it likes puts.
