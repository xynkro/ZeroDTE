"""Environment-based settings loader.
Loads .env from project root (~/Documents/Trading/ZeroDTE/.env).
NEVER commit .env to git; .env.example is the template.
"""
from __future__ import annotations

import os

# Fix numpy OpenBLAS hang on macOS (must be set before numpy imports)
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


def _f(key: str, default: float) -> float:
    v = os.getenv(key)
    try:
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _i(key: str, default: int) -> int:
    v = os.getenv(key)
    try:
        return int(v) if v else default
    except (TypeError, ValueError):
        return default


def _b(key: str, default: bool) -> bool:
    v = os.getenv(key, "")
    return v.lower() in ("true", "1", "yes") if v else default


class Settings:
    # Finnhub
    FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")

    # Alpaca
    ALPACA_API_KEY: str = os.getenv("ALPACA_API_KEY", "")
    ALPACA_SECRET_KEY: str = os.getenv("ALPACA_SECRET_KEY", "")
    ALPACA_BASE_URL: str = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    ALPACA_DATA_URL: str = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
    ALPACA_FEED: str = os.getenv("ALPACA_FEED", "iex")  # iex=free, sip=paid

    # IBKR
    IBKR_HOST: str = os.getenv("IBKR_HOST", "127.0.0.1")
    IBKR_PORT: int = _i("IBKR_PORT", 7497)
    IBKR_CLIENT_ID: int = _i("IBKR_CLIENT_ID", 42)

    # Risk + safety
    DAILY_LOSS_LIMIT_PCT: float = _f("DAILY_LOSS_LIMIT_PCT", 2.0)
    MAX_TRADES_PER_DAY: int = _i("MAX_TRADES_PER_DAY", 4)
    MAX_CONCURRENT_POSITIONS: int = _i("MAX_CONCURRENT_POSITIONS", 3)
    TRADING_ENABLED: bool = _b("TRADING_ENABLED", False)
    SHADOW_MODE: bool = _b("SHADOW_MODE", True)
    PAPER_BROKER: str = os.getenv("PAPER_BROKER", "none")  # "none" | "alpaca"
    SIZE_CAP_USD: float = _f("SIZE_CAP_USD", 500.0)
    # Runtime kill switch — set True by POST /api/alpaca/kill to HALT all new entries
    # (paper + broker). Cleared by POST /api/trading/resume or a restart. In-memory only.
    TRADING_HALTED: bool = False

    # ── Dealer-gamma (GEX) regime — collected for sizing context (see gex.py) ──
    # Phase 1: fetch + display + STAMP regime on each trade for post-hoc analysis.
    # Sizing stays OFF until negative-GEX days are shown to actually hurt our trades.
    GEX_ENABLED: bool = _b("GEX_ENABLED", True)
    GEX_SIZING_ENABLED: bool = _b("GEX_SIZING_ENABLED", False)   # let negative GEX cut size
    GEX_SYMBOL: str = os.getenv("GEX_SYMBOL", "_SPX")            # CBOE symbol (_SPX | SPY)
    GEX_REFRESH_MIN: int = _i("GEX_REFRESH_MIN", 30)            # refresh cadence (min)
    GEX_NEG_SIZE_FACTOR: float = _f("GEX_NEG_SIZE_FACTOR", 0.5)  # contracts × this on negative GEX

    # Position sizing (per-trade)
    ACCOUNT_SIZE_USD: float = _f("ACCOUNT_SIZE_USD", 7500.0)   # paper acct ~ live
    RISK_PER_TRADE_PCT: float = _f("RISK_PER_TRADE_PCT", 1.0)  # %, e.g. 1.0 = 1% per trade

    # Wave management thresholds (Phase 3 — canonical 0DTE)
    WAVE_FAVORABLE_MOVE_PCT: float = _f("WAVE_FAVORABLE_MOVE_PCT", 0.3)   # take-profit trigger
    WAVE_TIME_STOP_MIN_BEFORE_CLOSE: int = _i("WAVE_TIME_STOP_MIN", 30)   # was 15; canonical 30
    # Phase 3b: STOP only fires when bar CLOSES through short strike (not intra-bar wick).
    # Aligns with canonical "manage options actively, no stop orders" guidance.
    WAVE_STOP_REQUIRES_BAR_CLOSE: bool = _b("WAVE_STOP_REQUIRES_BAR_CLOSE", True)

    # Phase 4: VWAP gate. SELL CALL requires price > VWAP; SELL PUT requires price < VWAP.
    # Canonical mean-reversion rule — without this we'd sell calls into volume-weighted
    # uptrends and puts into downtrends. Hard gate.
    WAVE_VWAP_GATE_ENABLED: bool = _b("WAVE_VWAP_GATE_ENABLED", True)
    # Phase 4: prime-window confluence factor (10:30-13:00 ET = mid-morning sweet spot).
    # Soft factor — adds 1 to confluence score if signal fires in window.
    WAVE_PRIME_WINDOW_START: str = os.getenv("WAVE_PRIME_WINDOW_START", "10:30")
    WAVE_PRIME_WINDOW_END:   str = os.getenv("WAVE_PRIME_WINDOW_END",   "13:00")

    # Wave gospel — Phase 1 gating
    # Confluence: 5 variable-only factors. Signals < min score never fire.
    WAVE_MIN_CONFLUENCE_SCORE: int = _i("WAVE_MIN_CONFLUENCE_SCORE", 3)   # 3/5 minimum to fire
    WAVE_FULL_SIZE_CONFLUENCE: int = _i("WAVE_FULL_SIZE_CONFLUENCE", 4)   # ≥this gets full sizing
    WAVE_BLACKOUT_BLOCKS_ENTRIES: bool = _b("WAVE_BLACKOUT_BLOCKS_ENTRIES", True)  # was alert-only

    # Wave gospel — Phase 2 (exit refinements)
    # Vol-scaled TP: TP target = WAVE_TP_ATR_MULT × ATR(D1). 0 = use legacy fixed % move.
    # DEFAULT 0 (disabled) — backtest showed regression on this dataset; revisit
    # with refined P&L heuristics that scale TP-credit-kept with TP distance.
    WAVE_TP_ATR_MULT: float = _f("WAVE_TP_ATR_MULT", 0.0)
    # Same-bar exit guard: TP/TIME can't fire on entry bar (STOP always can).
    WAVE_SAMEBAR_EXIT_GUARD: bool = _b("WAVE_SAMEBAR_EXIT_GUARD", True)
    # Mid-session vol re-gate: block new entries if rolling 30m range > mult × expected.
    WAVE_MIDSESSION_REGATE: bool = _b("WAVE_MIDSESSION_REGATE", True)
    WAVE_MIDSESSION_VOL_MULT: float = _f("WAVE_MIDSESSION_VOL_MULT", 2.0)

    # Day-of-week filter (lesson from options.cafe — Mon/Wed/Fri tend to range,
    # Tue trends, Thu carries data risk). Default OFF to keep all weekdays;
    # set MWF_ONLY=true in .env to restrict.
    MWF_ONLY: bool = _b("MWF_ONLY", False)

    # FOMC-specific tighter blackout (lesson from CBOE Henry Schwartz article):
    # general blackout = ±15min, FOMC announcements get ±30min for safety.
    BLACKOUT_FOMC_MIN: int = _i("BLACKOUT_FOMC_MIN", 30)

    # Iron Condor strike-picker — geometric default (matches what backtest
    # validated at 96.1% WR), with optional delta-targeted upgrade when
    # chain Greeks are healthy.
    # 0.9% OTM ≈ 12Δ at 0DTE — wider than original 0.5% (which was too tight,
    # producing low win rates in backtesting). 0.9% gives ~$5 cushion on SPY
    # at $575. Still competitive premium with theta runway, but survives
    # typical intraday swings. Range floor ensures strikes stay outside
    # the obs-window established range.
    IC_DEFAULT_PCT_OTM: float = _f("IC_DEFAULT_PCT_OTM", 0.9)
    IC_DELTA_TARGET: float = _f("IC_DELTA_TARGET", 0.20)        # 0.20 = 20Δ short
    IC_INSTRUMENT: str = os.getenv("IC_INSTRUMENT", "XSP")
    IC_WING_WIDTH: float = _f("IC_WING_WIDTH", 0)               # 0 = instrument default

    # EOD IC — CBOE real-delta strike placement (fixes the "geometric pennies" bug:
    # the old picker placed shorts at 1-6Δ → ~$35 credit / ~$965 risk = 27:1 garbage).
    # Now places shorts at a real delta off the CBOE chain with wider SPX wings and
    # SKIPS entirely when the credit is too thin to be worth the wing risk.
    EOD_IC_USE_CBOE: bool = _b("EOD_IC_USE_CBOE", True)
    EOD_IC_SHORT_DELTA: float = _f("EOD_IC_SHORT_DELTA", 0.16)    # short-leg delta target
    EOD_IC_WING_DOLLARS: float = _f("EOD_IC_WING_DOLLARS", 25.0)  # SPX wing width ($)
    EOD_IC_MIN_CREDIT_PCT: float = _f("EOD_IC_MIN_CREDIT_PCT", 10.0)  # min credit as % of wing → else skip
    # IC trade management (shown in the alert): take profit at TP_PCT% of credit
    # captured; stop loss at SL_MULT× the credit (or a short-strike touch).
    EOD_IC_TP_PCT: float = _f("EOD_IC_TP_PCT", 50.0)             # close at 50% of credit captured
    EOD_IC_SL_MULT: float = _f("EOD_IC_SL_MULT", 2.0)           # stop at 2× credit loss

    # IC auto-build time (ET, "HH:MM"). Default 09:45 = right after observation
    # window completes. Henry Schwartz's CBOE article suggests 12:30 ET for
    # tighter risk/reward, but user preference is "fire and forget early".
    # Moved from 09:45 to 10:15 — lets the morning volatility settle and
    # gives a 30-min obs window (9:45-10:15) for range/drift estimation.
    # Henry Schwartz (CBOE) notes first 30 min is highest breach risk.
    EOD_IC_BUILD_ET: str = os.getenv("EOD_IC_BUILD_ET", "10:15")

    # IV gate — refuse IC builds when VIX is too elevated.
    # Volatility Box research: VIX > 30 = "high-fear sessions" with elevated breach risk.
    # 25 is a more conservative default; raise to 30 for less restrictive gating.
    IC_MAX_VIX: float = _f("IC_MAX_VIX", 25.0)

    # Wave strike-picker — Phase 3 canonical: 1.5% OTM / 10-15Δ short.
    # Backtest evidence: 0.5% OTM was negative EV; 1.5% OTM was best at +$3.4k/60d.
    WAVE_DEFAULT_PCT_OTM: float = _f("WAVE_DEFAULT_PCT_OTM", 1.5)
    WAVE_TARGET_DELTA: float = _f("WAVE_TARGET_DELTA", 0.12)             # 10-15Δ canonical

    # ═══════════════════════════════════════════════════════════════════════
    # Directional Spread Strategy (May 2026 pivot — unified IC + Wave replacement)
    # ═══════════════════════════════════════════════════════════════════════
    # Backtest validation: 4.4y SPX data, 153 trades, 81% WR, +$6,603 total,
    # profitable in every year 2022-2026. Verdict: DEPLOY (72/100).
    #
    # KEY INSIGHT: Take VERY tiny profits (10% of credit captured) immediately.
    # Tastytrade scalp philosophy — feast and famine, can't afford to be wrong.
    #
    # When DIRECTIONAL_SPREAD_ENABLED=true, this strategy runs ALONGSIDE the
    # legacy wave_manager. Shadow-mode validation period before deletion.
    DIRECTIONAL_SPREAD_ENABLED: bool = _b("DIRECTIONAL_SPREAD_ENABLED", False)

    # ── Strike/exit config (May 2026 HONEST RE-VALIDATION, Black-Scholes engine) ──
    # The original pivot used a power-law underlying-move proxy for spread P&L.
    # That proxy booked a "win" on a 0.008% favorable tick (median 1 bar) and so
    # reported ~0 breaches — it never let trades live long enough to breach. Under
    # Black-Scholes repricing (honest_backtest.py) the deployed 40Δ/TP10/ladder
    # config is NEGATIVE (-$3.5k, 1/5 years positive) despite a 91% win rate:
    # tiny wins (10% of credit) can't cover full-credit losses.
    #
    # The honest search inverted the thesis. The +EV frontier is HIGH-TP /
    # NO-LADDER (pure theta harvest): sell the vertical, hold it, close only when
    # it's nearly worthless or at the time stop. Risk-adjusted winner:
    #   30Δ / TP90 / no-ladder / BS pricing → +$5,479, DD -$1,581, positive 5/5 yrs.
    #
    # 30Δ short (≈0.5% OTM at normal vol). Lower delta than the old 40Δ = wider
    # strikes, fewer breaches, smoother equity than the 40Δ variant.
    DIRECTIONAL_SHORT_DELTA: int = _i("DIRECTIONAL_SHORT_DELTA", 30)

    # $10 SPX wing — Tastytrade canonical, $1 SPY equivalent.
    DIRECTIONAL_WING_DOLLARS: float = _f("DIRECTIONAL_WING_DOLLARS", 10.0)

    # Final TP target — 90% of credit captured. Let the spread decay (harvest
    # theta) instead of scalping 10%. Honest backtest: low TP is negative-EV,
    # high TP is the profitable half of the parameter space.
    DIRECTIONAL_TP_TARGET: float = _f("DIRECTIONAL_TP_TARGET", 90.0)

    # ── Staged quant-audit controls (2026-06) · default = CURRENT behaviour ──
    # These are wired but OFF, so flipping the env var is the entire change. See
    # docs/ZeroDTE_Quant_Audit.pptx + docs/FLAWS.md for the rationale.
    #
    # Suppress the CALL book. The validated +$5,479 edge is ~96% PUT-selling
    # (147 puts +$5,357 vs 6 calls +$121). The live call book (sell-calls-on-rallies)
    # is counter-trend in an up-drift and statistically unsupported. ON = stand
    # aside on every sell-call signal; keep selling puts.
    DIRECTIONAL_SUPPRESS_CALLS: bool = _b("DIRECTIONAL_SUPPRESS_CALLS", False)
    # Evaluate exits on CLOSED bars only (mirrors honest_backtest's worst-first
    # ordering). The live feed re-dispatches the developing 5m bar every ~60s, so
    # evaluating exits on it can book intra-bar TPs the backtest would never see
    # (phantom wins). ON = defer exit evaluation to the first dispatch of the NEXT
    # bar. NOTE: validate session-end (time-stop / EOD) behaviour before enabling.
    EXIT_ON_CLOSED_BAR_ONLY: bool = _b("EXIT_ON_CLOSED_BAR_ONLY", False)

    # ── Pricing-honesty + execution upgrades (2026-06, default OFF) ──
    # Skew: price puts richer / calls cheaper (real index 0DTE skew) vs a flat vol.
    # Re-validation: baseline +$5,479/t1.85 → +skew +$9,926/t3.65 (edge ~99.7%
    # puts). CONTINGENT on capturing the skew at fills — trust only once
    # broker-realized P&L confirms it.
    DIRECTIONAL_SKEW_ENABLED: bool = _b("DIRECTIONAL_SKEW_ENABLED", False)
    DIRECTIONAL_SKEW_PUT_MULT: float = _f("DIRECTIONAL_SKEW_PUT_MULT", 1.15)
    DIRECTIONAL_SKEW_CALL_MULT: float = _f("DIRECTIONAL_SKEW_CALL_MULT", 0.90)
    # Vol-floor ratchet: lift exit-repricing vol if intraday RV exceeds entry RV
    # (conservative-only). Re-validation: −10% P&L (honest tails), lower drawdown.
    DIRECTIONAL_VOL_RATCHET: bool = _b("DIRECTIONAL_VOL_RATCHET", False)
    # GEX gating: stand aside on strongly-negative dealer-gamma sessions. Validate
    # the breach-rate edge before enabling. Inert until proven.
    GEX_GATING_ENABLED: bool = _b("GEX_GATING_ENABLED", False)
    GEX_GATE_REGIME: str = os.getenv("GEX_GATE_REGIME", "negative")
    # Marketable-limit execution: limit (1 tick give) instead of market mleg orders.
    # NEEDS a real option-quote feed to set the price; without one the limit comes
    # from the same BS model (circular). Scaffold only until a quote feed exists.
    ALPACA_MARKETABLE_LIMIT: bool = _b("ALPACA_MARKETABLE_LIMIT", False)
    # Read real Alpaca fills into broker_realized_pnl (instrumentation, default ON).
    READ_BROKER_FILLS: bool = _b("READ_BROKER_FILLS", True)
    # EXECUTE the EOD iron condor on Alpaca paper (northstar strategy #1). Until
    # 2026-06-10 the IC was ALERT-ONLY: it built strikes, pinged Telegram, and never
    # placed the trade. ON = submit the 4-leg SPY mleg (SPX/10, $1 grid) when the
    # CBOE build passes the min-credit gate; the breakeven stop-rule then CLOSES it
    # via two reverse spreads. Held to expiry otherwise (0DTE — expires same day).
    IC_EXECUTION_ENABLED: bool = _b("IC_EXECUTION_ENABLED", False)
    IC_CONTRACTS: int = _i("IC_CONTRACTS", 1)

    # Directional VIX stand-aside threshold (decoupled from the IC builder's 22 line).
    # Validated on the put book (153-trade backtest, prior-day VIX): VIX 22-30 is the
    # BEST regime (+$128/trade, 83% WR, 11% breach); only VIX>=30 is net-negative
    # (50% breach, tail risk). Default 22.0 = CURRENT behaviour. Set 30.0 to capture
    # the +$2,304 the 22-line was leaving on the table. (Note: the live gate compares
    # VIX1D, which is more reactive than the 30d VIX used to calibrate this.)
    WAVE_VIX_STANDASIDE: float = _f("WAVE_VIX_STANDASIDE", 22.0)

    # Dynamic stop-loss ladder — DISABLED. The honest backtest showed the ladder
    # ratchets you out of winners that recover; no-ladder beat ladder at every
    # delta. Only the initial -100% loss stop + time stop remain.
    DIRECTIONAL_USE_DYNAMIC_STOPS: bool = _b("DIRECTIONAL_USE_DYNAMIC_STOPS", False)

    # Dynamic stop-loss ladder triggers (peak pct_kept thresholds) — only used
    # when DIRECTIONAL_USE_DYNAMIC_STOPS=true (legacy/experimental).
    DIRECTIONAL_LADDER_50: float = _f("DIRECTIONAL_LADDER_50", 50.0)
    DIRECTIONAL_LADDER_75: float = _f("DIRECTIONAL_LADDER_75", 75.0)
    DIRECTIONAL_LADDER_90: float = _f("DIRECTIONAL_LADDER_90", 90.0)
    DIRECTIONAL_LOCK_50: float = _f("DIRECTIONAL_LOCK_50", 25.0)
    DIRECTIONAL_LOCK_75: float = _f("DIRECTIONAL_LOCK_75", 50.0)
    DIRECTIONAL_LOCK_90: float = _f("DIRECTIONAL_LOCK_90", 75.0)

    # P&L model: "bs" = Black-Scholes repricing (HONEST, validated default),
    # "quadratic"/"linear" = legacy underlying-move proxy (kept for rollback only —
    # known to inflate win rate and hide breaches; do NOT trust for validation).
    DIRECTIONAL_PNL_MODEL: str = os.getenv("DIRECTIONAL_PNL_MODEL", "bs")

    # IV/realized-vol premium for BS pricing. Option IV typically exceeds realized;
    # 1.2 = 20% premium. Drives entry credit + strike width. +EV across 1.0-1.5.
    DIRECTIONAL_PREMIUM_MULT: float = _f("DIRECTIONAL_PREMIUM_MULT", 1.20)

    # Round-trip transaction cost per spread ($), subtracted from each trade's P&L
    # in shadow accounting. SPX vertical bid/ask + commissions ≈ $25. +EV to ~$60.
    DIRECTIONAL_COST_PER_SPREAD: float = _f("DIRECTIONAL_COST_PER_SPREAD", 25.0)

    # SPY equivalents for Alpaca paper trading (Alpaca lacks index options)
    SPY_WING_DOLLARS: float = _f("SPY_WING_DOLLARS", 1.0)

    # TradingView enrichment (external TA for richer confluence scoring)
    TV_ENRICHMENT_ENABLED: bool = os.getenv("TV_ENRICHMENT_ENABLED", "true").lower() in ("1", "true", "yes")
    TV_ENRICHMENT_INTERVAL: str = os.getenv("TV_ENRICHMENT_INTERVAL", "5m")

    # Server
    BACKEND_HOST: str = os.getenv("BACKEND_HOST", "0.0.0.0")
    BACKEND_PORT: int = _i("BACKEND_PORT", 8765)
    FRONTEND_ORIGIN: str = os.getenv("FRONTEND_ORIGIN", "http://localhost:5179")

    # Telegram (cross-device alerts; shared bot with FinancePWA)
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "922547929")  # DM fallback / smoke tests
    # Group routing — Finance & Trading supergroup with topics
    TELEGRAM_GROUP_CHAT_ID: str = os.getenv("TELEGRAM_GROUP_CHAT_ID", "")
    TELEGRAM_TOPIC_ZERO_DTE: int = _i("TELEGRAM_TOPIC_ZERO_DTE", 0)
    TELEGRAM_TOPIC_MACRO: int = _i("TELEGRAM_TOPIC_MACRO", 0)
    TELEGRAM_TOPIC_IRON_CONDOR: int = _i("TELEGRAM_TOPIC_IRON_CONDOR", 0)
    DASHBOARD_PUBLIC_URL: str = os.getenv("DASHBOARD_PUBLIC_URL", "")

    # Shared-secret guarding the write/control endpoints (kill, resume, prefs,
    # flow scan). Empty = auth disabled (back-compat). When set, the backend
    # injects it into the served dashboards so the operator's own page works,
    # and any other caller must present it as the X-ZeroDTE-Token header.
    API_WRITE_TOKEN: str = os.getenv("API_WRITE_TOKEN", "")

    # Paths
    project_root: Path = PROJECT_ROOT
    data_dir: Path = PROJECT_ROOT / "backend" / "data"


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
(settings.data_dir / "paper_trades").mkdir(parents=True, exist_ok=True)
