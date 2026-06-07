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
        "latest_signal": _sig_dict(sigs[-1]) if sigs else None,
        "recent_signals": [_sig_dict(s) for s in reversed(sigs[-6:])],
        "open_positions": [_pos_dict(p) for p in open_pos],
        "time_stop_at": _time_stop_iso(),
        "time_stop_min": settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE,
        "tp_target_pct": settings.DIRECTIONAL_TP_TARGET,
        "tv_chart_url": TV_CHART_URL,
    }
