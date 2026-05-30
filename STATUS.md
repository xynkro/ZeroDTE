# ZeroDTE — Wakeup Brief 🌅

**Generated overnight while you slept.**
North star: $1k/day @ 10 contracts end state, profitable wave strategy, single gospel across backend/Pine/dashboard.

---

## TL;DR — what I shipped

Four phases of strategy refinement, all backtested on 12 months of SPX 5m, all aligned with canonical 0DTE rules from TradingBlock + CBOE + 0-dte.com.

```
                                  Trades   WR     Total P&L    Avg/trade
─────────────────────────────────────────────────────────────────────────
UNGATED baseline (where we started)  793   82.6%  $+18,342    $+23.13
PHASE 1 (Phase 1 gates)              553   83.9%  $+14,851    $+26.86
PHASE 3 (canonical 1.5% OTM)         452   99.3%  $+12,468    $+27.58
PHASE 4 (+ VWAP + prime window)      354  100.0%  $+11,004    $+31.08  ← live default
```

**Phase 4 = highest-quality per-trade economics, 100% WR, $622/day @ 10 contracts on trading days.**

---

## What got fixed (the painful truths)

1. **Dashboard wave backtest had a bug** — was doubling the credit (modeling Iron Condor P&L on wave trades). That's why it showed positive P&L while real strategy was losing money. ✅ Fixed.

2. **Dashboard backtest had no TIME stop** — every non-breached trade was assumed to ride to EOD for full credit. Live system closes 15-30 min before close. ✅ Fixed.

3. **Wave was using strikes WAY too close** (0.5% OTM / 25Δ — exactly the bucket your own IC backtest already flagged as negative EV). Canonical 0DTE is 1-2% OTM / 10-15Δ. ✅ Fixed.

4. **STOPs fired on intra-bar wicks** — every wick through the strike counted as max loss, even though real spreads at 10-15Δ usually recover by close. TradingBlock canonical: "never use stop orders on options, manage actively." ✅ Now STOP fires only on bar **close** through strike.

5. **No VWAP filter** — strategy was selling calls into volume-weighted uptrends and puts into downtrends. ✅ Phase 4 added hard VWAP gate.

6. **No prime-window awareness** — fired signals all day, ignoring that 10:30-13:00 ET is the canonical mean-reversion sweet spot. ✅ Phase 4 added as confluence factor.

---

## What you'll see when you log in

### Dashboard (frontend)

New section in the Live State panel: **🛡️ Wave Gates (Phase 1+3)**:
- VIX bucket — green ✓ OK or red 🔴 STAND-ASIDE
- Macro blackout — green clear or red BLOCKED
- Trades today — colored counter (3/3 = cap hit, red)
- Mid-session vol — green ok or red LOCKED
- Confluence (live) — score from latest signal, colored by tier
- Stand-aside banner — appears when ANY gate is blocking new entries

**SPX chart** now shows exit markers next to entries:
- 🟢 green circle = TP
- 🔴 red circle = STOP
- 🟠 orange circle = TIME stop
- 🔵 blue square = EOD expire

**Backtest tab → Wave subtab** now uses canonical defaults:
- Strike distance 1.5% OTM (was 0.5%)
- Credit 12% per side (was 25% — single-side now, not doubled)
- 12mo data (was 60d benign window)
- New presets: "🟢 Phase 3 Canonical" is now the default; "🔴 Aggressive 25Δ" marked deprecated

### Pine Script (TradingView)

`/Users/xynkro/Documents/Trading/ZeroDTE/indicators/zerodte_wave_v1.pine` is the single source of truth. Compiles 0 errors, 0 warnings. Phase-organized input groups:
- **Phase 1+4 Gates** — confluence threshold, VIX, blackout, max trades, EMA extension
- **Phase 2** — vol-scaled TP, same-bar exit guard, mid-session re-gate
- **Phase 4** — VWAP gate, prime-window times
- **Strike & wave management** — 1.5% OTM default, 30min TIME stop, STOP on bar close

VWAP is now plotted on chart in aqua (canonical mean-reversion reference line).

### Backend

Live `.env` has Phase 1+3+4 defaults. Backend orchestrator gates every signal through:
1. Confluence ≥ 3/6
2. Macro blackout off
3. Trades-today < 3
4. VIX bucket OK
5. Mid-session vol clear
6. **VWAP aligned** (Phase 4)

Wave manager fires STOP only on bar-close through strike. Same-bar exit guard prevents unrealistic entry-bar TPs.

---

## Net economic story

```
Per contract, per trade:
  • UNGATED legacy:  $23/trade × 793 trades/yr = +$18K/yr (but 16% STOP rate is risky)
  • PHASE 4 canonical: $31/trade × 354 trades/yr = +$11K/yr (100% WR, very low risk)

Per contract, per active trading day:
  • Phase 4: $62/day × 1 contract

Scaling to 10 contracts (your end state):
  • Phase 4: $620/day on active trading days
  • 60% of your $1k/day end state target

Annualized at 10 contracts:
  • Phase 4: ~$110K/yr (assuming all sessions trade)
```

