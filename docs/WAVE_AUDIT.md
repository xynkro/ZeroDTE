# WAVE STRATEGY AUDIT — the money verdict (2026-06-11)

_Mandate: "$1,000/night on $10k, consistently, or shut it down." This is the
no-charades answer, from a 9-agent backtest/research swarm + inline sweeps, all on
the honest Black-Scholes engine with skew + vol-ratchet (the live pricing model)._

## 1. The verdict in one box

**Was the wave being done right? No — too TIGHT on entries and 6–15× too SMALL.**
- The **sizing bug** was 85% of the gap: contracts were sized against SPX-scale
  risk (~$700/ct) while executing SPY (~$70/ct real). The identical engine that
  pays **$4.7/night at 1 contract pays $63–86/night at 8**.
- The **VWAP gate loses money in all 16 tested cells** (removes 21% of nights AND
  lowers $/night AND t-stat). Killed.
- **Time-stop 30 → 15 min** before close: +$16–18/night (head-to-head rerun).
- **TP90 → TP95**: better in 8/8 cells. **Entry cutoff 14:00 → 15:30**: +$7.3k
  with the same worst night.
- The headline +$5,479 you were staring at was the OLD config at 1/10th size with
  4 bug-era call trades on the ledger. **Every losing trade pre-dates every fix.**

**Can it print $1,000/night on $10k? NO — not in this strategy class, not honestly.**
At 8 contracts, $1k nights are mathematically impossible (max ~3 TPs × ~$350 ≈
$800); at 15 contracts only ~3% of nights touch $1k and you run a **91% chance of
a 30%-drawdown year and up to 9% bust risk** — the market shuts the project down
before you do. Your $1k/night era was a different game (long-gamma catalyst
scalping at full leverage) plus survivorship; defined-risk premium selling
structurally cannot average 10%/day.

## 2. What it CAN do (the honest money table)

Final config **X**: conf≥2 · 30Δ · TP95 · **no VWAP gate** · time-stop 15 min ·
entries till 15:30 ET · events-blocked (FOMC/NFP/OPEX) · max 3 trades/night ·
skew + ratchet. Canonical backtest row (SPX-scale per contract, cap 3/night):

| era | trades | nights | $/night/ct | worst night | green | t |
|---|---|---|---|---|---|---|
| ALL (2022–26) | 1,507 | 822 | +$89.2 | −$1,078 | 67% | 7.0 |
| **2024+ (now-regime)** | 888 | 458 | **+$106.0** | −$1,078 | 69% | 5.9 |

At account scale ($10k, SPY = SPX$/10 × contracts), 2024+ era, **before** live
slippage:

| size | avg $/night | worst night | monthly (~21 nights) |
|---|---|---|---|
| 6 contracts (ramp, NOW) | **+$64** | −$647 | ~$1,330 |
| 8 contracts (after fills verify) | **+$85** | −$863 | ~$1,790 |
| 15 contracts (REJECTED) | +$159 | −$1,617 | 91% chance of a 30%-DD year |

**Cost haircut (the planning number):** the edge's breakeven is ~$70 RT/spread
(SPX-scale). At the realistic ~$40 leg, expect **~27% less** → plan on
**$60–90/night at 8 contracts ≈ $1,100–1,500/month = 11–15%/month on $10k.**
That is already 3–5× professional premium-selling benchmarks. Anything above is
upside, not plan.

**The honest road to $1k/night:** compound ~10.7%/month → ~$160k of capital →
$1k/night at the same risk discipline, in ~27 months IF the edge holds. Not by
levering $10k.

## 3. Risk — what actually kills it
- **Worst observed night** at 8 cts: −$964; hard defined-risk ceiling −$1,560
  (3 trades gapping through stops). Median 12-mo max-DD ~$2.3–2.5k; ~1-in-3
  chance of a 30%-DD year at realistic costs; bust (<50%) risk <0.5% at 8 cts.
- **Fills decide everything.** The edge lives between $25 and $70 RT cost. If
  live slippage runs $55+, $/night halves and t thins to ~2.5. Measured tonight:
  IC market orders gave ~19% slippage vs mid. `broker_realized_pnl` is live —
  judge the first 2–3 weeks on it.
- **Regime:** 2024+ edge (~$106/night/ct) is ~2× the 2022–23 era. Both positive,
  but the mandate math only works in the current regime.
- **Discipline is load-bearing:** the 3/night cap, 15-min time-stop, stops, and
  event-block own the tail. Every documented blowup in this class came from
  overriding exactly these after a winning streak.
- In-sample honesty: t=7.6 came from a 16-cell grid → expect live 10–25% under
  model from selection alone.

## 4. What changed (all LIVE as of tonight)
- **Sizing**: execution-scale fix (`exec_scale`); ramp **6 contracts** now →
  **8** after 2–3 weeks of clean fills (risk 4.5% → $500 cap; 15 rejected).
- **Gates**: VWAP gate OFF · time-stop 15 · cutoff 15:30 · conf≥2 · VIX<30 ·
  events-block ON · 3/night cap (captures ~all of uncapped P&L).
- **Strikes/exits**: 30Δ · TP95 · skew pricing · vol-ratchet (all validated).
- **First end-to-end live trade tonight**: IC built → executed → filled ($40 real
  credit) → market dumped → breakeven stop fired → **closed at −$23** (saved $137
  of the $160 max). The machine works; that loss was managed, not suffered.

## 5. Scoreboard that matters from here
1. **Realized cost per spread** (broker fills vs model) — the edge's life bar.
2. **$/night vs the $60–90 band** at current size, 2024+ model.
3. **Green-night rate vs 69%** and worst-night vs −$647 (6 ct) / −$863 (8 ct).
4. ~20 clean nights → size 6→8. ~3 months inside bands → revisit 10. Never 15.
