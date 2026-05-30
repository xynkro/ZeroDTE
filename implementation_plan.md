# ZeroDTE PWA Dashboard — Implementation Plan

**Status**: Plan awaiting approval. NO code changes until approved.
**Created**: 2026-05-08
**Origin**: User reframed scope from "Pine signals only" to "full PWA dashboard with signals + suggested strike prices + premium + position tracking + macro news awareness."

The Pine indicator (already deployed to TV chart) becomes the **signal-source**. The PWA is the **trading-decision UI** on top.

---

## What the PWA must do

| # | Feature | Priority |
|---|---|---|
| 1 | Live SPX price + regime classifier (VOLATILE/NON-VOLATILE/PRE-OBS) | P0 |
| 2 | Predicted day HIGH / LOW for current session, drawn live | P0 |
| 3 | Indicator stack live: RSI(14), Stoch %K/%D, WVF | P0 |
| 4 | Signal alerts: "SELL CALL CS NOW" / "SELL PUT CS NOW" with audible/visual ping | P0 |
| 5 | **Suggested strike prices per signal** — given current price + projected boundary, surface specific short strike + long strike | P0 |
| 6 | **Live premium estimate** for the suggested spread (delta, theta, breakeven, max loss, max profit, BPR) | P0 |
| 7 | Position tracker: open spreads, P&L, distance to strikes, profit-target progress | P0 |
| 8 | Macro news feed (the missing piece that blew up the account) — high-severity events flagged | P0 |
| 9 | Macro blackout calendar — FOMC / CPI / NFP days flagged with hours-until-event countdown | P1 |
| 10 | Profit-target alert when open spread hits 25% profit | P1 |
| 11 | Iron condor builder — end-of-day suggested strikes for overnight expiration | P1 |
| 12 | One-click order submission (stretch — manual confirmation + dry-run mode first) | P2 |
| 13 | P&L journal — auto-log closed spreads, daily/weekly summary | P2 |

---

## Tech stack

