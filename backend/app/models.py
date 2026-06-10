"""Pydantic models for WebSocket messages + state objects."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel


class LiveQuote(BaseModel):
    symbol: str
    last: float
    bid: float | None = None
    ask: float | None = None
    timestamp: str  # ISO


class IndicatorState(BaseModel):
    rsi: float | None = None
    stoch_k: float | None = None
    stoch_d: float | None = None
    wvf: float | None = None
    wvf_spike: bool = False
    atr_5m: float | None = None
    atr_d1: float | None = None
    # Pullback trend filter (added after 12mo backtest validation)
    trend: str = "flat"        # "up" | "down" | "flat"
    ema_fast: float | None = None
    ema_slow: float | None = None
    trend_filter_enabled: bool = True


class RegimeState(BaseModel):
    classified: bool = False  # True after 09:45 ET observation period ends
    regime: Literal["volatile", "non_volatile", "pre_obs"] = "pre_obs"
    obs_high: float | None = None
    obs_low: float | None = None
    obs_open: float | None = None   # open of first obs bar
    obs_close: float | None = None  # close of last obs bar
    obs_range: float | None = None
    obs_drift_pct: float | None = None  # (obs_close - obs_open) / obs_open × 100
    proj_high: float | None = None  # call CS sell zone
    proj_low: float | None = None   # put CS sell zone


class StrikeSuggestion(BaseModel):
    instrument: Literal["XSP", "SPX", "SPY"]
    side: Literal["sell_call_cs", "sell_put_cs"]
    mode: Literal["wave", "iron_condor", "directional_spread"]
    short_strike: float
    long_strike: float
    wing_width: float
    multiplier: int = 100
    short_delta: float | None = None
    long_delta: float | None = None
    short_bid: float | None = None
    short_ask: float | None = None
    long_bid: float | None = None
    long_ask: float | None = None
    estimated_credit: float | None = None  # per-share
    estimated_credit_dollars: float | None = None  # × multiplier
    max_loss_dollars: float | None = None
    breakeven: float | None = None
    roi_pct: float | None = None
    pop_estimate_pct: float | None = None
    notional_per_contract: float | None = None
    warnings: list[str] = []


class SignalEvent(BaseModel):
    side: Literal["sell_call_cs", "sell_put_cs"]
    triggered_at: str
    underlying_price: float
    confluence: dict[str, bool]  # {zerodte_rp: True, ...} extra confluence factors
    confluence_score: int  # 1-5+
    # Wave-mode suggestions (25Δ short strikes — for active intraday trading)
    wave_strikes: list[StrikeSuggestion] = []
    # Iron-condor suggestions (8Δ short strikes — for end-of-day overnight deploy)
    ic_strikes: list[StrikeSuggestion] = []
    # Backwards compat
    suggested_strikes: list[StrikeSuggestion] = []
    # TradingView enrichment metadata (not scored — logged for post-hoc analysis)
    tv_enrichment: dict | None = None


class PaperTrade(BaseModel):
    """A simulated trade for assessment-mode tracking."""
    id: str
    trade_no: int = 0   # sequential per session, e.g. #1, #2, ... — used in alerts
    fired_at: str
    side: Literal["sell_call_cs", "sell_put_cs"]
    instrument: Literal["XSP", "SPX", "SPY"]
    short_strike: float
    long_strike: float
    underlying_at_signal: float
    proj_high_at_signal: float | None
    proj_low_at_signal: float | None
    estimated_credit: float
    contracts: int = 1                       # sizing recommendation
    # Exit-trigger thresholds computed at entry
    tp_underlying_target: float | None = None  # take-profit when price reaches this
    stop_underlying_target: float | None = None  # short strike (breach trigger)
    # Realized state
    closed: bool = False
    closed_at: str | None = None
    underlying_at_close: float | None = None
    outcome: Literal[
        "pending", "max_profit_otm", "managed_profit",
        "stopped_breach", "time_close", "eod_expire",
        "skipped_low_conf",
        # Directional spread strategy (May 2026 pivot)
        "tp_target_hit", "stop_ladder_hit", "breach_max_loss",
    ] | None = None
    exit_reason: str | None = None     # short human-readable reason
    pnl: float | None = None
    # Strategy tag — "wave" (legacy) or "directional_spread" (post-pivot)
    strategy: str = "wave"
    # Dynamic stop-loss ladder state (directional_spread strategy only)
    peak_pct_kept: float = 0.0          # highest pct of credit captured intra-trade
    current_stop_pct_kept: float = -100.0  # current stop level (ratchets up)
    breakeven_dist_pct: float | None = None  # %OTM at entry (for P&L computation)
    # Broker order tracking (Alpaca paper trading)
    alpaca_order_id: str | None = None
    broker_status: str | None = None  # "submitted" / "filled" / "shadow" / "error"
    # Dollar scale of the EXECUTED venue vs the SPX-notional ledger math: 0.1 when
    # the trade executes as SPY (1/10 SPX). Sizing + P&L use it so `contracts` is
    # the REAL executed contract count and `pnl` is REAL dollars. Default 1.0 keeps
    # pre-existing trades' semantics. (Fix for the 6-15x undersizing bug: we sized
    # against SPX-scale max loss ~$700/ct while executing SPY where risk ~$70/ct.)
    exec_scale: float = 1.0
    # Wall-clock entry time (distinct from fired_at = the BAR timestamp). Lets us
    # detect backfill/restart phantom trades whose bar time is stale. (quant-audit)
    opened_wall_clock: str | None = None
    # Broker-REALIZED economics from actual Alpaca fills — populated separately from
    # the Black-Scholes model P&L so the validation can be judged on real fills, not
    # the model grading itself. None until the fill-read hook is wired live. The
    # existing `pnl` field remains the MODEL pnl. (quant-audit: critical flaw #2)
    broker_realized_credit: float | None = None  # net entry credit from fills
    broker_realized_pnl: float | None = None      # realized $ from entry+exit fills
    # Black-Scholes pricing state (directional_spread, DIRECTIONAL_PNL_MODEL=bs).
    # Per-5m realized vol fixed at entry; check_exit reprices the spread each bar
    # with shrinking time-to-expiry instead of the underlying-move proxy.
    bs_realized_std: float | None = None
    # Dealer-gamma regime at entry (collected for post-hoc regime→outcome analysis;
    # does NOT change sizing unless GEX_SIZING_ENABLED). See gex.py.
    gex_regime: str | None = None        # "positive" | "negative" | "neutral"
    gex_net_ratio: float | None = None   # net/gross gamma balance, [-1, +1]


class IronCondorBuilder(BaseModel):
    """End-of-day iron condor suggestion (deploy at 12:30-13:00 ET for
    overnight expiration; both legs at ~8 delta target)."""
    build_id: str = ""           # unique per build (e.g. "ic_2026-05-08_1230")
    built_at: str = ""           # ISO timestamp when this build was created
    trigger: str = "auto"        # "auto" (12:30 ET) or "icnow" (manual)
    available: bool = False
    expiry: str | None = None
    underlying_price: float | None = None
    proj_high: float | None = None
    proj_low: float | None = None
    call_leg: StrikeSuggestion | None = None
    put_leg: StrikeSuggestion | None = None
    total_credit_dollars: float | None = None
    total_max_loss_dollars: float | None = None
    bpr_estimate_dollars: float | None = None
    # Skew info (asymmetric OTM based on obs window drift)
    skew_direction: str | None = None   # "bearish" | "bullish" | "neutral"
    obs_drift_pct: float | None = None  # % drift during obs window
    call_pct_otm: float | None = None   # effective %OTM used for call side
    put_pct_otm: float | None = None    # effective %OTM used for put side
    notes: list[str] = []
    # Broker execution (Alpaca paper, IC_EXECUTION_ENABLED). The IC was alert-only
    # until 2026-06-10 — these track the actual order so "set and forget" is real.
    alpaca_order_id: str | None = None
    broker_status: str | None = None    # "submitted" / "shadow" / "error" / "closed_stop"
    contracts: int = 1


class DashboardState(BaseModel):
    """Top-level state pushed to the PWA. Updated every poll cycle."""
    ts: str
    quote: LiveQuote | None = None
    indicators: IndicatorState = IndicatorState()
    regime: RegimeState = RegimeState()
    last_signals: list[SignalEvent] = []
    open_positions: list[PaperTrade] = []
    macro_alerts: list[dict] = []
    backend_status: Literal["ok", "ibkr_disconnected", "warming_up", "error"] = "warming_up"
    feed_type: str = "none"       # alpaca / ibkr / yfinance / none
    alpaca_ready: bool = False    # True when Alpaca trader is initialized
    notes: list[str] = []
    # End-of-day IC builder — populated when regime classified + chain reachable.
    # `iron_condor` always reflects the LATEST build (for backwards compat with frontend).
    # `iron_condor_history` is the full list of every build that fired today (auto +
    # /icnow), so EOD can score each one separately.
    iron_condor: IronCondorBuilder = IronCondorBuilder()
    iron_condor_history: list[IronCondorBuilder] = []
    # Current dealer-gamma (GEX) snapshot — regime/walls for the dashboard. See gex.py.
    gex: dict | None = None
