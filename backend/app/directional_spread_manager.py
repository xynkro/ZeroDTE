"""Directional Spread Manager — post-pivot strategy (May 2026).

Implements the unified strategy from the May 2026 backtest validation:
  - Single-sided credit spreads (call OR put, never both at once per signal)
  - 40Δ short / $10 wing (Tastytrade-style aggressive credit collection)
  - Dynamic stop-loss ladder ratchets stops as profit accrues
  - Final TP at 10% of credit captured (Tastytrade scalp philosophy)
  - Catastrophe stop on bar-close-through-short-strike
  - TIME stop 30min before close

Replaces both the symmetric IC builder and the static-TP wave manager.
Runs ALONGSIDE the legacy wave_manager during shadow-mode validation
(controlled by DIRECTIONAL_SPREAD_ENABLED in .env).

KEY DIFFERENCE from wave_manager:
  - wave_manager uses fixed TP (0.3% favorable move = 75% credit kept)
  - directional_spread uses pct_kept tracking + ratcheting stops

The pct_kept approximation maps underlying price moves → spread P&L:
  pct_kept = (favorable_move_pct / breakeven_dist_pct) ^ 0.7 * 100  (quadratic)
  where breakeven_dist_pct = strike_distance_pct at entry.

Backtest validation: 4.4y SPX data, 153 trades, 81% WR, $6,603 total,
profitable in every year 2022-2026, Verdict: DEPLOY (72/100).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

from . import bs_pricing as bs
from .config import settings
from .models import PaperTrade, SignalEvent, StrikeSuggestion
from .predictor import Bar


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# ────────────────────────────────────────────────────────────────────
# Black-Scholes pricing helpers (DIRECTIONAL_PNL_MODEL=bs — honest engine)
# ────────────────────────────────────────────────────────────────────

def _periods_remaining(bar_time) -> float:
    """Number of 5-minute periods from bar_time to 16:00 ET expiry."""
    et = bar_time.astimezone(ET) if bar_time.tzinfo else bar_time
    return max((16 * 60) - (et.hour * 60 + et.minute), 0) / 5.0


def bs_entry_strikes(
    side: str,
    spot: float,
    realized_std: float,
    entry_dt: datetime,
    short_delta: int,
    wing_dollars: float,
    premium_mult: float | None = None,
) -> dict | None:
    """Place strikes at TRUE Black-Scholes delta and derive the entry credit.

    Returns {short_strike, long_strike, credit_dollars, tv0} or None if the
    session has too little vol data to price (caller falls back / skips).
    """
    if realized_std is None or realized_std <= 0:
        return None
    pr = _periods_remaining(entry_dt)
    if pr <= 0:
        return None
    pm = premium_mult if premium_mult is not None else settings.DIRECTIONAL_PREMIUM_MULT
    tv0 = bs.total_vol_to_expiry(realized_std, pr, pm)
    if tv0 <= 0:
        return None
    if side == "sell_call_cs":
        short_k = bs.strike_for_call_delta(spot, tv0, short_delta / 100.0)
        long_k = short_k + wing_dollars
    else:
        short_k = bs.strike_for_put_delta(spot, tv0, short_delta / 100.0)
        long_k = short_k - wing_dollars
    credit_ps = bs.spread_value(side, spot, short_k, long_k, tv0)
    if credit_ps <= 0.02:
        return None
    return {
        "short_strike": round(short_k, 2),
        "long_strike": round(long_k, 2),
        "credit_dollars": round(credit_ps * 100, 2),
        "tv0": tv0,
    }


# ────────────────────────────────────────────────────────────────────
# Delta → %OTM and credit mappings (calibrated to low-VIX 0DTE pricing)
# These match the backtest module for consistency.
# ────────────────────────────────────────────────────────────────────

DELTA_TO_OTM_PCT = {
    45: 0.16,
    40: 0.22,   # POST-PIVOT DEFAULT
    35: 0.30,   # Tastytrade canonical
    30: 0.35,
    25: 0.40,
    20: 0.50,
    15: 0.65,
    10: 0.85,
    5:  1.20,
}

DELTA_TO_CREDIT_PCT = {
    45: 0.45,
    40: 0.40,   # POST-PIVOT DEFAULT — $400 credit on $10 wing
    35: 0.35,
    30: 0.30,
    25: 0.27,
    20: 0.25,
    15: 0.18,
    10: 0.13,
    5:  0.07,
}


def spy_strike_params(
    side: str,
    spy_price: float,
    short_delta: int,
) -> dict:
    """Compute SPY-scaled strikes for Alpaca paper trading.

    SPY options have $0.50 strike increments. Wing = SPY_WING_DOLLARS ($1 default).
    Same delta/OTM% math as SPX — percentages are scale-independent.
    """
    otm_pct = DELTA_TO_OTM_PCT.get(short_delta, 0.22)
    wing = settings.SPY_WING_DOLLARS
    if side == "sell_call_cs":
        short_strike = round(spy_price * (1 + otm_pct / 100.0) * 2) / 2  # nearest $0.50
        long_strike = short_strike + wing
    else:
        short_strike = round(spy_price * (1 - otm_pct / 100.0) * 2) / 2
        long_strike = short_strike - wing
    credit = credit_dollars_for_delta(short_delta, wing, multiplier=100)
    return {
        "short_strike": short_strike,
        "long_strike": long_strike,
        "wing": wing,
        "credit": credit,
        "side_type": "call" if side == "sell_call_cs" else "put",
    }


def otm_pct_for_delta(short_delta: int) -> float:
    """%OTM for given short delta (low-VIX approximation)."""
    return DELTA_TO_OTM_PCT.get(short_delta, 0.22)


def credit_dollars_for_delta(short_delta: int, wing_dollars: float, multiplier: int = 100) -> float:
    """Estimated credit per contract: delta-based pct × wing × multiplier."""
    pct = DELTA_TO_CREDIT_PCT.get(short_delta, 0.40)
    return pct * wing_dollars * multiplier


# ────────────────────────────────────────────────────────────────────
# Sizing — single-contract default with risk-budget cap
# ────────────────────────────────────────────────────────────────────

def recommend_contracts(
    max_loss_per_contract_usd: float,
    confluence_score: int,
    confluence_max: int = 4,
) -> tuple[int, str]:
    """Return (contracts, rationale).

    Sizing rule (post-pivot):
      base_risk = account_size × risk_per_trade_pct/100
      contracts = floor(base_risk / max_loss_per_contract), capped at MAX_CONCURRENT_POSITIONS

    With 40Δ + $10 wing: max_loss ≈ $600 per contract.
    With $400 risk budget (4% of $10k): 0 contracts mathematically → forced to 1 (over budget).
    Trader can bump RISK_PER_TRADE_PCT to size up.
    """
    if max_loss_per_contract_usd <= 0:
        return 0, "no loss data"

    base_risk = settings.ACCOUNT_SIZE_USD * (settings.RISK_PER_TRADE_PCT / 100.0)
    n = int(base_risk // max_loss_per_contract_usd)
    n = max(0, min(n, settings.MAX_CONCURRENT_POSITIONS))

    # Floor: if any signal passes gate, allow at least 1 contract (small accts)
    if n == 0 and max_loss_per_contract_usd > 0:
        n = 1
        over_pct = (max_loss_per_contract_usd / base_risk - 1) * 100
        return n, (f"min size 1 ct (conf={confluence_score}/{confluence_max}, "
                   f"$1 ct = ${max_loss_per_contract_usd:.0f} "
                   f"≈ {over_pct:+.0f}% over ${base_risk:.0f} budget)")

    rationale = (f"{n} ct (conf={confluence_score}/{confluence_max}) — "
                 f"risking ~${n * max_loss_per_contract_usd:.0f} of ${base_risk:.0f} budget")
    return n, rationale


# ────────────────────────────────────────────────────────────────────
# Spread P&L approximation — must match backtest module
# ────────────────────────────────────────────────────────────────────

def spread_pct_kept(
    side: str,
    entry_price: float,
    current_price: float,
    breakeven_dist_pct: float,
    model: str = "quadratic",
) -> float:
    """Return % of credit kept (running profit as fraction of max profit).

    Range: [-200, +100]
      +100% = max profit (spread → $0)
       0%   = break-even (spread = credit collected)
      -100% = lost full credit
      -200% = max loss territory (capped)

    'quadratic' model: gamma curvature for 0DTE (more realistic than linear).
    """
    if side == "sell_call_cs":
        favorable_pct = (entry_price - current_price) / entry_price * 100.0
    else:
        favorable_pct = (current_price - entry_price) / entry_price * 100.0

    if breakeven_dist_pct <= 0:
        return 0.0

    if model == "linear":
        pct_kept = (favorable_pct / breakeven_dist_pct) * 100.0
    else:  # quadratic
        if favorable_pct >= 0:
            pct_kept = (favorable_pct / breakeven_dist_pct) ** 0.7 * 100.0
        else:
            pct_kept = -((abs(favorable_pct) / breakeven_dist_pct) ** 1.3) * 100.0

    return max(-200.0, min(100.0, pct_kept))


# ────────────────────────────────────────────────────────────────────
# Entry — build a PaperTrade with directional spread parameters
# ────────────────────────────────────────────────────────────────────

def open_directional_trade(
    sig_event: SignalEvent,
    sp: StrikeSuggestion,
    trade_no: int,
    short_delta: int | None = None,
    realized_std: float | None = None,
) -> tuple[PaperTrade, str]:
    """Open a directional spread paper trade.

    Computes:
      - Strike placement at SHORT_DELTA (≈ %OTM from entry price)
      - Credit estimate from DELTA_TO_CREDIT_PCT lookup
      - Dynamic stop ladder initialized at -100% credit
    """
    short_delta = short_delta or settings.DIRECTIONAL_SHORT_DELTA
    breakeven_dist_pct = otm_pct_for_delta(short_delta)
    multiplier = sp.multiplier or 100
    wing = sp.wing_width

    # Credit from delta lookup (calibrated; ignores sp.estimated_credit if 0)
    credit = sp.estimated_credit_dollars
    if credit is None or credit <= 0:
        credit = credit_dollars_for_delta(short_delta, wing, multiplier)

    max_loss = wing * multiplier - credit

    # Sizing
    confluence_max = len(sig_event.confluence) if sig_event.confluence else 4
    contracts, sizing_note = recommend_contracts(
        max_loss_per_contract_usd=max_loss,
        confluence_score=sig_event.confluence_score,
        confluence_max=confluence_max,
    )

    return PaperTrade(
        id=str(uuid4()),
        trade_no=trade_no,
        fired_at=sig_event.triggered_at,
        side=sig_event.side,
        instrument=sp.instrument,
        short_strike=sp.short_strike,
        long_strike=sp.long_strike,
        underlying_at_signal=sig_event.underlying_price,
        proj_high_at_signal=None,
        proj_low_at_signal=None,
        estimated_credit=credit,
        contracts=contracts,
        # No tp_underlying_target / stop_underlying_target — managed via ladder
        tp_underlying_target=None,
        stop_underlying_target=sp.short_strike,  # for catastrophe stop only
        strategy="directional_spread",
        peak_pct_kept=0.0,
        current_stop_pct_kept=-100.0,  # initial: lose full credit
        breakeven_dist_pct=breakeven_dist_pct,
        bs_realized_std=realized_std,  # set → check_exit reprices with Black-Scholes
        outcome="pending",
    ), sizing_note


# ────────────────────────────────────────────────────────────────────
# Exit checking — called per bar by orchestrator
# ────────────────────────────────────────────────────────────────────

def check_exit(trade: PaperTrade, bar: Bar) -> Optional[dict]:
    """Evaluate dynamic stop ladder + TP target + hard stops.

    Updates trade in-place (peak_pct_kept, current_stop_pct_kept).
    Returns exit dict if trade closes this bar, else None.
    """
    if trade.closed:
        return None
    if trade.strategy != "directional_spread":
        return None  # only manage directional spreads here
    # Black-Scholes path (honest engine) — repriced spread, not underlying-move proxy
    if trade.bs_realized_std is not None:
        return _check_exit_bs(trade, bar)
    if trade.breakeven_dist_pct is None or trade.breakeven_dist_pct <= 0:
        return None  # safety — bad state

    et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
    bar_min = et.hour * 60 + et.minute
    close_min = 16 * 60
    time_stop_min = close_min - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE

    # EOD — expire OTM if still alive
    if bar_min >= close_min:
        _close(trade, bar, outcome="max_profit_otm",
               reason="expired OTM at 16:00 ET — full credit kept")
        trade.pnl = (trade.estimated_credit or 0) * trade.contracts
        return _exit_dict(trade, bar)

    # Same-bar guard (capital protection — STOP fires same bar; TP/TIME don't)
    is_same_bar = False
    try:
        fired_dt = datetime.fromisoformat(trade.fired_at)
        is_same_bar = bar.time <= fired_dt
    except Exception:
        is_same_bar = False

    # Intra-bar extremes
    if trade.side == "sell_call_cs":
        worst_price, best_price = bar.high, bar.low
    else:
        worst_price, best_price = bar.low, bar.high

    pct_worst = spread_pct_kept(
        trade.side, trade.underlying_at_signal, worst_price,
        trade.breakeven_dist_pct, model=settings.DIRECTIONAL_PNL_MODEL,
    )
    pct_best = spread_pct_kept(
        trade.side, trade.underlying_at_signal, best_price,
        trade.breakeven_dist_pct, model=settings.DIRECTIONAL_PNL_MODEL,
    )

    # Update peak (using best intra-bar price)
    if pct_best > trade.peak_pct_kept:
        trade.peak_pct_kept = pct_best

    # Dynamic stop ladder (lock values now configurable — see DIRECTIONAL_LOCK_*)
    if trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_90:
        trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_90)
    elif trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_75:
        trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_75)
    elif trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_50:
        trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_50)

    # STOP — fires same-bar (capital protection)
    if pct_worst <= trade.current_stop_pct_kept:
        _close(trade, bar, outcome="stop_ladder_hit",
               reason=f"stop hit at {trade.current_stop_pct_kept:+.0f}% credit "
                      f"(peak was {trade.peak_pct_kept:+.0f}%)")
        trade.pnl = (trade.estimated_credit or 0) * (trade.current_stop_pct_kept / 100.0) * trade.contracts
        return _exit_dict(trade, bar)

    # CATASTROPHE: bar CLOSE through short strike (gap-through guard)
    call_breach = trade.side == "sell_call_cs" and bar.close >= trade.short_strike
    put_breach  = trade.side == "sell_put_cs"  and bar.close <= trade.short_strike
    if call_breach or put_breach:
        _close(trade, bar, outcome="breach_max_loss",
               reason=f"short strike ${trade.short_strike:.0f} closed-through at ${bar.close:.2f}")
        # Full max loss = wing × multiplier - credit
        credit = trade.estimated_credit or 0
        # Estimate wing from long-short distance
        wing_dollars = abs(trade.long_strike - trade.short_strike)
        max_loss = (wing_dollars * 100) - credit  # 100 multiplier
        trade.pnl = -max_loss * trade.contracts
        return _exit_dict(trade, bar)

    # TP TARGET — fires intra-bar (not on same-bar)
    if not is_same_bar and pct_best >= settings.DIRECTIONAL_TP_TARGET:
        _close(trade, bar, outcome="tp_target_hit",
               reason=f"TP {settings.DIRECTIONAL_TP_TARGET:.0f}% credit captured "
                      f"(peak {trade.peak_pct_kept:+.0f}%)")
        trade.pnl = (trade.estimated_credit or 0) * (settings.DIRECTIONAL_TP_TARGET / 100.0) * trade.contracts
        return _exit_dict(trade, bar)

    # TIME stop — N min before close (not same-bar)
    if not is_same_bar and time_stop_min <= bar_min < close_min:
        # Close at current bar's close P&L
        pct_close = spread_pct_kept(
            trade.side, trade.underlying_at_signal, bar.close,
            trade.breakeven_dist_pct, model=settings.DIRECTIONAL_PNL_MODEL,
        )
        _close(trade, bar, outcome="time_close",
               reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} min — "
                      f"close at {pct_close:+.0f}% credit")
        trade.pnl = (trade.estimated_credit or 0) * (pct_close / 100.0) * trade.contracts
        return _exit_dict(trade, bar)

    return None


def _check_exit_bs(trade: PaperTrade, bar: Bar) -> Optional[dict]:
    """Black-Scholes exit logic — mirrors honest_backtest.run_honest_backtest.

    Reprices the spread each bar with shrinking time-to-expiry (real theta/gamma)
    instead of the underlying-move power-law proxy. pct_kept is measured against
    the actual entry credit; TP/loss-stop/wick-breach/time-stop all act on the
    repriced value. Ladder is gated by DIRECTIONAL_USE_DYNAMIC_STOPS (default off).
    """
    side = trade.side
    short_k = trade.short_strike
    long_k = trade.long_strike
    wing = abs(long_k - short_k)
    mult = 100
    credit_ps = (trade.estimated_credit or 0.0) / mult
    if credit_ps <= 0:
        return None
    cost = settings.DIRECTIONAL_COST_PER_SPREAD
    pm = settings.DIRECTIONAL_PREMIUM_MULT
    r5 = trade.bs_realized_std
    max_loss_pct = (credit_ps - wing) / credit_ps * 100.0  # e.g. -167%

    et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
    bar_min = et.hour * 60 + et.minute
    close_min = 16 * 60
    time_stop_min = close_min - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE

    def _pk(value_ps: float) -> float:
        return max(max_loss_pct, min(100.0, (credit_ps - value_ps) / credit_ps * 100.0))

    def _pnl(exit_pct: float) -> float:
        return (trade.estimated_credit or 0.0) * (exit_pct / 100.0) * trade.contracts - cost * trade.contracts

    # EOD — settle at intrinsic value (a close-through here is a real loss)
    if bar_min >= close_min:
        v = bs.spread_value(side, bar.close, short_k, long_k, 0.0)
        exit_pct = _pk(v)
        outcome = "max_profit_otm" if exit_pct > 0 else "breach_max_loss"
        _close(trade, bar, outcome=outcome,
               reason=f"EOD settle at {exit_pct:+.0f}% credit")
        trade.pnl = _pnl(exit_pct)
        return _exit_dict(trade, bar)

    # Same-bar guard (TP/TIME can't fire on entry bar; loss stop always can)
    is_same_bar = False
    try:
        fired_dt = datetime.fromisoformat(trade.fired_at)
        is_same_bar = bar.time <= fired_dt
    except Exception:
        is_same_bar = False

    pr = _periods_remaining(bar.time)
    tv = bs.total_vol_to_expiry(r5, pr, pm)

    if side == "sell_call_cs":
        worst_px, best_px = bar.high, bar.low
    else:
        worst_px, best_px = bar.low, bar.high

    pk_worst = _pk(bs.spread_value(side, worst_px, short_k, long_k, tv))
    pk_best = _pk(bs.spread_value(side, best_px, short_k, long_k, tv))

    if pk_best > trade.peak_pct_kept:
        trade.peak_pct_kept = pk_best

    # Dynamic ladder (default OFF). Ratchets the stop up as profit peaks.
    if settings.DIRECTIONAL_USE_DYNAMIC_STOPS:
        if trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_90:
            trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_90)
        elif trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_75:
            trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_75)
        elif trade.peak_pct_kept >= settings.DIRECTIONAL_LADDER_50:
            trade.current_stop_pct_kept = max(trade.current_stop_pct_kept, settings.DIRECTIONAL_LOCK_50)

    # STOP — worst-first ordering (pessimistic)
    if pk_worst <= trade.current_stop_pct_kept:
        if trade.current_stop_pct_kept >= 0:
            exit_pct = trade.current_stop_pct_kept          # locking a profit (limit)
            outcome = "stop_ladder_hit"
        else:
            exit_pct = pk_worst                              # losing stop — gap-honest fill
            outcome = "breach_max_loss"
        _close(trade, bar, outcome=outcome,
               reason=f"stop at {exit_pct:+.0f}% credit (peak {trade.peak_pct_kept:+.0f}%)")
        trade.pnl = _pnl(exit_pct)
        return _exit_dict(trade, bar)

    # WICK breach through short strike → at/near max loss
    wick_breach = (side == "sell_call_cs" and worst_px >= short_k) or \
                  (side == "sell_put_cs" and worst_px <= short_k)
    if wick_breach and pk_worst <= -100.0:
        _close(trade, bar, outcome="breach_max_loss",
               reason=f"wick through short ${short_k:.0f} → max loss")
        trade.pnl = _pnl(max_loss_pct)
        return _exit_dict(trade, bar)

    # TP — limit at the target level (not same-bar)
    if not is_same_bar and pk_best >= settings.DIRECTIONAL_TP_TARGET:
        exit_pct = settings.DIRECTIONAL_TP_TARGET
        _close(trade, bar, outcome="tp_target_hit",
               reason=f"TP {exit_pct:.0f}% credit captured (peak {trade.peak_pct_kept:+.0f}%)")
        trade.pnl = _pnl(exit_pct)
        return _exit_dict(trade, bar)

    # TIME stop
    if not is_same_bar and time_stop_min <= bar_min < close_min:
        v = bs.spread_value(side, bar.close, short_k, long_k, tv)
        exit_pct = _pk(v)
        outcome = "time_close"
        _close(trade, bar, outcome=outcome,
               reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE}min close at {exit_pct:+.0f}% credit")
        trade.pnl = _pnl(exit_pct)
        return _exit_dict(trade, bar)

    return None


def _close(trade: PaperTrade, bar: Bar, outcome: str, reason: str):
    trade.closed = True
    trade.closed_at = bar.time.isoformat()
    trade.underlying_at_close = bar.close
    trade.outcome = outcome  # type: ignore
    trade.exit_reason = reason


def _exit_dict(trade: PaperTrade, bar: Bar) -> dict:
    return {
        "trade_no": trade.trade_no,
        "side": trade.side,
        "outcome": trade.outcome,
        "exit_reason": trade.exit_reason,
        "pnl": trade.pnl,
        "underlying_at_close": trade.underlying_at_close,
        "underlying_at_signal": trade.underlying_at_signal,
        "short_strike": trade.short_strike,
        "instrument": trade.instrument,
        "contracts": trade.contracts,
        "fired_at": trade.fired_at,
        "closed_at": trade.closed_at,
        "estimated_credit": trade.estimated_credit,
        "peak_pct_kept": trade.peak_pct_kept,
        "strategy": trade.strategy,
    }
