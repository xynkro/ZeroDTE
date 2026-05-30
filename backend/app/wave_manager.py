"""Wave trade manager — sizing on entry, exit-condition checks per bar.

Each entry signal opens a PaperTrade with a `trade_no` (#1, #2, ... per
session). On every subsequent bar, we check open trades against:

  TP   (take-profit)  — underlying moved favorably by WAVE_FAVORABLE_MOVE_PCT
                        (proxy for option premium dropping ~75%)
  STOP (max loss)     — underlying touched / breached the short strike
  TIME (time stop)    — within N min of close, force-close if still open
  EOD  (let expire)   — at 16:00 ET, mark expired OTM (max profit if alive)

When a trigger fires, we mark the trade closed and return an exit record
that the orchestrator turns into a Telegram alert and a dashboard signal
chip with the same trade_no, formatted as "EXIT #N — TP / STOP / TIME / EOD".
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .config import settings
from .models import PaperTrade, SignalEvent, StrikeSuggestion
from .predictor import Bar


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# ────────────────────────────────────────────────────────────────────
# Sizing — derive contract count from risk budget × confluence
# ────────────────────────────────────────────────────────────────────

def recommend_contracts(
    max_loss_per_contract_usd: float,
    confluence_score: int,
    confluence_max: int = 5,
    account_size: float | None = None,
    risk_per_trade_pct: float | None = None,
) -> tuple[int, str]:
    """Return (contracts, rationale_string).

    Sizing rule (Phase 1 — aligned with WAVE_MIN_CONFLUENCE_SCORE gate):
      base_risk_$ = account_size * risk_per_trade_pct/100
      conviction_mult based on confluence_score / confluence_max:
        5/5 = 1.00x (full)
        4/5 = 0.80x (80% — solid conviction, slightly cautious)
        3/5 = 0.60x (60% — minimum gate; reduced size)
        <3/5 = 0  (gated upstream by orchestrator; should not reach here)
      effective_risk = base_risk * conviction_mult
      contracts = floor(effective_risk / max_loss_per_contract)

    FLOOR RULE: any signal that PASSES the gate (≥3/5) gets at least 1 contract,
    even if strict sizing math would round to 0 (small account). The system flags
    it as "min size — bump RISK_PER_TRADE_PCT".

    Returns (0, ...) only when conviction is genuinely too low (confluence <3/5).
    With Phase 1, those signals are gated upstream and shouldn't reach here.
    """
    account = account_size or settings.ACCOUNT_SIZE_USD
    risk_pct = risk_per_trade_pct or settings.RISK_PER_TRADE_PCT
    base_risk = account * (risk_pct / 100.0)

    # Conviction multiplier
    conv = confluence_score / confluence_max if confluence_max else 0
    if conv >= 1.0:        mult, label = 1.00, "💎 full size (5/5 max conviction)"
    elif conv >= 0.8:      mult, label = 0.80, "80% (4/5 strong)"
    elif conv >= 0.6:      mult, label = 0.60, "60% (3/5 minimum)"
    else:                  mult, label = 0.0,  "skip (below 3/5 gate)"

    if max_loss_per_contract_usd <= 0:
        return 0, "no loss data"

    effective_risk = base_risk * mult
    n = int(effective_risk // max_loss_per_contract_usd)
    # Absolute per-trade risk ceiling (SIZE_CAP_USD); MAX_CONCURRENT_POSITIONS gates
    # simultaneous OPEN trades in the orchestrator, not contracts per trade.
    if settings.SIZE_CAP_USD and settings.SIZE_CAP_USD > 0:
        n = min(n, int(settings.SIZE_CAP_USD // max_loss_per_contract_usd))
    n = max(0, n)

    # FLOOR: any signal that passed the gate (≥3/5 = mult ≥0.6) gets ≥1 contract
    # even if account is too small for full sizing. Trader can size smaller manually.
    if n == 0 and mult >= 0.6 and max_loss_per_contract_usd > 0:
        # Show the over-budget situation honestly
        n = 1
        over_pct = (max_loss_per_contract_usd / base_risk - 1) * 100
        if base_risk > 0:
            label = (f"min size 1 ct (high conf {confluence_score}/{confluence_max}, "
                     f"but $1 ct = ${max_loss_per_contract_usd:.0f} "
                     f"≈ {over_pct:+.0f}% over your ${base_risk:.0f} budget — "
                     f"bump RISK_PER_TRADE_PCT in .env to size up)")
        return n, label

    rationale = (f"{n} ct ({label}) — risking ~${n * max_loss_per_contract_usd:.0f} "
                 f"of ${base_risk:.0f} budget")
    return n, rationale


# ────────────────────────────────────────────────────────────────────
# Entry — build PaperTrade with sizing + exit thresholds set
# ────────────────────────────────────────────────────────────────────

def open_wave_trade(
    sig_event: SignalEvent,
    sp: StrikeSuggestion,             # the chosen wave-mode strike pair (XSP recommended)
    trade_no: int,
    favorable_move_pct: float | None = None,
    atr_d1: float | None = None,
) -> PaperTrade:
    """Open a wave paper trade with TP/STOP targets.

    Phase 2: TP target uses vol-scaled distance (WAVE_TP_ATR_MULT × ATR(D1))
    when atr_d1 is provided AND WAVE_TP_ATR_MULT > 0. Falls back to fixed
    WAVE_FAVORABLE_MOVE_PCT × underlying when atr_d1 is unavailable.
    """
    underlying = sig_event.underlying_price

    # TP target — vol-scaled if ATR(D1) available, else legacy fixed %
    tp_distance: float
    if atr_d1 is not None and atr_d1 > 0 and settings.WAVE_TP_ATR_MULT > 0:
        # Phase 2: vol-scaled TP
        tp_distance = settings.WAVE_TP_ATR_MULT * atr_d1
        tp_method = f"vol-scaled ({settings.WAVE_TP_ATR_MULT}×ATR_D1=${tp_distance:.1f})"
    else:
        # Legacy: fixed % of underlying
        fav = favorable_move_pct or settings.WAVE_FAVORABLE_MOVE_PCT
        tp_distance = underlying * (fav / 100.0)
        tp_method = f"fixed {fav:.2f}% of underlying"

    if sig_event.side == "sell_call_cs":
        tp_target = underlying - tp_distance
    else:
        tp_target = underlying + tp_distance

    # Stop = the short strike (any breach = max loss)
    stop_target = sp.short_strike

    # Sizing
    max_loss = sp.max_loss_dollars or 380.0  # conservative default for XSP 5pt - 12% credit
    contracts, sizing_note = recommend_contracts(
        max_loss_per_contract_usd=max_loss,
        confluence_score=sig_event.confluence_score,
        confluence_max=len(sig_event.confluence) if sig_event.confluence else 5,
    )

    from uuid import uuid4
    return PaperTrade(
        id=str(uuid4()),
        trade_no=trade_no,
        fired_at=sig_event.triggered_at,
        side=sig_event.side,
        instrument=sp.instrument,
        short_strike=sp.short_strike,
        long_strike=sp.long_strike,
        underlying_at_signal=underlying,
        proj_high_at_signal=sig_event.confluence.get("_proj_high"),  # may be None
        proj_low_at_signal=sig_event.confluence.get("_proj_low"),
        estimated_credit=sp.estimated_credit_dollars or 0.0,
        contracts=contracts,
        tp_underlying_target=tp_target,
        stop_underlying_target=stop_target,
        outcome="pending",
    ), sizing_note


# ────────────────────────────────────────────────────────────────────
# Exit checking — called on every new bar by orchestrator
# ────────────────────────────────────────────────────────────────────

def check_exit(trade: PaperTrade, bar: Bar) -> Optional[dict]:
    """Return exit-trigger dict if this bar triggers an exit, else None.

    Phase 2 — same-bar exit guard:
      • STOP fires on entry bar (capital protection — gap-through-strike)
      • TP / TIME do NOT fire on entry bar (you can't realistically TP a position
        you just opened at the bar's close).

    Trade is mutated in-place (closed=True, outcome, pnl, etc.)
    """
    if trade.closed:
        return None

    et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
    bar_minute = et.hour * 60 + et.minute
    close_minute = 16 * 60       # 16:00 ET
    time_stop_minute = close_minute - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE

    # Same-bar guard: did this bar fire the entry?
    is_same_bar = False
    if settings.WAVE_SAMEBAR_EXIT_GUARD:
        try:
            from datetime import datetime as _dt
            fired_dt = _dt.fromisoformat(trade.fired_at)
            is_same_bar = bar.time <= fired_dt
        except Exception:
            is_same_bar = False

    # 1. STOP — short strike breach.
    # Phase 3b: STOP requires bar CLOSE through strike (not intra-bar wick).
    # Aligns with TradingBlock canonical: "never use stop orders on options — manage actively".
    # Intra-bar wicks recover ~50% of the time at 10-15Δ; close-only stops are realistic.
    if settings.WAVE_STOP_REQUIRES_BAR_CLOSE:
        # Phase 3: close-through-strike only
        call_breach = trade.side == "sell_call_cs" and bar.close >= trade.stop_underlying_target
        put_breach  = trade.side == "sell_put_cs"  and bar.close <= trade.stop_underlying_target
        breach_price = bar.close
    else:
        # Legacy: intra-bar wick triggers STOP
        call_breach = trade.side == "sell_call_cs" and bar.high >= trade.stop_underlying_target
        put_breach  = trade.side == "sell_put_cs"  and bar.low  <= trade.stop_underlying_target
        breach_price = bar.high if trade.side == "sell_call_cs" else bar.low

    if call_breach:
        _close(trade, bar, outcome="stopped_breach",
               reason=f"short call ${trade.short_strike:.0f} closed-through at ${breach_price:.2f}")
        trade.pnl = _pnl_estimate(trade, "stopped_breach")
        return _exit_dict(trade, bar)
    if put_breach:
        _close(trade, bar, outcome="stopped_breach",
               reason=f"short put ${trade.short_strike:.0f} closed-through at ${breach_price:.2f}")
        trade.pnl = _pnl_estimate(trade, "stopped_breach")
        return _exit_dict(trade, bar)

    # 2. TP — favorable move trigger (NOT on entry bar)
    if not is_same_bar:
        if trade.side == "sell_call_cs" and bar.low <= trade.tp_underlying_target:
            _close(trade, bar, outcome="managed_profit",
                   reason=f"price dropped to ${bar.low:.2f} (TP ${trade.tp_underlying_target:.2f})")
            trade.pnl = _pnl_estimate(trade, "managed_profit")
            return _exit_dict(trade, bar)
        if trade.side == "sell_put_cs" and bar.high >= trade.tp_underlying_target:
            _close(trade, bar, outcome="managed_profit",
                   reason=f"price rose to ${bar.high:.2f} (TP ${trade.tp_underlying_target:.2f})")
            trade.pnl = _pnl_estimate(trade, "managed_profit")
            return _exit_dict(trade, bar)

    # 3. TIME — N min before close (NOT on entry bar; rare edge case anyway)
    if not is_same_bar and bar_minute >= time_stop_minute and bar_minute < close_minute:
        _close(trade, bar, outcome="time_close",
               reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} min — close to avoid pin risk")
        trade.pnl = _pnl_estimate(trade, "time_close")
        return _exit_dict(trade, bar)

    # 4. EOD — at or past 16:00, expire OTM (always fires, even same-bar)
    if bar_minute >= close_minute:
        _close(trade, bar, outcome="eod_expire",
               reason="expired OTM at 16:00 ET — full credit kept")
        trade.pnl = _pnl_estimate(trade, "eod_expire")
        return _exit_dict(trade, bar)

    return None


def _close(trade: PaperTrade, bar: Bar, outcome: str, reason: str):
    trade.closed = True
    trade.closed_at = bar.time.isoformat()
    trade.underlying_at_close = bar.close
    trade.outcome = outcome  # type: ignore
    trade.exit_reason = reason


def _pnl_estimate(trade: PaperTrade, outcome: str) -> float:
    """Estimated $ P&L for the whole position (× contracts)."""
    credit = trade.estimated_credit or 0.0
    # Default max profit / max loss heuristic if not stored:
    # XSP 5pt wing × 100 = $500 risk; credit collected ≈ 24% = $120; max loss ≈ $380
    if credit <= 0:
        credit = 120.0
    max_loss = 500.0 - credit  # rough; specific to XSP 5pt wing
    if outcome == "stopped_breach":
        return -max_loss * trade.contracts
    if outcome == "managed_profit":
        return (credit * 0.75) * trade.contracts  # closed at 25% of credit = kept 75%
    if outcome == "eod_expire":
        return credit * trade.contracts            # full credit
    if outcome == "time_close":
        return (credit * 0.40) * trade.contracts  # rough breakeven-ish
    return 0.0


def _exit_dict(trade: PaperTrade, bar: Bar) -> dict:
    """Return a dict the orchestrator + telegram helper can format."""
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
    }
