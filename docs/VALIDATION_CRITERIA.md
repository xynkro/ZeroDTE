# ZeroDTE — Validation Exit-Criteria

_The council's sharpest point: protecting clean data is moot if the data can't
statistically settle "is the edge real?". This document defines, in advance, the
bar that decides DEPLOY-REAL vs KILL — so the decision is made by numbers, not by
a hot streak or a cold one._

Engine: honest Black-Scholes backtest, validated config **30Δ / TP90 / no-ladder**,
3-year SPX 5-min (2022–2026). All figures reproducible via
`run_honest_backtest(target_delta=30, final_tp_target=90, use_dynamic_stops=False)`.

---

## 1. The uncomfortable headline

The validated **5-year, 153-trade** backtest:

| metric | value |
|---|---|
| total P&L | +$5,479 |
| mean / trade | **+$35.8** |
| std / trade | **$240** (≈ 6.7× the mean) |
| **t-statistic** | **1.85** |

**t = 1.85 is BELOW 1.96.** Even five years of backtest does **not** clear the
conventional 95% bar that the per-trade edge is different from zero. The edge may
be real, but it is **statistically marginal** — small mean buried in large
per-trade variance.

→ **A 4–6 week paper window (a handful of trades) cannot possibly confirm it.**
You need *sample size*, and the sample has to come in clean.

## 2. Statistical bar — how many trades before we can conclude

At the measured expectancy (mean $35.8, sd $240):

| confidence | trades required |
|---|---|
| 95% (t > 1.96) | **173** |
| 99% (t > 2.58) | **299** |

The strategy fires ≈ **35 trades/year** in the backtest. So a real verdict needs
**~5 years of equivalent live trades** — OR a higher realized win-rate/expectancy
than the backtest (which would lower the bar). Either way: **do not conclude from
fewer than ~150 clean, post-fix trades.** Current live count: **4** (contaminated
by the over-trading bug). We are at ~2% of the evidence needed.

## 3. Slippage / cost — the edge is thin, and here's where it dies

Per-trade P&L minus a round-trip cost per spread (bid/ask + commissions):

| cost / spread (SPX) | total P&L | avg / trade | verdict |
|---|---|---|---|
| $25 (validated) | +$5,479 | +$35.8 | +EV |
| $40 | +$3,184 | +$20.8 | +EV |
| $60 | +$124 | +$0.8 | ~breakeven |
| $80 | −$2,937 | −$19.2 | **DEAD** |
| $100 | −$5,996 | −$39.2 | **DEAD** |

**Breakeven ≈ $60.8 / spread round-trip (SPX).** Above that, no edge.
Live executes **SPY at 1/10 scale**, so the live breakeven is **≈ $6.1 / spread**.

Realistic 0DTE costs (SPX $10-wide): bid/ask ~$10–30 + commissions ~$4–8 ≈
**$15–40** — *inside* the buffer, but not by a wide margin. **The validation must
therefore measure realized fill cost**, not just P&L. If live round-trip cost
trends above ~$40/spread (SPX) the edge is in jeopardy regardless of win-rate.

## 4. The decision rule (set NOW, before emotion)

**DEPLOY REAL MONEY only if, after ≥ ~150 clean post-fix paper trades:**
1. cumulative P&L is **positive** AND the t-stat of the live per-trade P&L **> 1.96**, and
2. realized round-trip cost per spread stays **< ~$40 (SPX-equiv)**, and
3. the live put/call mix and win-rate are **consistent with the backtest**
   (the debrief's directional-skew flag is not screaming), and
4. max drawdown has **not** breached the backtested **−$1,581** (SPX-equiv).

**KILL / re-design if:**
- realized cost pushes the breakeven test negative, OR
- 50+ clean trades in and the live t-stat is going the *wrong* way, OR
- drawdown breaches −$1,581 with no regime explanation.

**Otherwise: keep collecting clean data. Do not touch the strategy logic.**

## 5. Data-integrity guardrails (so the sample stays clean)
- **Daily reconciliation** of the recorded ledger vs the broker (see
  `scripts/reconcile.py` / `/api/reconcile`). A mismatch = a poisoned data point.
- The auto-**debrief** classifies every trade and flags directional skew.
- **Resilient restore** keeps one bad record from wiping the session.

---

_Bottom line: the question is not "are we up or down this week." It's "have we
collected enough clean, low-cost trades to clear t > 1.96." Today: no — not close._
