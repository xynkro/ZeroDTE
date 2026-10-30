"""Config L exits on REAL quotes (2026-10-30; hardened after review the same day).

Why this exists: the engine's Black-Scholes exit model valued the two XSP spreads the probe opened on
Oct 22/23 at 2–3× their real executable credit ON THE ENTRY BAR, booked a stop, and the broker entry
was dropped before any order was placed. Zero orders reached Alpaca in 15 sessions. A model that was
shown to be 5–10× off on credits must never mark a live position.

Rules (docs/TRIAL_GATES.md 2026-10-30) for every open trade with explicit broker legs:
  managed ONLY once the broker reports the entry FILLED (status "submitted" + a real fill read);
  a canceled/rejected/expired entry is booked not_filled with no exit order; a still-working entry
  is held, and canceled at the time stop.
  cost  = short.ask − long.bid on live NBBO of the broker legs ($/share → ×100 per contract);
          a missing long quote is treated as bid 0 (an upper bound) for the STOP test only.
  C     = broker fill credit per contract, else the executable credit at entry.
  STOP  when cost ≥ (1 + CALL_STOP_MULT)·C on TWO consecutive evaluations (quotes are ~60 s apart;
          one bad print must not stop a position).
  TP    when cost ≤ (1 − TP)·C.
  TIME  at/after 16:00 − WAVE_TIME_STOP_MIN on the WALL clock (never the bar stamp).
  Never on the entry bar, never within 90 s of the open, never on a prior-day trade.
Each trade is evaluated inside its own try; a bad trade never starves the others. Fail-soft: an
unquotable chain means hold (the time stop and Alpaca's ~15:45 sweep bound it).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import settings

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
MIN_AGE_SEC = 90          # no evaluation within 90 s of the open (fill + quote settle)
QUOTE_MAX_STALE = 120.0   # NBBO quotes older than this are dropped
FILLED = {"filled"}
DEAD = {"canceled", "cancelled", "rejected", "expired", "done_for_day", "replaced", "stopped", "suspended"}


def is_live_read_trade(pt) -> bool:
    return bool(getattr(pt, "order_underlying", None)) and getattr(pt, "order_short_strike", None) is not None


def _credit_ct(pt) -> float | None:
    n = max(1, int(getattr(pt, "contracts", 1) or 1))
    brc = getattr(pt, "broker_realized_credit", None)
    if brc is not None and float(brc) > 0:
        return float(brc) / n
    emq = getattr(pt, "entry_mid_quote", None)
    if emq is not None and float(emq) > 0:
        return float(emq)
    return None


async def _confirm_fill(orch, pt) -> str:
    """'filled' | 'working' | 'dead' | 'unknown'. Reads the entry credit on a fill."""
    if pt.broker_realized_credit is not None:
        return "filled"
    trader = getattr(orch, "alpaca_trader", None)
    if trader is None or not pt.alpaca_order_id:
        return "unknown"
    try:
        order = await trader.get_order(pt.alpaca_order_id)
    except Exception as e:  # noqa: BLE001
        log.warning("live_exit: get_order failed for #%s: %s", pt.trade_no, e)
        return "unknown"
    if not order:
        return "unknown"
    st = str(order.get("status") or "").lower()
    if st in FILLED:
        try:
            from .alpaca_trader import order_net_cashflow
            cf = order_net_cashflow(order)
            if cf is not None:
                pt.broker_realized_credit = cf
                orch._persist_state()
        except Exception as e:  # noqa: BLE001
            log.warning("live_exit: fill cashflow read failed for #%s: %s", pt.trade_no, e)
        return "filled"
    if st in DEAD:
        return "dead"
    return "working"


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
        today = now.strftime("%Y-%m-%d")
        now_min = now.hour * 60 + now.minute
        time_stop_min = 16 * 60 - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE
        at_time_stop = now_min >= time_stop_min
        bar_t = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time.replace(tzinfo=ET)
        from .nbbo_chain import fetch_chain_nbbo
        chains: dict[str, dict] = {}
        breach = getattr(orch, "_lx_breach", None)
        if breach is None:
            breach = orch._lx_breach = {}
        for pt in open_trades:
            try:
                if str(pt.fired_at)[:10] != today:
                    continue                                   # prior-day ghost: the sanitizer's job
                # never on the entry bar; never within MIN_AGE_SEC of the open
                try:
                    fired = datetime.fromisoformat(str(pt.fired_at))
                    fired = fired.astimezone(ET) if fired.tzinfo else fired.replace(tzinfo=ET)
                    if bar_t <= fired:
                        continue
                except Exception:  # noqa: BLE001
                    continue                                   # unparsable → hold, never fail open
                owc = getattr(pt, "opened_wall_clock", None)
                if owc:
                    try:
                        o = datetime.fromisoformat(str(owc)); o = o.astimezone(ET) if o.tzinfo else o.replace(tzinfo=ET)
                        if (now - o).total_seconds() < MIN_AGE_SEC:
                            continue
                    except Exception:  # noqa: BLE001
                        pass
                # the broker must HOLD the position before we manage it
                if pt.broker_status in (None, "pending", "shadow", "error", "skipped_afterhours"):
                    continue
                fill = await _confirm_fill(orch, pt)
                if pt.closed:
                    continue
                if fill == "dead":
                    _book_not_filled(orch, pt, bar, "entry order canceled/rejected/expired at the broker")
                    continue
                if fill in ("working", "unknown"):
                    if at_time_stop and fill == "working":
                        try:
                            ok = await trader.cancel_order(pt.alpaca_order_id)
                            if ok:
                                _book_not_filled(orch, pt, bar, "entry still working at the time stop — canceled")
                                continue
                        except Exception as e:  # noqa: BLE001
                            log.warning("live_exit: cancel of working entry #%s failed: %s", pt.trade_no, e)
                    log.info("live_exit #%s: entry %s — holding", pt.trade_no, fill)
                    continue
                C = _credit_ct(pt)
                und = pt.order_underlying
                if und not in chains:
                    try:
                        kw = {"span_pct": 0.02, "underlying": und, "max_stale_sec": QUOTE_MAX_STALE}
                        if und == "SPXW":
                            kw.update(span_pct=0.015, step=5)
                        chains[und] = await fetch_chain_nbbo(trader, float(pt.order_short_strike), now.strftime("%y%m%d"), **kw)
                    except Exception as e:  # noqa: BLE001
                        log.warning("live_exit: %s chain fetch failed (%s) — holding", und, e)
                        chains[und] = {}
                if pt.closed:
                    continue                                   # closed by /flat or another path during the await
                nb = chains[und]
                put = pt.side == "sell_put_cs"
                rows = {r["strike"]: r for r in (nb.get("puts" if put else "calls") or [])}
                s_ = rows.get(float(pt.order_short_strike)); l_ = rows.get(float(pt.order_long_strike))
                if not C:
                    if at_time_stop:
                        _close(orch, pt, bar, outcome="time_close", pnl_ct=None,
                               reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} close (no credit basis)")
                    else:
                        log.warning("live_exit: #%s has no credit basis — holding", pt.trade_no)
                    continue
                if not s_:
                    if at_time_stop:
                        _close(orch, pt, bar, outcome="time_close", pnl_ct=None,
                               reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} close (short leg unquotable)")
                    continue
                long_bid = l_["bid"] if l_ else 0.0            # missing long → bid 0 = upper bound on buy-back
                cost = round((s_["ask"] - long_bid) * 100, 2)
                exact = l_ is not None
                pct_kept = (C - cost) / C * 100.0
                pt.peak_pct_kept = max(float(getattr(pt, "peak_pct_kept", 0.0) or 0.0), pct_kept)
                if exact:
                    pt.exit_mid_quote = cost
                stop_mult = float(settings.CALL_STOP_MULT)
                tp = float(settings.DIRECTIONAL_TP_TARGET) / 100.0
                if cost >= (1.0 + stop_mult) * C:
                    breach[pt.id] = breach.get(pt.id, 0) + 1
                    if breach[pt.id] >= 2:
                        _close(orch, pt, bar, outcome="breach_max_loss", pnl_ct=C - cost,
                               reason=f"stop: buy-back ${cost:.0f} ≥ {1 + stop_mult:.0f}× credit ${C:.0f} on 2 reads ({und} {pt.order_short_strike:.0f}/{pt.order_long_strike:.0f}{'' if exact else ', long leg unquoted'})")
                    else:
                        log.info("live_exit #%s: first breach read (cost $%.0f vs credit $%.0f) — confirming", pt.trade_no, cost, C)
                    continue
                breach[pt.id] = 0
                if exact and cost <= (1.0 - tp) * C:
                    _close(orch, pt, bar, outcome="tp_target_hit", pnl_ct=C - cost,
                           reason=f"TP {tp * 100:.0f}%: buy-back ${cost:.0f} ≤ {(1 - tp) * 100:.0f}% of credit ${C:.0f}")
                elif at_time_stop:
                    _close(orch, pt, bar, outcome="time_close", pnl_ct=(C - cost) if exact else None,
                           reason=f"T-{settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE} close at buy-back ${cost:.0f} ({pct_kept:+.0f}% kept)")
                else:
                    log.debug("live_exit #%s: cost $%.0f vs credit $%.0f (%+.0f%% kept)", pt.trade_no, cost, C, pct_kept)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad trade never starves the others
                log.warning("live_exit: trade #%s evaluation failed: %s", getattr(pt, "trade_no", "?"), e)
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("live_exit check failed: %s", e)


def _book_not_filled(orch, pt, bar, reason: str) -> None:
    if pt.closed:
        return
    pt.closed = True
    pt.closed_at = datetime.now(ET).isoformat()
    pt.outcome = "time_close"
    pt.exit_reason = f"not filled: {reason}"
    pt.broker_status = "not_filled"
    pt.pnl = 0.0
    pt.broker_realized_pnl = 0.0
    log.warning("live_exit: trade #%s NOT FILLED — %s (no exit order)", pt.trade_no, reason)
    orch._persist_state()


def _close(orch, pt, bar, *, outcome: str, pnl_ct: float | None, reason: str) -> None:
    if pt.closed:
        return
    pt.closed = True
    pt.closed_at = datetime.now(ET).isoformat()
    pt.underlying_at_close = float(bar.close)
    pt.outcome = outcome
    pt.exit_reason = reason
    pt.pnl = (pnl_ct * int(pt.contracts or 1)) if pnl_ct is not None else pt.pnl
    log.info("live_exit: trade #%s CLOSED %s — %s", pt.trade_no, outcome, reason)
    orch._persist_state()
    try:
        t = asyncio.create_task(orch._submit_alpaca_exit(pt))
        bg = getattr(orch, "_bg_tasks", None)
        if bg is None:
            bg = orch._bg_tasks = set()
        bg.add(t); t.add_done_callback(bg.discard)
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
