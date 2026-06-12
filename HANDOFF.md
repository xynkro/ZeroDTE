# ═══════════════════════════════════════════════════════════════════════════
# 📍 CURRENT STATE — 2026-06-12 (read THIS first; history below is context)
# ═══════════════════════════════════════════════════════════════════════════
#
# PORTFOLIO = TWO BOOKS, both live on Alpaca paper (SPY @ 1/10 SPX scale):
#   1. MEIC (the "Mech") — multiple-entry iron condors, ladder 11:00/12:00/13:00/
#      14:00 ET × 1 contract, each its own breakeven stop. Backtested
#      (scripts/meic_backtest.py): +$464/day SPX-scale/1ct, 73% green nights,
#      t=21.9. Modeled on "Math Makes Money / AI Robot Phil" (his ~10%/mo
#      independently confirms our audit ceiling). MEIC_ENABLED=true.
#   2. WAVE (the "Core") — directional credit spreads. Config from the money
#      audit (docs/WAVE_AUDIT.md): conf≥2 · 30Δ · TP95 · 6 contracts ramp.
#
# LIVE TRACK RECORD (real Alpaca fills, managed red by design):
#   Jun-10 IC −$23 · Jun-11 MEIC −$62 (4/4 stopped, trend day; stops capped it).
#   Signal so far: fills run ~19% RICHER than the BS model (backtest UNDERSTATES
#   the condor book). Wave gated on confluence both nights — picky, not broken.
#
# DEBRIEF + IMPROVEMENT LOOP (commit 13a00bf) — the discipline layer:
#   - Nightly debrief auto-runs in EOD (backend/app/debrief.py build_ic_debrief):
#     per-rung status + REAL book P&L + slippage-vs-model + stop-rate-vs-backtest.
#     Writes docs/debriefs/YYYY-MM-DD.md + appends backend/data/debrief_log.jsonl.
#   - scripts/improve_loop.py (WEEKLY, human-gated, applies NOTHING): SAMPLE GATE
#     (≥20 condor nights / 25 wave trades) + BACKTEST GATE (change must beat live
#     config on t-stat AND worst-day after credits rescaled to measured slippage).
#     At N=2 it correctly REFUSES to tune. Do NOT bypass the gates.
#
# OPS — the whole engine runs ON THE MAC (launchd com.caspar.zerodte-backend,
#   now wrapped in `caffeinate -i` so it holds the Mac awake; lid must stay open).
#   Mac asleep = no trades, open condors UNMANAGED, no Telegram. Cloud watchdog
#   (.github/workflows/watchdog.yml) Telegrams if the heartbeat goes stale.
#   Claude is NOT in the trade loop — deterministic Python trades via Alpaca API;
#   Claude = engineer (tune/backtest/debug) + optional once-a-day macro veto only.
#
# NEXT CHECKPOINTS:
#   • Fri 2026-06-19 — first weekly loop review (read the assumption tracker, not P&L).
#   • ~mid-July (≈20 clean condor nights) — first point the loop CAN propose a
#     backtest-confirmed tuning change. The real decision fork.
#   • Ramp MEIC 1→2 contracts only after clean fills validate over ~2-3 weeks.
#   • Queued: CBOE-mid limit-order execution; reverse-reconcile 2 unexplained
#     Jun-5 mlegs; VPS migration (uptime upgrade) once paper numbers earn it.
#
# SECURITY (verbatim, persist): .env is gitignored — NEVER commit it. The 21
#   paper equity positions (AMD/AVGO/ENPH…) + casaa-* orders are CasaaFinance's,
#   NOT ZeroDTE's — never close/touch them. paper-api.alpaca.markets ONLY, never
#   real money. Never hit the kill-switch / close_all_positions in tests.
# ═══════════════════════════════════════════════════════════════════════════

# ZeroDTE Strategy Pivot — Context Handoff