| Layer | Choice | Reasoning |
|---|---|---|
| Backend | **Python 3.13 + FastAPI** | Same pattern as FXTrader / FinancePWA. You know it. WebSocket-friendly. |
| Frontend | **PWA (Vite + React/TypeScript)** | Same pattern as FinancePWA. Mobile-friendly for status checks. |
| Live data — SPX | **IBKR** (you already have it from FinancePWA) — alternative: Polygon | IBKR free tier covers SPX index + options chain |
| Live data — Options chain | **IBKR API** (`reqContractDetails` + `reqMktData` for each strike) | Real-time bid/ask + Greeks |
| Macro news | **Finnhub** free tier (60 calls/min) — economic calendar + headlines | Free; covers the gap |
| Real-time | **WebSocket** server-to-client; IBKR streams bars/quotes | <1s latency for signals |
| Local DB | **SQLite** | Position log, journal, macro events |
| Deployment | Localhost initially; Tailscale for cross-device access | You already use this for FXTrader |

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  IBKR API (TWS or Gateway running locally)               │
│  - SPX index live bars + last price                      │
│  - SPX options chain (0DTE strikes, premiums, Greeks)    │
└────────────┬─────────────────────────────────────────────┘
             │ TCP socket (IBKR's native API)
             ▼
┌──────────────────────────────────────────────────────────┐
│  Python backend (FastAPI)                                │
│  ├── ibkr_client.py    — IBKR API wrapper                │
│  ├── signals.py        — port of Pine v0.5 logic to Python│
│  ├── strikes.py        — given signal + price + boundary,│
│  │                       compute suggested strike pair   │
│  ├── premium.py        — fetch live premium for spread   │
│  ├── positions.py      — track open spreads + P&L        │
│  ├── macro_news.py     — Finnhub poll, economic calendar │
│  ├── ws.py             — WebSocket broadcast to clients  │
│  └── api.py            — REST endpoints                  │
└────────────┬─────────────────────────────────────────────┘
             │ WebSocket + REST
             ▼
┌──────────────────────────────────────────────────────────┐
│  PWA frontend (React + Vite)                             │
│  - Live SPX price ticker                                 │
│  - Regime banner (VOLATILE / NON-VOL)                    │
│  - Compact 5-min chart with predicted HIGH/LOW overlay   │
│  - Indicator panel: RSI, Stoch %K/%D, WVF                │
│  - SIGNAL CARD when signal fires:                        │
│    "SELL CALL CS — short SPX 7385 / long 7395 / credit   │
│     ~$1.45 / max loss $8.55 / breakeven 7386.45"         │
│  - Open positions panel                                  │
│  - Macro news feed (top of screen)                       │
└──────────────────────────────────────────────────────────┘
```

---

## Strike-suggestion logic

Given a SELL CALL signal at price P with projected_high H:

```
1. round_to_5(H + buffer) = short call strike  
   (place short strike SLIGHTLY BEYOND the projection — the projection is
    where you EXPECT price to stop, so short strike at projection itself
    is too tight; +1× the 5min ATR buffer keeps it safer)

2. short_strike + 5 (or +10) = long call strike (the wing)
   - 5pt wings collect smaller credit but lower max loss
   - 10pt wings collect more credit but higher max loss

3. Fetch live bid/ask for both strikes from IBKR options chain
4. Suggested credit = mid(short_call_bid, short_call_ask) − mid(long_call_bid, long_call_ask)
5. Max profit = credit
6. Max loss = wing_width − credit
7. Breakeven = short_strike + credit
8. POP (probability of profit) = ~1 − delta(short_strike) — heuristic
```

Same logic mirrored for SELL PUT CS.

---

## Architecture: MCP-driven (simplified after capability discovery)

The new TV Desktop MCP (78 tools, replaced old browser-control MCP) + IBKR MCP eliminate the need for a custom Python backend that re-implements indicators or directly connects to TWS:

```
┌────────────────────────────────────────────────────────────┐
│  TradingView Desktop (with ZeroDTE-RP Pine indicator)      │
│  ↓ CDP port 9222                                            │
│  TV MCP — exposes live quote, indicator values,             │
│           Pine drawings, screenshot                         │
└────────────────────────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│  Thin Python orchestrator (FastAPI, ~200 LOC)              │
│  - polls TV MCP every 5s for: quote, indicator values,      │
│    projected high/low, regime, signal flags                 │
│  - polls IBKR MCP every 30s for: account info, positions,   │
│    XSP options chain near projected boundaries              │
│  - computes strike suggestions (short = round_to_5(boundary │
│    + buffer); long = short ± 5)                             │
│  - WebSocket broadcasts state to PWA                        │
└────────────────────────────────────────────────────────────┘
                           ▼
┌────────────────────────────────────────────────────────────┐
│  PWA frontend (Vite + React + TS)                          │
│  - live state from WebSocket                                │
│  - regime banner, indicator readouts, signal card with      │
│    suggested strikes + premium, position tracker, news      │
└────────────────────────────────────────────────────────────┘
```

**Trade-off acknowledged**: TV must be running during trading hours for the live signal feed. If TV is closed, dashboard goes dark. This is acceptable for MVP because Caspar runs TV during trading sessions anyway. Phase 4+ can add a Python port of v0.5 logic for TV-independent operation if needed.

## INSTRUMENT DECISION: XSP not SPX (LOCKED for MVP)

| | SPX | **XSP (Mini-SPX)** |
|---|---|---|
| Underlying | S&P 500 index | S&P 500 index (SAME) |
| Multiplier | 100× | **10×** |
| Cash-settled | ✅ | ✅ |
| Pin/assignment risk | None | None |
| Max risk per 5pt spread | ~$500 | **~$50** |
| IBKR restriction (verified) | `"restricted": "IOPT"` | none ✅ |
| Suitable for SGD 8.8k account starting at $1k risk | No (50%+ BPR/spread) | **Yes (5% BPR/spread)** |

XSP gives identical signal applicability (same S&P 500 ticker) at 1/10 the risk. Cash-settled like SPX. We trade XSP options. Display continues on SPX TV chart for context.

## Phased delivery (compressed thanks to MCP-driven architecture)

### Phase 0 — DONE (this session)
- Project scaffold at `~/Documents/Trading/ZeroDTE/`
- Pine v0.5 (loose triggers, 6-bar cooldown, multi-fire/day) saved to TV account
- ZeroDTE-RP deployed to live SPX 5m chart via TV MCP
- IBKR MCP authenticated; XSP/SPX/SPY contracts verified
- 60-day SPX 5min backtest data captured + Python port of v0.5 logic built (`backend/scripts/backtest_predictor.py`)

### Phase 1 — Thin Python orchestrator + WebSocket (~half day)
- `backend/app/orchestrator.py` (~200 LOC): polls TV MCP + IBKR MCP, computes strike suggestions, broadcasts via WebSocket
- `backend/app/api.py`: FastAPI WebSocket endpoint
- Verify: when ZeroDTE-RP's SELL CALL flag flips to true on TV, orchestrator emits signal event with suggested XSP strike pair + IBKR premium quote

### Phase 2 — PWA frontend (~1 day)
- `frontend/` Vite + React + TS
- WebSocket client; live state rendering
- Components: regime banner, mini chart with projected H/L overlay, indicator panel (RSI/Stoch/WVF), signal card (XSP strikes + credit estimate + max loss + breakeven), position tracker (manual entry initially)
- Tailscale-accessible

### Phase 3 — Macro news + alerts (~half day)
- Finnhub free tier integration (60 calls/min)
- News severity tagging, top-of-screen feed
- Hardcoded macro blackout calendar (FOMC/CPI/NFP for next 30 days)
- Browser notification on signal fire + profit-target hit

### Phase 4 — Iron condor builder + P&L journal (~half day)
- End-of-day workflow at 13:00 ET — suggested IC strikes (wider than intraday spreads, anchored to projected H/L)
- Auto-log positions on close, daily/weekly summary

### Phase 5 — Auto-execute via IBKR MCP (STRETCH, ~half day)
- IBKR MCP has `place_order` style tools (need to verify schema)
- One-click order submission with confirmation modal
- Hard daily loss limit + max trades/day enforcement at orchestrator level
- Caspar's "stretch dream" — not in MVP

**Total Phase 1+2+3+4 = ~2.5 days end-to-end.** Phase 5 if approved adds ~half day.

---

## What this DOES NOT do (out of scope unless approved)

- Replace your TradingView chart for visualization (PWA shows mini chart for context; TV stays as your primary chart tool with the v0.5 Pine indicator)
- Trade futures, indices other than SPX, or anything besides 0DTE credit spreads + iron condors
- Backtest the live strategy (the `backend/scripts/backtest_predictor.py` already exists for offline validation)
- Cross-exchange (everything routes through IBKR)
- Macro position sizing (we don't auto-size; you choose contract count based on the suggested premium + your risk tolerance)

---

## Critical decisions before I start building

| # | Decision | My default if you don't override |
|---|---|---|
| **D1** | Broker / data source | **IBKR** (already have it for FinancePWA, supports SPX options) |
| **D2** | Project structure | New repo at `~/Documents/Trading/ZeroDTE/` (already created); reuse FXTrader/FinancePWA patterns |
| **D3** | Frontend framework | **Vite + React + TS** (matches FinancePWA — code reuse) — alternative: vanilla TS + lit-html (lighter) |
| **D4** | Wing width default | **5pt** (smaller credit, lower max loss; conservative for live-trading-with-news-blowups history) |
| **D5** | Signal logic source | **Pine v0.5** ported faithfully to Python (drop-in compatible — no re-tuning) |
| **D6** | Manual position entry vs auto-detection | **Manual entry initially** (you tell the dashboard "I sold X"). Phase 5 adds auto-detection from IBKR position feed. |
| **D7** | Sizing decisions | **Manual — dashboard suggests, you decide contract count.** No auto-sizing. |
| **D8** | Tailscale exposure | **Yes** — same as FXTrader. Access from phone. |

---

## Pre-build checks (need 5-min answers from you)

1. **Is IBKR TWS or Gateway running on your Mac right now?** (Required for live data.)
2. **What's your IBKR plan — do you have SPX options market data subscription?** (Real-time vs delayed matters for signals; if delayed, signals fire late.)
3. **What's your typical trading account size?** (For position-size suggestions; doesn't need to be exact, just the order of magnitude.)
4. **Confirm 0DTE on SPX** — index options, 100x multiplier, cash-settled. Or do you mean SPY (ETF, smaller, equity-settled)?
5. **Do you want Tailscale access** so you can check the dashboard from your phone during trading hours?

---

## Reply format

- **"approved D1-D8 defaults"** → I start Phase 0 (IBKR capability check) immediately, then Phase 1 build
- **Override any of D1-D8** → tell me which
- **Pre-build answers** → please answer Q1-Q5 above so I know what infrastructure I'm building against

I expect Phase 1 + Phase 2 to land in 2-3 days end-to-end. Phase 3 + 4 add another day. Phase 5 (auto-execute) is the stretch.
