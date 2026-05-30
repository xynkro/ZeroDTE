"""FastAPI app — REST status + WebSocket live state stream."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .config import settings
from .orchestrator import Orchestrator

# PWA served from the backend so the phone loads the whole app from ONE origin
# (e.g. http://mings-macbook-pro.taile25066.ts.net:8765/ over Tailscale). The
# frontend's API_BASE = location.hostname:8765 then auto-resolves to this backend.
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
FRONTEND_INDEX = FRONTEND_DIR / "index.html"


log = logging.getLogger(__name__)
orch = Orchestrator()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("Starting orchestrator...")
    asyncio.create_task(orch.start())
    yield
    # Shutdown
    await orch.stop()


app = FastAPI(title="ZeroDTE Dashboard", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    # Allow any origin — backend only listens on localhost / LAN / Tailscale, all
    # private-network. Avoids needing to enumerate every device's IP.
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
async def serve_pwa():
    """Serve the single-file PWA dashboard at the root path."""
    if FRONTEND_INDEX.exists():
        return FileResponse(FRONTEND_INDEX, media_type="text/html")
    return {"error": f"frontend not found at {FRONTEND_INDEX}"}


# PWA assets — manifest, service worker, icons (installable PWA over HTTPS).
@app.get("/manifest.webmanifest")
async def pwa_manifest():
    return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def pwa_service_worker():
    # Served from root so its scope is the whole app. no-cache so updates ship fast.
    return FileResponse(FRONTEND_DIR / "sw.js", media_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})


@app.get("/icon-192.png")
async def pwa_icon_192():
    return FileResponse(FRONTEND_DIR / "icon-192.png", media_type="image/png")


@app.get("/icon-512.png")
async def pwa_icon_512():
    return FileResponse(FRONTEND_DIR / "icon-512.png", media_type="image/png")


@app.get("/apple-touch-icon.png")
async def pwa_apple_icon():
    return FileResponse(FRONTEND_DIR / "apple-touch-icon.png", media_type="image/png")


@app.get("/api/status")
async def status():
    # Distinguish feed type so dashboard shows truth
    feed_type = "none"
    if orch.feed:
        cls_name = type(orch.feed).__name__
        if cls_name == "AlpacaFeed":
            feed_type = "alpaca"
        elif cls_name == "IbkrFeed":
            feed_type = "ibkr"
        elif cls_name == "YFinanceFeed":
            feed_type = "yfinance"
        else:
            feed_type = cls_name.lower()
    alpaca_ready = hasattr(orch, "alpaca_trader") and orch.alpaca_trader is not None
    return {
        "ok": orch.state.backend_status == "ok",
        "backend_status": orch.state.backend_status,
        "ts": orch.state.ts,
        "feed_type": feed_type,
        "feed_connected": orch.feed.connected if orch.feed else False,
        "ibkr_connected": (feed_type == "ibkr"
                           and orch.feed.connected if orch.feed else False),
        "alpaca_ready": alpaca_ready,
        "subscribers": len(orch.subscribers),
        "trading_enabled": settings.TRADING_ENABLED,
        "shadow_mode": settings.SHADOW_MODE,
    }


@app.get("/api/state")
async def get_state():
    return orch.state.model_dump()


@app.get("/api/bars")
async def get_bars(limit: int = 120):
    """Recent SPX 5m bars for the dashboard chart with overlays.

    Returns bars + current projection lines + EMA values + signal markers
    so the frontend can render with Lightweight Charts.
    """
    buf = list(orch.predictor._buffer)
    if not buf:
        return {"bars": [], "proj_high": None, "proj_low": None,
                "obs_high": None, "obs_low": None,
                "ema_fast": [], "ema_slow": [], "signals": []}
    bars = buf[-limit:]
    out_bars = [{
        "time": int(b.time.timestamp()),  # unix seconds for Lightweight Charts
        "open": b.open, "high": b.high, "low": b.low, "close": b.close,
        "volume": b.volume,
    } for b in bars]

    # Compute EMA10 / EMA30 over the same window
    import numpy as np
    from .predictor import _ema, EMA_FAST_LEN, EMA_SLOW_LEN
    closes = np.array([b.close for b in buf])
    ef_arr = _ema(closes, EMA_FAST_LEN)
    es_arr = _ema(closes, EMA_SLOW_LEN)
    # Map only the last `limit` bars
    n_total = len(buf)
    n_take = len(bars)
    start_idx = n_total - n_take
    ema_fast = [
        {"time": int(buf[start_idx + i].time.timestamp()), "value": float(ef_arr[start_idx + i])}
        for i in range(n_take) if not np.isnan(ef_arr[start_idx + i])
    ]
    ema_slow = [
        {"time": int(buf[start_idx + i].time.timestamp()), "value": float(es_arr[start_idx + i])}
        for i in range(n_take) if not np.isnan(es_arr[start_idx + i])
    ]

    # Signal markers — pull from signal history
    signal_markers = []
    for sig_ev in orch._signal_history[-30:]:
        ts_str = sig_ev.triggered_at
        from datetime import datetime as _dt
        try:
            ts_dt = _dt.fromisoformat(ts_str)
            # Only include if within current bar window
            if ts_dt >= bars[0].time:
                signal_markers.append({
                    "time": int(ts_dt.timestamp()),
                    "side": sig_ev.side,
                    "price": sig_ev.underlying_price,
                    "confluence": sig_ev.confluence_score,
                })
        except (ValueError, TypeError):
            pass

    # Exit markers — closed paper trades within the current bar window.
    # Phase 1+3 dashboard parity: chart shows STOP/TP/TIME/EOD outcomes inline.
    exit_markers = []
    for pt in orch.paper_trades[-50:]:
        if not pt.closed or not pt.closed_at:
            continue
        from datetime import datetime as _dt
        try:
            ts_dt = _dt.fromisoformat(pt.closed_at)
            if ts_dt < bars[0].time:
                continue
            outcome_label = {
                "managed_profit": "TP",
                "stopped_breach": "STOP",
                "time_close":     "TIME",
                "eod_expire":     "EOD",
                "max_profit_otm": "EOD",
            }.get(pt.outcome or "", str(pt.outcome or "?"))
            exit_markers.append({
                "time": int(ts_dt.timestamp()),
                "side": pt.side,
                "trade_no": pt.trade_no,
                "outcome": outcome_label,
                "pnl": pt.pnl,
                "underlying_at_close": pt.underlying_at_close,
            })
        except (ValueError, TypeError):
            pass

    return {
        "bars": out_bars,
        "proj_high": orch.state.regime.proj_high,
        "proj_low": orch.state.regime.proj_low,
        "obs_high": orch.state.regime.obs_high,
        "obs_low": orch.state.regime.obs_low,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "signals": signal_markers,
        "exits": exit_markers,
        "regime": orch.state.regime.regime,
        "trend": orch.state.indicators.trend,
    }


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    orch.add_subscriber(ws)
    try:
        # Send current snapshot immediately
        await ws.send_text(orch.state.model_dump_json())
        while True:
            # Keep alive — wait for client messages (ignored)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        orch.remove_subscriber(ws)


@app.get("/api/paper_trades")
async def paper_trades():
    return [t.model_dump() for t in orch.paper_trades]


@app.post("/api/telegram/eod_test")
async def telegram_eod_test(date: str | None = None):
    """Manually fire an EOD summary for a given date (defaults to yesterday's
    most recent bar). Used to verify the pipeline before relying on it overnight.

    POST /api/telegram/eod_test?date=2026-05-07
    """
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    _ET = _ZI("America/New_York")
    if date is None:
        # Use the most recent bar's date in the buffer
        buf = list(orch.predictor._buffer)
        if not buf:
            return {"ok": False, "error": "no bars in predictor buffer"}
        date = buf[-1].time.astimezone(_ET).strftime("%Y-%m-%d")
    # Force-clear BOTH in-memory and persistent dedup so it fires
    from . import dedup
    orch._eod_summary_fired = None
    dedup.set("eod_summary_fired", None)  # clear persistent dedup
    await orch._fire_eod_summary(date)
    return {"ok": True, "date": date}


@app.get("/api/telegram/test")
async def telegram_test():
    """Smoke-test: pings the Zero DTE Signals + Macro Financial News topics."""
    from . import telegram as tg
    res = tg.ping_test()
    if res is None:
        return {"ok": False, "error": "TELEGRAM_BOT_TOKEN missing or send failed (check backend log)"}
    return res


@app.get("/api/macro/news")
async def macro_news():
    return {"news": orch.macro.news}


@app.get("/api/macro/calendar")
async def macro_calendar():
    return {"calendar": orch.macro.calendar}


@app.get("/api/backtest/iron_condor")
async def backtest_iron_condor(
    credit_pct: float = 16.0,
    wing: float = 5.0,
    skip_volatile: bool = True,
    instrument: str = "XSP",
    strike_placement: str = "projected",
    strike_distance_pct: float = 0.0,
):
    """Backtest iron-condor on 60 days SPX 5m historical.

    strike_placement:
      'projected' — strikes at our indicator's projected day high/low
      'distance_pct' — strikes at obs-window midpoint ± strike_distance_pct%
                        (matches tastytrade-style '1.0% OTM' or '1.5% OTM')
    """
    from .backtest_api import run_iron_condor_backtest
    return run_iron_condor_backtest(
        credit_pct_per_side=credit_pct,
        wing_width_spx=wing,
        skip_volatile_days=skip_volatile,
        instrument=instrument,
        strike_placement=strike_placement,
        strike_distance_pct=strike_distance_pct,
    )


@app.get("/api/backtest/matrix")
async def backtest_matrix(
    instrument: str = "XSP",
    strike_distance_pct: float = 1.0,
    credit_pct_per_side: float = 12.0,
    profit_target_pct: float = 25.0,
    favorable_move_pct: float = 0.3,
    data_file: str = "SPX_5m_1y.json",
    sub_window_start: str | None = None,
    sub_window_end: str | None = None,
):
    """Multi-timeframe × multi-strategy matrix backtest.

    Resamples the same 60d SPX 5m source into 5/10/15/30/60m.
    Tests three strategy variants:
      - MeanRev: Stoch reversal cross at extremes (no trend filter — original)
      - Pullback: EMA10/30 trend filter + Stoch reversal (classic pullback)
      - PullbackPlus: trend + RSI agreement + WVF spike (most selective)

    Honestly addresses earlier 'frame-lock' and 'strategy-monoculture' concerns.
    Still has REGIME-LOCK caveat: 60-day window contains no tail events (max
    daily move 2.08%) so high WRs reflect this benign regime, NOT crash regimes.
    """
    from .multi_tf_backtest import run_matrix
    return run_matrix(
        instrument=instrument,
        strike_distance_pct=strike_distance_pct,
        credit_pct_per_side=credit_pct_per_side,
        profit_target_pct=profit_target_pct,
        favorable_move_pct=favorable_move_pct,
        data_file=data_file,
        sub_window_start=sub_window_start,
        sub_window_end=sub_window_end,
    )


@app.get("/api/backtest/regime")
async def backtest_regime(data_file: str = "SPX_5m_1y.json"):
    """Profile the backtest data window — exposes regime-lock concern."""
    import json as _json
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    import statistics as _stat
    _ET = _ZI("America/New_York")
    path = settings.data_dir / "historical" / data_file
    if not path.exists():
        path = settings.data_dir / "historical" / "SPX_5m_60d.json"
    if not path.exists():
        return {"error": "missing data"}
    raw = _json.loads(path.read_text())
    by_date: dict[str, list] = {}
    for b in raw:
        et = _dt.fromisoformat(b["datetime"]).astimezone(_ET)
        d = et.strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)
    dates = sorted(by_date.keys())
    opens = [by_date[d][0]["open"] for d in dates]
    closes = [by_date[d][-1]["close"] for d in dates]
    highs = [max(b["high"] for b in by_date[d]) for d in dates]
    lows = [min(b["low"] for b in by_date[d]) for d in dates]
    daily_ranges = [(highs[i] - lows[i]) / opens[i] * 100 for i in range(len(dates))]
    daily_moves = [(closes[i] - opens[i]) / opens[i] * 100 for i in range(len(dates))]
    ups = sum(1 for i in range(1, len(closes)) if (closes[i]-closes[i-1])/closes[i-1] > 0.001)
    downs = sum(1 for i in range(1, len(closes)) if (closes[i]-closes[i-1])/closes[i-1] < -0.001)
    flat = len(closes) - 1 - ups - downs
    biggest = sorted(zip(dates, daily_moves), key=lambda x: abs(x[1]), reverse=True)[:5]
    return {
        "window_start": dates[0],
        "window_end": dates[-1],
        "n_sessions": len(dates),
        "total_move_pct": round((closes[-1] - opens[0]) / opens[0] * 100, 2),
        "high_low_range_pct": round((max(highs) - min(lows)) / min(lows) * 100, 2),
        "up_days": ups,
        "down_days": downs,
        "flat_days": flat,
        "median_daily_range_pct": round(_stat.median(daily_ranges), 2),
        "mean_daily_range_pct": round(_stat.mean(daily_ranges), 2),
        "stdev_daily_range_pct": round(_stat.stdev(daily_ranges), 2),
        "biggest_5_moves": [{"date": d, "move_pct": round(m, 2)} for d, m in biggest],
        "tail_event_warning": (
            "Window contains NO tail events. Biggest single-day move was "
            f"{abs(biggest[0][1]):.2f}%. Real blow-up regimes (e.g. Trump-Iran day, "
            "COVID Mar-2020, tariff Apr-2025) saw 4-8%+ single-day moves. "
            "Backtest WRs reflect benign regime ONLY."
        ),
    }


@app.get("/api/backtest/directional_spread")
async def backtest_directional_spread(
    short_delta: int = 40,
    wing_dollars: float = 10.0,
    confluence_min: int = 3,
    require_vwap: bool = True,
    use_dynamic_stops: bool = True,
    final_tp_target: float = 10.0,
    time_stop_min: int = 30,
    slippage_pct: float = 0.0,
    pnl_model: str = "quadratic",
    data_window: str = "auto",
):
    """Unified directional spread backtest — May 2026 pivot strategy.

    Replaces both /api/backtest/iron_condor and /api/backtest/wave with a
    single-sided credit spread + dynamic stop ladder approach.

    Backtest verdict on 4.4y data (2022-2026): DEPLOY (72/100)
      - 153 trades, 81% WR, +$6,603 total
      - Profitable in every year 2022-2026
      - Slippage robust to 20%

    Default params reflect winner config from stress testing.
    """
    from .directional_spread_backtest import run_directional_spread_backtest
    return run_directional_spread_backtest(
        short_delta=short_delta,
        wing_dollars=wing_dollars,
        confluence_min=confluence_min,
        require_vwap=require_vwap,
        use_dynamic_stops=use_dynamic_stops,
        final_tp_target=final_tp_target,
        time_stop_min=time_stop_min,
        slippage_pct=slippage_pct,
        pnl_model=pnl_model,
        data_window=data_window,
        return_trades=True,
    )


@app.get("/api/backtest/wave")
async def backtest_wave(
    credit_pct: float = 12.0,            # Phase 3 canonical: 12% (was 25% — modeled IC, not wave)
    wing: float = 5.0,
    instrument: str = "XSP",
    strike_distance_pct: float = 1.5,    # Phase 3 canonical: 1.5% OTM (was 0.5%)
    profit_target_pct: float = 75.0,     # close at 75% credit kept = 25% remaining
    favorable_move_pct: float = 0.3,
    stop_on_close: bool = True,          # Phase 3b: STOP on bar close vs intra-bar wick
    time_stop_min: int = 30,             # Phase 3c: 30min before close
    use_12mo_data: bool = True,          # Phase 3: 12mo includes tail events
    skip_volatile: bool = True,
):
    """Wave-trading backtest: simulates one SINGLE-side credit spread per indicator signal.

    PHASE 3 CANONICAL DEFAULTS (TradingBlock + CBOE + 0-dte.com aligned):
      • 1.5% OTM strikes (10-15Δ short) — was 0.5% (25Δ, negative EV)
      • STOP on bar CLOSE through strike — was intra-bar wick
      • 30min TIME stop buffer — was 15min
      • 12mo data window — was 60d benign

    PHASE 3 BUG FIX:
      • Old code doubled credit (`× 2`), modeling IC P&L on wave trades.
      • Fixed to single-side credit. Real-world numbers now.
    """
    from .backtest_api import run_wave_backtest
    return run_wave_backtest(
        credit_pct_per_side=credit_pct,
        wing_width_spx=wing,
        instrument=instrument,
        strike_distance_pct=strike_distance_pct,
        profit_target_pct=profit_target_pct,
        favorable_move_pct=favorable_move_pct,
        stop_on_close=stop_on_close,
        time_stop_min=time_stop_min,
        use_12mo_data=use_12mo_data,
        skip_volatile_days=skip_volatile,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Monitor + Alpaca paper broker endpoints
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/monitor/stats")
async def monitor_stats():
    """Aggregate statistics for the monitoring dashboard."""
    from collections import defaultdict
    trades = orch.paper_trades
    ds_trades = [t for t in trades if t.strategy == "directional_spread"]
    closed = [t for t in ds_trades if t.closed and t.pnl is not None]
    open_trades = [t for t in ds_trades if not t.closed]

    if not closed:
        return {
            "total": 0, "wins": 0, "losses": 0, "wr_pct": 0,
            "total_pnl": 0, "avg_win": 0, "avg_loss": 0,
            "max_drawdown": 0, "profit_factor": 0,
            "daily": [], "equity_curve": [], "open_trades": [],
        }

    wins = [t for t in closed if (t.pnl or 0) > 0]
    losses = [t for t in closed if (t.pnl or 0) <= 0]
    total_pnl = sum(t.pnl or 0 for t in closed)
    avg_win = sum(t.pnl or 0 for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t.pnl or 0 for t in losses) / len(losses) if losses else 0
    gross_profit = sum(t.pnl for t in wins) if wins else 0
    gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999

    sorted_closed = sorted(closed, key=lambda t: t.closed_at or "")
    cum = 0
    peak = 0
    max_dd = 0
    equity = []
    for t in sorted_closed:
        cum += (t.pnl or 0)
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
        equity.append({"time": t.closed_at, "pnl": t.pnl, "cum": round(cum, 2)})

    daily_map = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
    for t in closed:
        day = (t.fired_at or "")[:10]
        daily_map[day]["pnl"] += (t.pnl or 0)
        daily_map[day]["trades"] += 1
        if (t.pnl or 0) > 0:
            daily_map[day]["wins"] += 1
    daily = [{"date": d, **v} for d, v in sorted(daily_map.items())]

    open_info = [{
        "trade_no": t.trade_no, "side": t.side, "instrument": t.instrument,
        "short_strike": t.short_strike, "long_strike": t.long_strike,
        "fired_at": t.fired_at, "estimated_credit": t.estimated_credit,
        "peak_pct_kept": round(t.peak_pct_kept, 1),
        "current_stop_pct_kept": round(t.current_stop_pct_kept, 1),
        "contracts": t.contracts,
        "alpaca_order_id": t.alpaca_order_id,
        "broker_status": t.broker_status,
    } for t in open_trades]

    return {
        "total": len(closed), "wins": len(wins), "losses": len(losses),
        "wr_pct": round(len(wins) / len(closed) * 100, 1) if closed else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_win": round(avg_win, 2), "avg_loss": round(avg_loss, 2),
        "max_drawdown": round(max_dd, 2),
        "profit_factor": round(profit_factor, 2),
        "daily": daily, "equity_curve": equity, "open_trades": open_info,
    }


@app.get("/api/alpaca/status")
async def alpaca_status():
    """Alpaca paper trading status."""
    if not hasattr(orch, "alpaca_trader") or orch.alpaca_trader is None:
        return {"enabled": False, "reason": "alpaca_trader not initialized"}
    orders = []
    try:
        orders = await orch.alpaca_trader.get_orders("open")
    except Exception:
        pass
    return {
        "enabled": settings.PAPER_BROKER == "alpaca",
        "trading_enabled": settings.TRADING_ENABLED,
        "base_url": settings.ALPACA_BASE_URL,
        "open_orders": len(orders),
        "orders": orders[:10],
    }


@app.get("/api/tv/test")
async def tv_test():
    """Test TradingView enrichment — returns raw TA data for SPY."""
    import asyncio
    from .tv_enrichment import enrich_signal, tv_analyze, yahoo_price
    result = {}
    # Quick price check
    price = await asyncio.get_event_loop().run_in_executor(None, yahoo_price, "SPY")
    result["spy_price"] = price
    # Full enrichment
    enrichment = await asyncio.get_event_loop().run_in_executor(
        None, enrich_signal, "sell_call_cs", price or 550.0, "5m",
    )
    result["enrichment"] = enrichment
    result["tv_enrichment_enabled"] = settings.TV_ENRICHMENT_ENABLED
    return result


# ── Options Flow Scanner ──────────────────────────────────────────────

@app.get("/api/flow/last")
async def flow_last():
    """Return the last flow scan result (or null if none yet)."""
    return orch._last_flow_scan or {"scanned": False}


@app.post("/api/flow/scan")
async def flow_scan_now():
    """Trigger an on-demand flow scan (requires IBKR connected + market hours)."""
    ibkr_ok = orch.feed and orch.feed.connected
    if not ibkr_ok:
        return {"ok": False, "error": "IBKR not connected — scanner needs live TWS options data. "
                "Connect TWS and retry during market hours."}

    # Get current underlying price from quote state
    price = None
    if orch.state.quote and orch.state.quote.last:
        price = orch.state.quote.last
    if price is None:
        return {"ok": False, "error": "No underlying price available — wait for first bar"}

    try:
        from .flow_scanner import scan_options_flow, format_flow_alert

        result = await scan_options_flow(
            ibkr_feed=orch.feed,
            symbol="SPX",
            underlying_price=price,
            strike_pct_band=0.05,
        )
        if result is None:
            return {"ok": False, "error": "Chain fetch failed — check IBKR connection and market hours"}

        orch._last_flow_scan = result.to_dict()
        return {"ok": True, **result.to_dict()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/alpaca/kill")
async def alpaca_kill():
    """Emergency kill: cancel all open Alpaca orders, disable paper broker."""
    settings.PAPER_BROKER = "none"
    if not hasattr(orch, "alpaca_trader") or orch.alpaca_trader is None:
        return {"ok": True, "cancelled": 0, "paper_broker": "none"}
    try:
        orders = await orch.alpaca_trader.get_orders("open")
        cancelled = 0
        for o in orders:
            if await orch.alpaca_trader.cancel_order(o["id"]):
                cancelled += 1
        return {"ok": True, "cancelled": cancelled, "paper_broker": "none"}
    except Exception as e:
        return {"ok": False, "error": str(e), "paper_broker": "none"}
