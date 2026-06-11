"""Assemble the 'cockpit' payload for the Signals view — the actionable brain:
the latest signal ('sell this spread at these strikes'), tonight's call/put sell
zones, and live open positions with their TP/stop targets + the daily time-stop.

Used by BOTH GET /api/signals and the snapshot publisher, so the phone (Pages)
shows the same thing the backend-served terminal does. The live countdown / P&L
tick client-side off the timestamps + targets in this payload.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .config import settings

ET = ZoneInfo("America/New_York")
# Default visual chart — a generic SPX 5m chart. Swap for a saved Pine layout URL.
TV_CHART_URL = "https://www.tradingview.com/chart/?symbol=SP%3ASPX&interval=5"


def _primary_strike(s):
    """First populated strike suggestion on a SignalEvent (directional / wave / IC)."""
    for attr in ("suggested_strikes", "wave_strikes", "ic_strikes"):
        lst = getattr(s, attr, None)
        if lst:
            return lst[0]
    return None


def _sig_dict(s) -> dict:
    sg = _primary_strike(s)
    return {
        "side": s.side,
        "triggered_at": s.triggered_at,
        "underlying_price": s.underlying_price,
        "confluence_score": getattr(s, "confluence_score", None),
        "instrument": getattr(sg, "instrument", None),  # execution scale (e.g. SPY)
        "short_strike": getattr(sg, "short_strike", None),
        "long_strike": getattr(sg, "long_strike", None),
        "credit": getattr(sg, "estimated_credit_dollars", None),
        "roi_pct": getattr(sg, "roi_pct", None),
    }


def _pos_dict(p) -> dict:
    return {
        "trade_no": p.trade_no,
        "side": p.side,
        "instrument": p.instrument,
        "short_strike": p.short_strike,
        "long_strike": p.long_strike,
        "fired_at": p.fired_at,
        "credit": p.estimated_credit,
        "contracts": p.contracts,
        "tp_underlying_target": p.tp_underlying_target,
        "stop_underlying_target": p.stop_underlying_target,
        "pnl": p.pnl,
        "peak_pct_kept": round(p.peak_pct_kept, 1),
        "broker_status": p.broker_status,
    }


def _time_stop_iso() -> str:
    """Today's daily time-stop = 16:00 ET close minus the configured minutes.
    An ISO timestamp the frontend counts down to (only meaningful intraday)."""
    mins = settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE
    now_et = datetime.now(ET)
    close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    return (close - timedelta(minutes=mins)).isoformat()


def _today_block(orch) -> dict:
    """Today's heartbeat + activity so the dashboard never looks frozen: did the
    engine evaluate today, how many signals, and why each was stood aside."""
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")
    lb = getattr(orch, "_last_bar_wall", None)
    try:
        last_bar = lb.strftime("%H:%M ET") if lb is not None else None
    except Exception:
        last_bar = None
    fired = sum(1 for s in (getattr(orch, "_signal_history", None) or [])
                if (getattr(s, "triggered_at", "") or "")[:10] == today)
    same_day = getattr(orch, "_today_date", None) == today
    evaluated = getattr(orch, "_today_evaluated", 0) if same_day else 0
    gated = dict(getattr(orch, "_today_gated", {})) if same_day else {}
    gated_n = sum(gated.values())
    is_weekday = now_et.weekday() < 5
    try:
        is_rth = bool(orch._is_rth_now())
    except Exception:
        is_rth = False
    if not is_weekday:
        status = "Market closed (weekend)"
    elif fired:
        status = f"{fired} signal{'s' if fired != 1 else ''} fired today"
    elif gated_n:
        top = max(gated, key=gated.get)
        status = f"{evaluated} signal{'s' if evaluated != 1 else ''} evaluated · all stood aside — {top}"
    elif is_rth:
        status = "Live — no qualifying signal yet"
    else:
        status = "No signal today"
    return {
        "date": today, "weekday": is_weekday, "market_open": is_rth,
        "last_bar_et": last_bar, "evaluated": evaluated, "fired": fired,
        "gated": gated, "status": status,
    }


def _meic_block(orch) -> dict:
    """Today's iron-condor ladder for the dashboard: every scheduled slot with its
    build (strikes/credit/broker status) or 'pending/missed' — so the PWA shows
    exactly what the MEIC book is doing, rung by rung."""
    now_et = datetime.now(ET)
    today = now_et.strftime("%Y-%m-%d")
    now_min = now_et.hour * 60 + now_et.minute

    def _ic_dict(b):
        return {
            "build_id": b.build_id,
            "time": (b.build_id or "")[-4:][:2] + ":" + (b.build_id or "")[-2:],
            "call_short": getattr(b.call_leg, "short_strike", None) if b.call_leg else None,
            "call_long": getattr(b.call_leg, "long_strike", None) if b.call_leg else None,
            "put_short": getattr(b.put_leg, "short_strike", None) if b.put_leg else None,
            "put_long": getattr(b.put_leg, "long_strike", None) if b.put_leg else None,
            "credit": b.total_credit_dollars,
            "status": b.broker_status or ("alert_only" if b.available else "skipped"),
            "stopped": b.broker_status == "closed_stop",
        }

    todays = [b for b in (orch.state.iron_condor_history or [])
              if (b.build_id or "").startswith(f"ic_{today}")]
    used = set()
    rungs = []
    raw = settings.MEIC_ENTRY_TIMES_ET if settings.MEIC_ENABLED else settings.EOD_IC_BUILD_ET
    for s in str(raw).split(","):
        s = s.strip()
        try:
            hh, mm = s.split(":")
            smin = int(hh) * 60 + int(mm)
        except ValueError:
            continue
        match = next((b for b in todays if b.build_id not in used and abs(
            (int((b.build_id or "0000")[-4:][:2]) * 60 + int((b.build_id or "00")[-2:])) - smin) <= 25), None)
        if match is not None:
            used.add(match.build_id)
            rungs.append({"slot": s, **_ic_dict(match)})
        else:
            state = "pending" if now_min < smin else ("window" if now_min <= smin + 25 else "missed")
            if now_et.weekday() >= 5 or not getattr(orch, "_is_rth_now", lambda: False)():
                state = "pending"
            rungs.append({"slot": s, "status": state})
    for b in todays:  # manual /icnow builds outside the schedule
        if b.build_id not in used:
            rungs.append({"slot": "manual", **_ic_dict(b)})
    return {"enabled": settings.MEIC_ENABLED and settings.IC_EXECUTION_ENABLED,
            "contracts": settings.IC_CONTRACTS, "rungs": rungs}


def assemble(orch) -> dict:
    st = orch.state
    q = getattr(st, "quote", None)
    underlying = getattr(q, "last", None)
    sigs = list(getattr(st, "last_signals", []) or [])
    reg = st.regime
    open_pos = [p for p in orch.paper_trades
                if not p.closed and getattr(p, "strategy", None) == "directional_spread"]
    return {
        "generated_at": st.ts,
        "underlying": underlying,
        "feed": getattr(st, "feed_type", None),
        "backend_status": getattr(st, "backend_status", None),
        "regime": {
            "regime": reg.regime,
            "classified": reg.classified,
            "proj_high": reg.proj_high,   # call-spread sell zone
            "proj_low": reg.proj_low,     # put-spread sell zone
            "obs_drift_pct": reg.obs_drift_pct,
        },
        "today": _today_block(orch),
        "meic": _meic_block(orch),
        "latest_signal": _sig_dict(sigs[-1]) if sigs else None,
        "recent_signals": [_sig_dict(s) for s in reversed(sigs[-6:])],
        "open_positions": [_pos_dict(p) for p in open_pos],
        "time_stop_at": _time_stop_iso(),
        "time_stop_min": settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE,
        "tp_target_pct": settings.DIRECTIONAL_TP_TARGET,
        "tv_chart_url": TV_CHART_URL,
    }