> ## ⚠️ CORRECTION (2026-05-29) — the DEPLOY verdict below is INVALID
> The backtest that produced "DEPLOY 72/100, 81-94% WR, +$5.6k" used a **power-law
> underlying-move proxy** for spread P&L. That proxy booked a 10%-credit "win" on a
> **0.008% favorable tick** (median 1 bar to exit) and therefore reported **~0 breaches** —
> trades closed before any breach could register. It also never priced the deployed
> strikes: `short_delta=40` had no entry in the backtest's delta table and silently fell
> through to 0.50% OTM / $200 credit, while live ran 0.22% OTM / $400 credit.
>
> **Honest re-validation with Black-Scholes repricing** (`backend/app/honest_backtest.py`,
> `bs_pricing.py`): the deployed **40Δ/TP10/ladder** config is **NEGATIVE** (≈ −$3.5k,
> positive only 1/5 years) despite a 91% win rate — tiny wins can't cover full-credit losses.
> The "TP 10-50% plateau" is actually a cliff (TP50 → −$3.4k). The pivot thesis (scalp 10%,
> ratchet stops) is **backwards**.
>
> **New live config (shadow): 30Δ / TP90 / no-ladder / `DIRECTIONAL_PNL_MODEL=bs`.**
> Honest backtest: +$5,479, DD −$1,581, positive every year 2022-2026. Confidence **moderate
> (0.6)** — small sample (153 trades), flat-IV BS. Validate live before any real-money flip.
> Do NOT trust the legacy proxy backtest (`directional_spread_backtest.py`) for validation.

**Date**: 2026-05-16 (superseded 2026-05-29 — see correction above)
**Status**: Pivot implementation complete and live in shadow mode. Pending: Pine indicator update, 4-6 week shadow validation.

---

## TL;DR

We pivoted the entire ZeroDTE strategy from a symmetric Iron Condor + static-TP Wave hybrid to a unified **directional credit spread** approach. The new strategy is live in shadow mode (`DIRECTIONAL_SPREAD_ENABLED=true`) running alongside the legacy `wave_manager` for safe rollback. Backtest validation across 4.4 years of SPX data (2022-05 → 2026-05) returns DEPLOY verdict from the backtest-expert evaluator (72/100).

---

## Why The Pivot

**Original user complaint:**
- IC strategy failing 2/4 days a week
- Strikes so far OTM (5-8Δ at low VIX) that premiums were pennies
- User's pre-blowup strategy that made $1K/day used 0.15-0.20Δ — much closer to ATM
- Felt the system was "missing a directional IC" and current strategies were "shady"

**Research surfaced** (in chronological order during the session):
1. **YouTube credit spread tutorial** (1h 47min) — Key insights:
   - "The best losers are the best winners"
   - Dynamic stop-loss ladder: at 50% TP → stop to BE, at 75% TP → stop to 50% locked, at 90% TP → stop to 75% locked
   - 94% WR account showed $43k profit over 2mo using these exact rules
   - Single-sided (puts only in uptrend) outperforms symmetric IC
   - Avg win $2,600, avg loss $1,900 (active management keeps losses below max)

2. **Tastytrade research** — Critical insight on 0DTE math:
   - 0DTE spreads have **10× the gamma exposure** of 45DTE spreads
   - 0DTE spreads collect only **1/8 the credit** of 45DTE spreads at same delta
   - Net result: **1 contract of 0DTE = ~1 contract 45DTE in directional risk**
   - Canonical 0DTE = 35Δ short / 25Δ long / $10 wide
   - At low VIX, far-OTM (5-8Δ) collects pennies for the same gamma risk → blow-up trade

3. **ThetaProfits 9k-trade study + Option Alpha 25k-trade study**:
   - Directional bias beats symmetric IC over time
   - 10-15Δ symmetric IC is +EV but tail-risk heavy

**The convergence**: Single-sided + 30-40Δ short + dynamic stops + small wings is the canonical 0DTE setup. Everything in our system was wrong direction (5Δ symmetric IC with no active management).

---

## What Was Built

### 1. Backtest module — `backend/app/directional_spread_backtest.py`

Stand-alone validation module. Doesn't touch the existing IC/wave backtests. Key features:
- Uses Wave predictor's signals (confluence + Pullback trend filter)
- Adds confluence filter (≥3 of 4 factors) + mandatory VWAP gate
- Maps `short_delta → %OTM` via static lookup (calibrated for low-VIX 0DTE)
- Approximates spread P&L from underlying movement (quadratic model for gamma curvature; linear option for sanity check)
- Dynamic stop ladder tracking peak `pct_kept`
- Intra-bar extremes evaluated to fire stops before bar close (prevents catastrophe stop from over-firing)

### 2. Strategy module — `backend/app/directional_spread_manager.py`

Live execution counterpart to the backtest. Same P&L model, same ladder logic. Functions:
- `open_directional_trade()` — builds PaperTrade with strategy="directional_spread"
- `check_exit()` — called per bar by orchestrator, evaluates ladder + catastrophe stop + TIME stop
- `spread_pct_kept()` — same quadratic/linear approximation as backtest
- Constants `DELTA_TO_OTM_PCT` and `DELTA_TO_CREDIT_PCT` (calibrated to 0DTE pricing)