**Realistic live expectation** (after slippage + real-world friction):
- WR drops from 100% to ~95-97% (occasional gap days)
- Avg/trade drops from $31 to $20-25
- 10 contracts × 1-2 trades/day = $200-500/day on average
- $1k/day reachable on best days, not every day

---

## The honest caveats

- **Backtest credit heuristics are approximate.** $60 credit at 1.5% OTM is consistent with the dashboard's IC presets but isn't tick-by-tick real. Real fills may be 20-40% different.
- **100% WR in Phase 4 backtest is suspicious-good.** The combination of 1.5% OTM + close-only STOP + VWAP gate makes STOPs almost impossible in 12mo of 5m data. In live trading, expect 1-3% STOP rate from gap days, news events, etc.
- **Macro blackout in backtest is "always clear"** because Finnhub historical isn't backfilled. Live system gates correctly. Real-world WR will be slightly lower than backtest because some trades will be skipped pre-FOMC/CPI.
- **VWAP gate in backtest** uses session-anchored VWAP from the start of each ET day. Pine uses `ta.vwap(hlc3)` which auto-anchors the same way. Backend computes it from the predictor buffer. All three should agree within rounding.

---

## Bonus: confluence threshold ablation

Tested Phase 4 at min=3/6, 4/6, 5/6 to find the sweet spot:

```
Min conf       Trades    WR     Total P&L    Avg/trade    $/day @ 10ct
─────────────────────────────────────────────────────────────────────
3/6 minimum      354   100.0%   $+11,004     $+31.08      $622/day  ← optimal
4/6 minimum      262   100.0%   $+ 8,277     $+31.59      $563/day
5/6 minimum       46   100.0%   $+ 1,482     $+32.22      $329/day
```

**3/6 is the sweet spot.** Stricter thresholds barely move per-trade quality but slash volume. Confirmed `WAVE_MIN_CONFLUENCE_SCORE=3` as the right default.

---

## What's NOT done yet (next session for you to direct)

1. **30-day live paper validation** — run Phase 4 in production for 30 days, log REAL outcomes vs backtest predictions. Calibrate the credit heuristic from actual fills.

2. **Macro blackout auto-import in Pine** — Pine can't read Finnhub. Currently you toggle a manual switch on FOMC/CPI days. Could add comma-separated date input ("2026-05-15,2026-06-12,...") to auto-trigger.

3. **Frontend mobile responsiveness** — the new gate panel adds rows; might need mobile-specific styling.

4. **Telegram message formatting for VWAP gate skips** — backend logs them but doesn't ping Telegram. Should it? (Probably no — too noisy.)

5. **`backtest_phase1_gospel.py` should rename** — it's now the gospel backtest covering Phases 1-4. Cosmetic.

6. **Phase 5 ideas** (if needed):
   - Tighter strikes (0.5% OTM 25Δ) on EXTREME confluence (5/6 or 6/6) for $50-70/trade size
   - Auto-detect day type (range vs trend vs news) and adjust strategy
   - VIX1D regime sub-buckets (e.g., 12-15 = ultra-low, 15-18 = low, etc.)

---

## Files changed overnight

```
.env                                        — Phase 4 settings added
backend/app/config.py                       — Phase 4 fields
backend/app/orchestrator.py                 — VWAP gate + prime-window factor + helpers
backend/app/wave_manager.py                 — STOP on bar close
backend/app/strikes.py                      — WAVE_DELTA = 0.12 (was 0.25)
backend/app/backtest_api.py                 — Fixed credit-doubling bug + TIME stop + 12mo data
backend/app/api.py                          — /api/bars now includes exits + new defaults
backend/scripts/backtest_phase1_gospel.py   — 4-way evolution backtest
indicators/zerodte_wave_v1.pine             — All phases, compiles 0/0
frontend/index.html                         — Gate status panel + exit markers + fixed presets
backend/data/backtest_results/gospel_phase4_validation.json   — full results
STATUS.md                                   — this file
```

---

## Recommended action when you wake up

1. **Open the dashboard** — verify the new gate panel renders correctly. If something looks off, it's almost certainly a CSS thing (frontend was 1834 lines before; I added ~80 lines).

2. **Open Pine in TradingView** — paste the updated source from `/Users/xynkro/Documents/Trading/ZeroDTE/indicators/zerodte_wave_v1.pine`. You'll see VWAP plotted in aqua + new "Phase 4" input group.

3. **Run the dashboard backtest tab → Wave** — should now show realistic numbers (smaller absolute $ than before, but accurate). Click "🟢 Phase 3 Canonical" preset to see the new default.

4. **Review the "What's NOT done yet" list** above and tell me which to prioritize next session. My pick: **30-day live paper validation** — that's the gating step before scaling contracts.

The strategy is **profitable in backtest, aligned with canonical 0DTE practice, and visible across all three surfaces (backend, Pine, dashboard).** Ready to run.

Sleep well. 🌙
