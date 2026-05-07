# ZeroDTE — Exploration & Build Plan

**Status**: Research/exploration. NO code yet. Awaiting approval before any build.
**Created**: 2026-05-07
**Origin**: Pivot from CryptoTrader (frozen). Caspar's documented 28-day track record = $1k/night on 0DTE credit spreads. The strategy works; the gap is execution support and macro-news awareness.

---

## What you have (the inputs to this project)

### A documented working strategy
- **28 days × ~$1k/night = ~$28k/month** track record on 0DTE put + call credit spreads
- Mean-reversion intraday via short-premium credit spreads
- End-of-day iron condor for overnight expiration
- Indicator stack: Williams Vix Fix (WVF), RSI, Stochastic %K/%D, Fib retracement
- 20-30% profit target per leg

### A documented failure mode
- **Operation Midnight Hammer (Trump → Iran strike) blew up your account**
- Root cause: not tracking macro news; iron condors got run over by directional break
- Fix: macro news awareness layer + macro-event blackout windows

### A clear ask
- Build a **dashboard** to support the strategy (not autonomous trading)
- You execute trades; tool helps you decide

### What's TBD
- Brokerage (you'll provide later — IBKR via your existing FinancePWA setup is the obvious default)
- Underlying (SPX likely — most popular for 0DTE credit spreads, cash-settled, no pin risk; also QQQ/IWM)
- Account size
- Position size limits

---

## Strategy codified (one-page playbook)

```
┌─────────────────────────────────────────────────────────┐
│ ZERO-DTE CREDIT SPREAD PLAYBOOK (intraday)              │
├─────────────────────────────────────────────────────────┤
│ 09:30 ET   Market open                                  │
│ 09:45 ET   END OF OBSERVATION WINDOW                    │
│            Classify regime:                             │
│              VOLATILE if:    VIX1D > X OR               │
│                             first-15-min range > 1.5×ATR│
│              NON-VOLATILE if: opposite                  │
│                                                          │
│ IF NON-VOLATILE — INTRADAY MEAN-REVERSION CYCLE:        │
│   Step 1: wait for rally (price near upper Fib)         │
│   Step 2: at peak (RSI > 70 OR WVF spike OR %K > 80     │
│           crossing down %D) → sell CALL credit spread   │
│   Step 3: wait for reversal                             │
│   Step 4: at trough (RSI < 30 OR Stoch oversold cross) →│
│           sell PUT credit spread                        │
│   Step 5: close call spread when 20-30% profit reached  │
│   Step 6: wait for next high → re-sell call spread      │
│   Step 7: close put spread on next reversal             │
│   ... cycle through session                             │
│                                                          │
│ IF VOLATILE — STAND ASIDE                                │
│   Don't fight a trending day with mean-reversion        │
│                                                          │
│ ~13:00 ET (~midnight SGT) — IRON CONDOR FOR EXPIRATION: │
│   Deploy IC with strikes WIDE                           │
│   Wing distance > expected intraday move                │
│   Fib retracement levels for strike anchoring           │
│   Let it expire OTM                                      │
│                                                          │
│ MACRO BLACKOUT (the missing piece):                      │
│   FOMC days, CPI/NFP releases, OPEX, geopolitical news  │
│   → no new positions; close existing iron condors       │
└─────────────────────────────────────────────────────────┘
```

---

## What the dashboard needs to do

| Component | Priority | Description |
|---|---|---|
| **Macro news feed** | P0 | The missing piece. Real-time news + economic calendar + geopolitical events with severity tagging. Loud alert for high-severity items during your session window. |
| **Regime classifier** | P0 | Computes at 09:45 ET: VOLATILE / NON-VOLATILE. Inputs: VIX1D, first-15-min range, intraday IV. Color-coded display. |
| **Indicator panel** | P0 | Real-time WVF + RSI + Stoch %K/%D on the underlying chart. Visual cues when reversals fire (e.g., RSI cross-back from 70). |
| **Options chain** | P1 | Real-time option chain with strikes, premiums, Delta, Theta, Vega, Gamma. Highlight strikes near your Fib levels. |
| **Strike-suggester** | P1 | Given current price + Fib levels + your indicator state, suggests "you'd typically sell call credit spread at K1/K2 here". |
| **Position tracker** | P0 | Open spreads, distance from strikes, P&L, days/hours to expiration, profit-target progress (e.g., "67% of target hit; close suggested"). |
| **Profit-target alert** | P1 | Audible/visual alert when an open spread hits the 20-30% profit zone. |
| **Fib retracement** | P1 | Auto-drawn intraday Fib (typically anchored to overnight range or pre-market high/low). |
| **IV rank / percentile** | P2 | tastytrade-style "is implied vol high or low" gauge. Sell vol when IV high. |
| **P&L journal** | P2 | Auto-log each closed spread; daily/weekly P&L; track edge over time. |
| **Macro blackout flag** | P0 | Warns "FOMC at 14:00 ET — close iron condors before". Maps known event calendar to blackout windows. |

P0 = critical (especially macro news, given the blow-up).
P1 = high value, build after P0.
P2 = nice-to-have.

---

## Phased build proposal

### Phase 0 — Research & spec (this session, ~2 hours)
- This document: strategy codification + dashboard requirements
- Decide brokerage + data sources
- Pick tech stack
- Write project README + architecture doc
- **HALT for approval**

### Phase 1 — MVP dashboard (~1-2 days, no broker yet)
- Project scaffold: Python backend (FastAPI), PWA frontend
- Macro news feed: fetch from a free news API + economic calendar
  - **Candidate**: Finnhub free tier, NewsAPI, Polygon news
  - Economic calendar: ForexFactory or Investing.com calendar scrape
- Regime classifier: pulls VIX1D + SPX 1-min bars from a free source (Polygon free tier or yfinance) → computes VOLATILE/NON-VOLATILE at 09:45 ET
- Indicator panel: WVF, RSI(14), Stoch %K(14)/%D(3) on SPX live chart
- Position tracker: MANUAL ENTRY (you type "I just sold an SPX 5800/5810 call credit spread for $1.20") + P&L computed against live mid
- Profit-target alerts: auto-monitor, beep at 25% profit
- Macro blackout calendar: hard-coded for next 30 days (FOMC, CPI, NFP), visible warning

### Phase 2 — Broker integration (~1-2 days, after you provide creds)
- IBKR or tastytrade API
- Real-time options chain
- Auto-fetch your open positions (replaces manual entry)
- Greek calculations
- Strike suggestions based on Delta % (e.g., "0.10 delta short strike = 10% probability of breach")

### Phase 3 — Strategy assistance (~1-2 days)
- Auto-suggest credit spread strikes when indicator confluence fires
- Auto-close suggestions when profit target hit
- Iron condor builder for end-of-day with Fib-anchored strike auto-selection
- Risk metrics per spread (max loss, BPR, breakeven prices, prob of profit)

### Phase 4 — Auto-execute (optional, only if you want)
- One-click order submission to broker
- Pre-confirm screen (so it's not pure auto)
- Kill switch + daily loss limit hard enforcement

---

## Tech stack proposal

| Layer | Choice | Reasoning |
|---|---|---|
| Backend | Python 3.13 + FastAPI | Same pattern as FXTrader / FinancePWA; you already know it |
| Frontend | PWA (Vite + vanilla TS or React) | Same pattern as FXTrader / FinancePWA; works on phone for after-hours review |
| Data — quotes | TBD: yfinance / Polygon free / IBKR (when integrated) | Phase 1 free; Phase 2+ via broker |
| Data — news | Finnhub free tier (60 calls/min, news + economic calendar) | Free start; upgrade if rate-limited |
| Data — options chain | IBKR (Phase 2) or tastytrade API | Live chains require broker API |
| Local DB | SQLite | Position log, journal entries, macro events |
| Realtime | WebSocket from broker; SSE for news polling | Standard |
| Indicators | Compute server-side in Python (numpy) | Same indicators as FX project; we have the helpers |
| Macro alerts | Browser notification + optional Telegram (you have telegram MCP) | Critical for the news-blow-up fix |

---

## What we kill / archive / keep

| Project | Status | Action |
|---|---|---|
| FXTrader (`~/Documents/Trading/FXTrader/`) | Already frozen at `fx-final-2026-05-07` | Keep as reference; do nothing. |
| CryptoTrader (`~/Documents/Trading/CryptoTrader/`) | Active research, paused | **Recommend: tag `crypto-paused-2026-05-07`, leave files in place. Don't delete.** Lessons in `docs/LESSONS.md` may carry over. |
| ZeroDTE (`~/Documents/Trading/ZeroDTE/`) | Just created | Active project going forward |

I'd carry forward LESSONS.md from CryptoTrader (the discipline rules apply to ANY systematic trading project). Add ZeroDTE-specific lessons as we learn them.

---

## Open questions before I start building

| # | Question | My default if you don't override |
|---|---|---|
| **Q1** | Brokerage? | **IBKR** (you have it from FinancePWA; lowest commissions for retail options; mature API). Alternative: tastytrade (specialized for options, native IV rank) |
| **Q2** | Underlying for 0DTE? | **SPX** (cash-settled, no pin risk, deepest 0DTE liquidity, $100/point). Alternatives: SPY (smaller contract, equity-settled, pin risk), QQQ |
| **Q3** | Where you'll be physically when trading? | Singapore (UTC+8). 09:30 ET = 21:30 SGT. Implies a desk-trading scenario in your evening; dashboard must be browser-based, mobile-friendly for status checks |
| **Q4** | Position-size cap? | Max risk per spread = 1% of account (e.g., $50k account → max $500 risk per spread; max 2-3 concurrent). User can override per trade. |
| **Q5** | Build location? | `~/Documents/Trading/ZeroDTE/` (already created). Repo: separate from CryptoTrader/FXTrader. |
| **Q6** | Should I also archive (git tag) the CryptoTrader project before pivoting? | YES — match the FX pattern with `crypto-paused-2026-05-07` tag |
| **Q7** | Do you want auto-execute eventually, or always manual? | **Always manual.** Your edge IS your judgment; the tool surfaces info, you decide. |

---

## Falsification — when does this project itself fail?

This isn't a backtest hypothesis (your strategy already has live track record). But the PROJECT could fail if:
1. **Data feeds prove unreliable for real-time** — Phase 1 free APIs may rate-limit during high-volume periods (when you most need them); broker integration becomes mandatory not optional
2. **Macro news feed has too many false alarms** — if every minor headline triggers an alert, you'll mute it and we're back to the Iran problem
3. **Latency is too high** — 0DTE moves in seconds; if dashboard refreshes every 5s, it's too slow for real-time decisions
4. **You stop using it** — most useful test: after 2 weeks of trading with the dashboard, do you still use it or has it become noise?

Mitigation: build incrementally, you use it daily, we tune based on actual feedback.

---

## What I'll do next IF YOU APPROVE

1. Tag CryptoTrader as paused (`crypto-paused-2026-05-07`)
2. Initialize ZeroDTE git repo
3. Write `README.md`, `LESSONS.md` (carry forward), `architecture.md`
4. Build Phase 1 MVP (~1-2 days work):
   - Project scaffold
   - Macro news feed (Finnhub free tier)
   - Regime classifier (yfinance for SPX/VIX1D)
   - Indicator panel (WVF/RSI/Stoch on SPX)
   - Manual position tracker
   - Profit-target alerts
   - Macro blackout calendar (hard-coded for now)
5. **HALT** before Phase 2 (broker integration) until you provide credentials

---

## Decision

**Approve with default Q1-Q7?** I'll proceed Phase 0 (archive + scaffold) immediately, then start Phase 1.

**Override any of Q1-Q7?** Tell me which.

**Want different scope?** (e.g., skip the dashboard, just build a news/alerts service first; or focus only on the macro-blackout system to fix the specific blow-up problem and leave the rest as-is)