### 3. Orchestrator wiring — `backend/app/orchestrator.py`

`_open_paper_trade()` now routes via `settings.DIRECTIONAL_SPREAD_ENABLED`:
- True → uses `directional_spread_manager.open_directional_trade()` with 40Δ/$10 wing strikes (SPX)
- False → falls through to legacy `wave_manager.open_wave_trade()` (XSP/SPX based on wave_strikes pick)

`_check_open_wave_trades()` now dispatches per-trade based on `trade.strategy`:
- "directional_spread" → `directional_spread_manager.check_exit()`
- otherwise → `wave_manager.check_exit()`

### 4. New PaperTrade fields — `backend/app/models.py`

Added to support ladder state across bars:
- `strategy: str = "wave"` (legacy default; "directional_spread" for post-pivot)
- `peak_pct_kept: float = 0.0`
- `current_stop_pct_kept: float = -100.0`
- `breakeven_dist_pct: float | None = None`
- New outcomes: `tp_target_hit`, `stop_ladder_hit`, `breach_max_loss`
- `StrikeSuggestion.mode` now accepts `"directional_spread"` alongside `"wave"`/`"iron_condor"`

### 5. API endpoint — `backend/app/api.py`

`GET /api/backtest/directional_spread` with full param surface. Returns same shape as wave/IC endpoints.

### 6. Telegram exit messages — `backend/app/telegram.py`

`ping_signal_exit()` now handles new outcomes:
- `tp_target_hit` → 🎯 TP HIT — 10% credit captured
- `stop_ladder_hit` → 🪜 STOP LADDER — profit ratcheted
- `breach_max_loss` → 🛑 MAX LOSS — close-through-strike
- `max_profit_otm` → ✅ EOD — expired OTM (max profit)

### 7. Configuration — `backend/app/config.py` + `.env`

New settings (defaults in parentheses are the locked-in winner):
```
DIRECTIONAL_SPREAD_ENABLED=true   # Toggle the pivot
DIRECTIONAL_SHORT_DELTA=40        # Short leg delta
DIRECTIONAL_WING_DOLLARS=10       # SPX wing width
DIRECTIONAL_TP_TARGET=10          # 10% credit captured = take profit
DIRECTIONAL_LADDER_50=50          # Peak >50% → stop to BE
DIRECTIONAL_LADDER_75=75          # Peak >75% → stop to 50% locked
DIRECTIONAL_LADDER_90=90          # Peak >90% → stop to 75% locked
DIRECTIONAL_PNL_MODEL=quadratic   # P&L approximation (quadratic | linear)
```

### 8. Historical data extension — `backend/scripts/extend_historical_data.py`

Pulls SPY 5m bars from Alpaca going back to 2022-01-03, scales ×10 to SPX-equivalent, saves to `backend/data/historical/SPX_5m_3y.json`. **84,959 bars covering 4.4 years**. The `directional_spread_backtest` auto-picks this file when `data_window="auto"`.

---

## Backtest Validation Results

### Final winner config: **40Δ short / $10 wing / conf≥3 / VWAP gate / TP10% / dynamic ladder**

**On 4.4 years SPX data (2022-05 → 2026-05):**

| Metric | Linear P&L model | Quadratic P&L model | Mean |
|---|---|---|---|
| Trades | 153 | 153 | 153 |
| Win rate | 81.1% | 94.1%* | ~87% |
| Total P&L | +$6,603 | +$4,703* | ~$5,650 |
| Max DD | -$1,431 | -$344 | -$846 |
| Capture % | 8.8% | 15.8% | ~12% |

*The quadratic model shows a higher WR but lower total because more trades hit the 10% TP threshold intra-bar (more small wins, fewer chases to full credit).

**Per-year (mean linear+quadratic):**

| Year | Trades | WR | P&L |
|---|---|---|---|
| 2022 (bear regime) | 25 | 60-64% | +$45 |
| 2023 | 40 | 82-85% | +$2,305 |
| 2024 | 36 | 83-86% | +$2,097 |
| 2025 | 39 | 67% | +$471 |
| 2026 (partial) | 13 | 54-69% | +$172 |

**POSITIVE IN EVERY YEAR INCLUDING 2022 BEAR REGIME** ✓

**Slippage stress test** (assumed 0-20% friction on credit):
| Slip | Total P&L | Max DD |
|---|---|---|
| 0% | +$5,943 | -$1,060 |
| 5% | +$5,613 | -$1,007 |
| 10% | +$5,283 | -$954 |
| 20% | +$4,623 | -$849 |

**Backtest evaluator verdict**: **DEPLOY (72/100)**

