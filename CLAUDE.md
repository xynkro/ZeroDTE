# ZeroDTE

A 0DTE SPX options trading engine (Python/FastAPI, deterministic — Claude is NOT in the trade loop). It runs **two books live on the Alpaca PAPER broker** at 1/10 SPX scale via SPY: **MEIC** (multiple-entry iron condors, `meic_backtest.py`) and **WAVE** (directional credit spreads, `directional_spread_manager.py`). It is past the original "shadow/logging-only" phase — it actively SUBMITS paper orders. **Read `HANDOFF.md` FIRST, every time, before touching anything** — it is the live state-of-the-world. The single hard boundary: this trades **paper money only**; do not flip it toward real money or any live broker.

## Hard rules — never break these
- **Paper only.** `ALPACA_BASE_URL` must stay `https://paper-api.alpaca.markets`; `PAPER_BROKER=alpaca`. Never point at a real-money Alpaca/IBKR endpoint, never raise `IBKR_PORT` 7497→7496. IBKR is a **data feed**, not an order path. Live-money flip happens only after the validation gates pass, by Caspar, deliberately.
- **Never touch CasaaFinance's positions.** The ~21 paper equity holdings (AMD/AVGO/ENPH…) and `casaa-*` orders share this paper account but belong to FinancePWA — never close/modify them. Never call the kill-switch / `close_all_positions()` in tests.
- **`.env` is gitignored — never commit it.** It holds Alpaca/Finnhub/Telegram keys + `API_WRITE_TOKEN`.
- **Don't bypass the improve-loop gates.** `scripts/improve_loop.py` proposes NOTHING until ≥20 clean condor nights / ≥25 wave trades AND a change beats live config on t-stat AND worst-day after slippage rescaling. At small N it correctly REFUSES to tune. Leave it.

## How we work here
- **Build/run:** runs continuously via launchd `com.caspar.zerodte-backend` (wrapped in `caffeinate -i`; Mac lid must stay open). Manual: `./start_backend.sh` (uvicorn on :8765). Health: `curl -s localhost:8765/api/status | python3 -m json.tool`.
- **Conventions:** Python 3.13, FastAPI + `ib_insync` + Alpaca REST/feed. All behavior is `.env`-flag driven (config.py reads it). Backtest before any strategy change (`backtest-expert` skill); brainstorm before strategy pivots (`superpowers:brainstorming`).
- **Done means:** backend restarts clean, `/api/status` shows feed connected, the nightly debrief (`debrief.py`) and `improve_loop.py` gates still pass — confirm before claiming it works.

## Architecture — only what isn't obvious from the code
- `orchestrator.py` — the engine; routes signals→trades, dispatches per-`trade.strategy`, owns the IBKR/Alpaca-feed + Alpaca-order split.
- `alpaca_trader.py` — the ONLY order-submission path; every method early-returns when `TRADING_ENABLED=false`.
- `bs_pricing.py` / `honest_backtest.py` — the HONEST Black-Scholes P&L engine. The legacy `directional_spread_backtest.py` quadratic/linear proxy **inflated win rate and hid breaches** — do not validate against it.
- Telegram alerts + a public GitHub-Pages monitor (`xynkro.github.io/ZeroDTE/v2/`) fed by `publish_monitor.py`; cloud `watchdog.yml` pings if the Mac heartbeat goes stale.

## Anti-patterns — things that have gone wrong here
- Don't trust the legacy proxy backtest's "DEPLOY 72/100, 94% WR"; it booked wins on 0.008% ticks — use BS repricing, because honest numbers flipped the old 40Δ/TP10/ladder config NEGATIVE.
- Don't open two Pine scripts in the TradingView editor at once — `pine_save` writes the wrong source to the saved tab.

## When unsure
- Read `HANDOFF.md` first (current state + security block), then `docs/debriefs/` and `STATUS.md`.
- Default to the conservative choice: stay paper, smaller size, don't tune. Ask only if a change could touch real money or another project's positions.
