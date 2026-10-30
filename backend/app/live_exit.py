"""Config L exits on REAL quotes (2026-10-30).

Why this exists: the engine's Black-Scholes exit model (directional_spread_manager._check_exit_bs)
valued the two XSP spreads the probe opened on Oct 22/23 at 2.1–2.6× their real executable credit
ON THE ENTRY BAR, booked a −110%/−163% stop, and the broker entry was dropped before any order was
placed. Zero orders reached Alpaca in 15 sessions. A model that was already shown to be 5–10× off
on credits must never mark a live position.

Rules (pre-registered, docs/TRIAL_GATES.md 2026-10-30): for every open trade with explicit broker
legs (pt.order_underlying), each 5-minute bar after the ENTRY bar:
  cost  = short.ask − long.bid on the live NBBO of the broker legs ($/share → ×100 per contract)
  C     = broker_realized_credit (real fill, per contract) once known, else the entry executable credit
  STOP  when cost ≥ (1 + CALL_STOP_MULT)·C      (default 1.0 → the −100%-of-credit stop)
  TP    when cost ≤ (1 − TP)·C                   (TP = DIRECTIONAL_TP_TARGET %)
  TIME  at/after the time-stop minute (15:25 ET) → close at cost
  No exit is ever evaluated on the entry bar, or while the broker entry is still pending.
Fail-soft: an unquotable chain means "hold" (the 15:25 time stop and Alpaca's 15:45 sweep bound it).
"""
from __future__ import annotations

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def is_live_read_trade(pt) -> bool:
    return bool(getattr(pt, "order_underlying", None)) and getattr(pt, "order_short_strike", None) is not None


def _credit_ct(pt) -> float | None:
    """Real entry credit per contract ($): broker fill first, else the executable quote at entry."""
    n = max(1, int(getattr(pt, "contracts", 1) or 1))
    brc = getattr(pt, "broker_realized_credit", None)
    if brc is not None and float(brc) > 0:
        return float(brc) / n
    emq = getattr(pt, "entry_mid_quote", None)
    if emq is not None and float(emq) > 0:
        return float(emq)
    return None


async def check_exits(orch, bar) -> None:
    """Evaluate every open explicit-leg trade against live NBBO. Never raises into the bar loop."""
    try:
        open_trades = [t for t in orch.paper_trades if not t.closed and is_live_read_trade(t)]
        if not open_trades:
            return
        trader = getattr(orch, "alpaca_trader", None)
        if trader is None:
            return
        now = datetime.now(ET)
        bar_t = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time.replace(tzinfo=ET)
        bar_min = bar_t.hour * 60 + bar_t.minute
        time_stop_min = 16 * 60 - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE
        from .nbbo_chain import fetch_chain_nbbo
        chains: dict[str, dict] = {}
        for pt in open_trades:
            # never on the entry bar; never before the broker has the position
            try:
                fired = datetime.fromisoformat(str(pt.fired_at))
                fired = fired.astimezone(ET) if fired.tzinfo else fired.replace(tzinfo=ET)
                if bar_t <= fired:
                    continue
            except Exception:  # noqa: BLE001
                pass
            if pt.broker_status in (None, "pending"):
                continue
            C = _credit_ct(pt)
            if not C:
                log.warning("live_exit: trade #%s has no credit basis — holding", pt.trade_no)
                continue
            und = pt.order_underlying
            if und not in chains:
                try:
                    spot_guess = float(pt.order_short_strike)
                    chains[und] = await fetch_chain_nbbo(trader, spot_guess, now.strftime("%y%m%d"),
                                                         span_pct=0.02, underlying=und)
                except Exception as e:  # noqa: BLE001
                    log.warning("live_exit: %s chain fetch failed (%s) — holding", und, e)
                    chains[und] = {}
            nb = chains[und]
            put = pt.side == "sell_put_cs"
            rows = {r["strike"]: r for r in (nb.get("puts" if put else "calls") or [])}
            s_ = rows.get(float(pt.order_short_strike)); l_ = rows.get(float(pt.order_long_strike))
            if not s_ or not l_:
                if bar_min >= time_stop_min:
                    _close(orch, pt, bar, outcome="time_close", pnl_ct=None,
                           reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} close (legs unquotable at the mark)")
                continue
            cost = round((s_["ask"] - l_["bid"]) * 100, 2)          # buy-back $/ct, executable
            pct_kept = (C - cost) / C * 100.0
            pt.peak_pct_kept = max(float(getattr(pt, "peak_pct_kept", 0.0) or 0.0), pct_kept)
            pt.exit_mid_quote = cost
            stop_mult = float(settings.CALL_STOP_MULT)
            tp = float(settings.DIRECTIONAL_TP_TARGET) / 100.0
            if cost >= (1.0 + stop_mult) * C:
                _close(orch, pt, bar, outcome="breach_max_loss", pnl_ct=C - cost,
                       reason=f"stop: buy-back ${cost:.0f} ≥ {1 + stop_mult:.0f}× credit ${C:.0f} (NBBO {und} {pt.order_short_strike:.0f}/{pt.order_long_strike:.0f})")
            elif cost <= (1.0 - tp) * C:
                _close(orch, pt, bar, outcome="tp_target_hit", pnl_ct=C - cost,
                       reason=f"TP {tp * 100:.0f}%: buy-back ${cost:.0f} ≤ {(1 - tp) * 100:.0f}% of credit ${C:.0f}")
            elif bar_min >= time_stop_min:
                _close(orch, pt, bar, outcome="time_close", pnl_ct=C - cost,
                       reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} close at buy-back ${cost:.0f} ({pct_kept:+.0f}% kept)")
            else:
                log.debug("live_exit #%s: cost $%.0f vs credit $%.0f (%+.0f%% kept)", pt.trade_no, cost, C, pct_kept)
    except Exception as e:  # noqa: BLE001
        log.warning("live_exit check failed: %s", e)


def _close(orch, pt, bar, *, outcome: str, pnl_ct: float | None, reason: str) -> None:
    import asyncio
    pt.closed = True
    pt.closed_at = bar.time.isoformat()
    pt.underlying_at_close = float(bar.close)
    pt.outcome = outcome
    pt.exit_reason = reason
    pt.pnl = (pnl_ct * int(pt.contracts or 1)) if pnl_ct is not None else pt.pnl
    log.info("live_exit: trade #%s CLOSED %s — %s", pt.trade_no, outcome, reason)
    orch._persist_state()
    try:
        asyncio.create_task(orch._submit_alpaca_exit(pt))
    except Exception as e:  # noqa: BLE001
        log.error("live_exit: could not schedule broker close for #%s: %s", pt.trade_no, e)
    try:
        from . import telegram as tg
        orch._tg(tg.ping_signal_exit, trade_no=pt.trade_no, side=pt.side, outcome=outcome,
                 underlying_at_close=float(bar.close), underlying_at_signal=pt.underlying_at_signal,
                 short_strike=pt.short_strike, contracts=pt.contracts, pnl=pt.pnl or 0.0,
                 exit_reason=reason, pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                 peak_pct_kept=pt.peak_pct_kept, tp_pct=settings.DIRECTIONAL_TP_TARGET)
    except Exception as e:  # noqa: BLE001
        log.warning("live_exit exit ping failed: %s", e)