Dimension scores:
- Sample Size: 17/20 (153 trades, close to ideal 100+)
- Expectancy: 13/20 (~$27/trade after mean)
- Risk Management: 13/20 (WR strong but WL ratio is 0.29 — high WR but small wins)
- Robustness: 9/20 (only 4 years tested; want 5+)
- Execution Realism: 20/20 (slippage tested, plateau confirmed)

Only red flag: "Only 4 years tested — may miss regime changes (minimum 5 recommended)."

---

## Current Live State

- **Backend**: Running on port 8765 (last started 2026-05-15 18:46 ET; auto-restarted with new code)
- **Feed**: Alpaca IEX, connected (`alpaca_ready=true`)
- **Trading**: `TRADING_ENABLED=false`, `SHADOW_MODE=true` (paper only)
- **Strategy**: `DIRECTIONAL_SPREAD_ENABLED=true` (new strategy active)
- **TradingView**: Both Wave v2 + IC V2 Pine scripts on chart (legacy display only — backend logic now runs directional strategy regardless of what the indicators show)

### Verified working

✓ Backend starts cleanly with new strategy enabled
✓ `/api/status` returns OK with feed connected
✓ `/api/backtest/directional_spread` returns valid results (94.1% WR, +$4,703 on 4.4y quadratic)
✓ All Python imports clean (`directional_spread_manager`, `directional_spread_backtest`, orchestrator)
✓ `StrikeSuggestion` model accepts `mode="directional_spread"`
✓ Numpy types properly cast to native Python (FastAPI serialization works)

---

## Pending Work

1. **Pine indicator update** (cosmetic only — not blocking)
   - Currently the Wave v2 (604 lines) + IC v2 (283 lines) Pine scripts still display on TradingView
   - Backend ignores the IC v2 strike output and uses 40Δ/$10 wing instead
   - Wave v2's signal generation still feeds the predictor (signals are the right thing)
   - Optional task: collapse into a single "ZeroDTE Spread Engine" Pine indicator showing 40Δ strikes + dynamic stop ladder visualization
   - **Critical safety rule from prior session**: Only ever have ONE Pine script open in the Pine Editor at a time. Adding the OTHER script must be done via Indicators > My Scripts dialog. Otherwise `pine_save` writes the wrong source to the editor's saved tab.

2. **Shadow mode validation period** (4-6 weeks)
   - Let the system accumulate live signals + paper trades with the new directional strategy
   - Compare results to backtest predictions
   - Watch for: do live signals fire at the rate the backtest expected (~6/month)?
   - Watch for: is the WR holding above 70%?
   - Watch for: are catastrophe breaches happening more than backtest suggested (0 in 153 trades)?

3. **Decision after shadow validation**
   - If live results match backtest: retire `wave_manager`, delete IC builder, simplify orchestrator
   - If live results worse: revisit P&L model approximation (probably the issue — quadratic vs reality)
   - If live results catastrophically worse: rollback by setting `DIRECTIONAL_SPREAD_ENABLED=false`

4. **Optional future work**
   - Add VIX-aware delta scaling (higher VIX → lower delta short)
   - Real options chain integration (Alpaca paid tier or IBKR) to replace the static `DELTA_TO_CREDIT_PCT` lookup with live Greeks
   - Macro blackout integration (currently disabled in backtest — would tighten edge if added)

---

## Critical Files To Know

```
backend/app/
├── directional_spread_backtest.py  # NEW — validation module (612 lines)
├── directional_spread_manager.py   # NEW — live strategy module (227 lines)
├── wave_manager.py                 # LEGACY — preserved for rollback
├── orchestrator.py                 # MODIFIED — routes via DIRECTIONAL_SPREAD_ENABLED
├── models.py                       # MODIFIED — PaperTrade extended, StrikeSuggestion.mode expanded
├── config.py                       # MODIFIED — added DIRECTIONAL_* settings
├── telegram.py                     # MODIFIED — new outcome labels
├── api.py                          # MODIFIED — added /api/backtest/directional_spread

backend/scripts/
└── extend_historical_data.py       # NEW — Alpaca → SPX 5m fetcher

backend/data/historical/
├── SPX_5m_60d.json                 # legacy 60d data
├── SPX_5m_1y.json                  # legacy 12mo data
└── SPX_5m_3y.json                  # NEW — 4.4y data (2022-2026, 84,959 bars)

.env                                # MODIFIED — DIRECTIONAL_* defaults set

indicators/
├── zerodte_wave_v2.pine            # unchanged (604 lines)
└── zerodte_iron_condor_v2.pine     # unchanged (283 lines)
```

---

## How To Resume

### Continue shadow mode validation
1. Backend should already be running. Check: `curl -s http://localhost:8765/api/status | python3 -m json.tool`
2. Restart if needed: `lsof -ti :8765 | xargs kill; cd ~/Documents/Trading/ZeroDTE && source .venv/bin/activate && nohup python3 -m uvicorn backend.app.api:app --host 0.0.0.0 --port 8765 > /tmp/zerodte_backend.log 2>&1 &`
3. Verify directional strategy is active: `curl -s http://localhost:8765/api/state | python3 -c "import json,sys; d=json.load(sys.stdin); print([t.get('strategy','wave') for t in d.get('paper_trades',[])])"`
4. Live signals will populate `paper_trades` as they fire (look for `strategy: "directional_spread"`)

### Run a fresh backtest
```bash
# Easy way: hit the endpoint
curl -s "http://localhost:8765/api/backtest/directional_spread?short_delta=40&final_tp_target=10&data_window=3y" | python3 -m json.tool | head -30

# Python: explore configs
cd ~/Documents/Trading/ZeroDTE && source .venv/bin/activate
python3 -c "
from backend.app.directional_spread_backtest import run_directional_spread_backtest
r = run_directional_spread_backtest(short_delta=40, final_tp_target=10, data_window='3y', pnl_model='quadratic')
print(r['summary'])
"
```

### Rollback to legacy strategy
```bash
# Edit .env, change to:
DIRECTIONAL_SPREAD_ENABLED=false
# Restart backend. Wave manager + IC will fire as before.
```

### Pull more historical data (when 2027 rolls around or to extend back further)
```bash
cd ~/Documents/Trading/ZeroDTE && source .venv/bin/activate
# Edit START date in the script if pulling further back
python3 backend/scripts/extend_historical_data.py
```

---

## Key Decisions And Rationale

1. **Why 40Δ short (not 35Δ Tastytrade canonical or 20Δ user's old strategy)?**
   - 4.4y stress test winner with all years positive
   - Higher delta = more credit (~$400 vs $250 at 20Δ) which makes the 10% TP target meaningful
   - 35Δ TP30 won 12mo but degraded 2025-2026; 40Δ TP10 survived the regime shift

2. **Why TP at only 10% of credit captured?**
   - 0DTE doesn't give time to ride to max profit
   - Tastytrade scalp philosophy: take quick small wins
   - On 4.4y data: TP10 produced more wins per period and was regime-proof
   - Parameter plateau confirmed: profitable across TP 10-50%

3. **Why parallel deployment (not replace wave_manager)?**
   - Safe rollback path during 4-6 week shadow validation
   - Single boolean flip in .env reverts to legacy
   - Backtest gives high confidence but real signals may surface edge cases

4. **Why static `DELTA_TO_OTM_PCT` lookup instead of live Greeks?**
   - Alpaca free tier doesn't include options chain
   - IBKR works for live execution but we're in shadow mode now
   - Static lookup is calibrated for low VIX (which dominated the dataset)
   - Future upgrade: hook into Alpaca paid tier or IBKR option chain when going live

5. **Why quadratic P&L model is the default?**
   - Linear model under-weights gamma curvature (penalizes early small favorable moves)
   - Quadratic better reflects real 0DTE spread behavior near the strike
   - Both models tested in stress; strategy survives both
   - User can flip via `DIRECTIONAL_PNL_MODEL=linear` for conservative numbers

---

## Open Questions For Future You

1. Should the strike placement scale with current VIX (rather than static 0.22% OTM at 40Δ)?
2. Should we add a "regime detector" that pauses trading if recent N trades drop below 70% WR?
3. Should the catastrophe stop also fire on intra-bar wicks (currently only bar close)?
4. The 2026 partial year (13 trades) is weaker than 2023-2024 — is it noise or real regime change?
5. Do we want the Pine indicator updated for shadow mode visualization, or is the dashboard enough?

---

## Process Notes For Anyone Resuming

- The `backtest-expert` skill is what guided the validation methodology. Re-invoke it for any future strategy work.
- The `superpowers:brainstorming` skill should be invoked before any further strategy changes (not just code changes).
- The user (Caspar) ships fast, expects end-to-end execution, gives direct feedback. Match that pace.
- User's profile is in `~/.claude/projects/-Users-xynkro/memory/user_profile.md`. He's an active trader, not a hobbyist.
- The TradingView Pine indicator save-target bug is REAL — read prior session's notes on managing two scripts before touching the Pine Editor.
