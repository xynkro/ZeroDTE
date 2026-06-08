"""Orchestrator: market feed → signal logic → strike pricing → WebSocket.

Feed priority: Alpaca (REST, no threads) → IBKR (TWS, thread-based) → yfinance (fallback).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Set
from uuid import uuid4
from zoneinfo import ZoneInfo

from .config import settings
from .ibkr_feed import IbkrFeed
from .live_predictor import LivePredictor
from .macro_news import MacroFeed
from .models import (
    DashboardState, IndicatorState, IronCondorBuilder, LiveQuote,
    PaperTrade, RegimeState, SignalEvent, StrikeSuggestion,
)
from .predictor import Bar
from .strikes import (
    StrikePair, build_strike_pair_from_chain, fallback_pair_no_chain,
    build_strike_pair_melded,
    WAVE_DELTA, IC_DELTA, MULTIPLIERS, DEFAULT_WING_WIDTH,
)
from . import telegram as tg
from . import dedup
from .state_store import store as state_store
from . import tv_enrichment


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _to_pydantic(sp: StrikePair, underlying_price: float | None = None) -> StrikeSuggestion:
    """Convert internal StrikePair → wire-format StrikeSuggestion."""
    notional = None
    if underlying_price is not None:
        scale = 0.1 if sp.instrument in ("XSP", "SPY") else 1.0
        notional = underlying_price * scale * sp.multiplier
    return StrikeSuggestion(
        instrument=sp.instrument, side=sp.side, mode=sp.mode,
        short_strike=sp.short_strike, long_strike=sp.long_strike,
        wing_width=sp.wing_width, multiplier=sp.multiplier,
        short_delta=sp.short_delta, long_delta=sp.long_delta,
        short_bid=sp.short_bid, short_ask=sp.short_ask,
        long_bid=sp.long_bid,  long_ask=sp.long_ask,
        estimated_credit=sp.estimated_credit,
        estimated_credit_dollars=sp.estimated_credit_dollars,
        max_loss_dollars=sp.max_loss_dollars,
        breakeven=sp.breakeven, roi_pct=sp.roi_pct,
        pop_estimate_pct=sp.pop_estimate_pct,
        notional_per_contract=notional,
        warnings=list(sp.warnings),
    )


class Orchestrator:
    def __init__(self):
        self.feed = IbkrFeed()
        self.macro = MacroFeed()
        self._daily_atr: float = 50.0
        self.predictor = LivePredictor(atr_d1_lookup=lambda d: self._daily_atr)
        self.subscribers: Set = set()
        self.state = DashboardState(
            ts=datetime.now(timezone.utc).isoformat(),
            backend_status="warming_up",
        )
        self.paper_trades: list[PaperTrade] = []
        self._signal_history: list[SignalEvent] = []
        # Per-trade Alpaca entry tasks (by pt.id) — the exit awaits the entry so a
        # same-bar stop can't close internally before the broker order is placed
        # (which would orphan a live position).
        self._broker_entry_tasks: dict = {}
        # Feed-staleness watchdog state — wall-clock of the last bar handled.
        self._last_bar_wall: datetime | None = None
        self._feed_stale_alerted: bool = False
        self._staleness_task = None
        # Dealer-gamma (GEX) regime cache + refresh task.
        self._gex = None
        self._gex_task = None
        # Cache last fetched chain per instrument to avoid hammering IBKR
        self._last_chain_ts: dict[str, datetime] = {}
        self._last_chain: dict[str, dict] = {}
        self._chain_ttl_seconds = 60
        self._eod_ic_built_today: str | None = None  # date-string (YYYY-MM-DD ET)
        self._eod_summary_fired: str | None = None   # date-string (YYYY-MM-DD ET)
        # Phase 2: mid-session vol re-gate state. Once flipped to volatile mid-day,
        # block new wave entries for rest of session. Resets on new session.
        self._mid_session_volatile: bool = False
        self._mid_session_volatile_date: str | None = None
        self._trade_seq_today = 0                     # resets each session day
        self._trade_seq_date: str | None = None       # session date when seq last reset
        self.bot_poller = None  # type: ignore  # set in start()
        self._eod_safety_task = None
        self._reconnect_task = None
        self._midday_status_pinged: str | None = None  # date-string; once per session
        # Flow scanner: tracks which scan windows have fired today
        self._flow_scans_fired: set[str] = set()  # e.g. {"2026-05-25_10:30", ...}
        self._last_flow_scan: dict | None = None   # last FlowScanResult.to_dict()

        # ── Restore live state from disk (survives backend restarts) ──
        self._restore_from_disk()

    def _restore_from_disk(self) -> None:
        """Load persisted state if it's from today's session. Restores
        signal_history, paper_trades, iron_condor_history, trade counter,
        and IC built-flag so a mid-session restart doesn't lose context."""
        data = state_store.load()
        if data is None:
            return
        try:
            from .models import SignalEvent, PaperTrade, IronCondorBuilder

            def _safe_load(cls, items, label):
                """Load per-item: one malformed record is dropped + logged, never
                discards the whole session's state (resilient restore)."""
                out = []
                for it in (items or []):
                    try:
                        out.append(cls(**it))
                    except Exception as e:  # noqa: BLE001
                        log.warning("state restore: dropping malformed %s (%s): %s",
                                    label, str(it)[:80], e)
                return out

            self._signal_history = _safe_load(SignalEvent, data.get("signal_history"), "signal")
            self.paper_trades = _safe_load(PaperTrade, data.get("paper_trades"), "paper_trade")
            self.state.iron_condor_history = _safe_load(
                IronCondorBuilder, data.get("iron_condor_history"), "ic_build")
            if self.state.iron_condor_history:
                # Restore latest as the active IC
                latest = self.state.iron_condor_history[-1]
                if latest.available:
                    self.state.iron_condor = latest
            self._trade_seq_today = int(data.get("trade_seq_today") or 0)
            self._trade_seq_date = data.get("trade_seq_date")
            self._eod_ic_built_today = data.get("eod_ic_built_today")
            self.state.last_signals = self._signal_history[-20:]
            self.state.open_positions = [t for t in self.paper_trades if not t.closed]
            log.info(
                "state restored: %d signals, %d trades (%d open), %d IC builds, next trade_no=%d",
                len(self._signal_history),
                len(self.paper_trades),
                len(self.state.open_positions),
                len(self.state.iron_condor_history),
                self._trade_seq_today + 1,
            )
        except Exception as e:
            log.warning("state restore failed (continuing fresh): %s", e)

    def _persist_state(self) -> None:
        """Schedule debounced write of live state to disk.

        The snapshot is materialized SYNCHRONOUSLY here on the event-loop thread
        (no await between the list copies and model_dump), so the background writer
        thread never iterates self.paper_trades / self._signal_history while the
        loop is appending to them ('list changed size during iteration' / torn write).
        """
        try:
            snapshot = {
                "signal_history": [s.model_dump() for s in list(self._signal_history)],
                "paper_trades":   [t.model_dump() for t in list(self.paper_trades)],
                "iron_condor_history": [b.model_dump() for b in list(self.state.iron_condor_history)],
                "trade_seq_today": self._trade_seq_today,
                "trade_seq_date":  self._trade_seq_date,
                "eod_ic_built_today": self._eod_ic_built_today,
            }
            state_store.save_async(snapshot)
        except Exception as e:
            log.warning("persist_state failed: %s", e)

    async def start(self):
        log.info("Orchestrator starting...")
        # Spin up Telegram bot command poller (handles /status, /shutup, etc.)
        from .telegram_bot import TelegramBotPoller
        self.bot_poller = TelegramBotPoller(self)
        await self.bot_poller.start()
        await self.macro.start()  # async news + calendar polling
        # 60s safety-net for EOD summary — fires if past 16:30 ET and no
        # bar-driven trigger arrived
        self._eod_safety_task = asyncio.create_task(self._eod_safety_loop())
        # Feed-staleness alarm (detects a frozen feed during RTH)
        self._staleness_task = asyncio.create_task(self._feed_staleness_watchdog())
        # Dealer-gamma (GEX) regime refresh — context for sizing/analysis
        if settings.GEX_ENABLED:
            self._gex_task = asyncio.create_task(self._gex_refresh_loop())

        # Alpaca trader for ORDER execution — initialize up-front when PAPER_BROKER=
        # alpaca, independent of which DATA feed wins below. Previously it was only
        # created on the Alpaca/IBKR feed paths, so yfinance/idle sessions left it
        # None and every trade got broker_status=None (no order ever attempted).
        if settings.PAPER_BROKER == "alpaca" and settings.ALPACA_API_KEY:
            try:
                from .alpaca_trader import AlpacaTrader
                self.alpaca_trader = AlpacaTrader()
                self.state.alpaca_ready = True
                log.info("Alpaca trader initialized up-front (PAPER_BROKER=alpaca)")
            except Exception as e:
                log.warning("Alpaca trader init failed: %s", e)

        # ── Feed priority: Alpaca → IBKR → yfinance → idle ──
        import os

        # 1. Try Alpaca first (pure async REST, no threads, options chain)
        if settings.ALPACA_API_KEY:
            log.info("Trying Alpaca feed (REST, zero threads)...")
            from .alpaca_feed import AlpacaFeed
            alpaca = AlpacaFeed()
            alpaca.on_bar(self.handle_bar)
            alpaca_ok = await alpaca.start_spx_5m()
            if alpaca_ok:
                self.feed = alpaca
                self.alpaca_feed = alpaca  # keep reference for options chain access
                self.state.backend_status = "ok"
                self.state.feed_type = "alpaca"
                self.state.notes.append(
                    "📡 Live data via Alpaca (SPY 5m → SPX). "
                    "REST API — no socket drops, no thread leaks. "
                    "Options chain + paper trading available."
                )
                atr = await alpaca.daily_atr("SPX", 14)
                if atr is not None:
                    self._daily_atr = atr
                    log.info("Alpaca D1 ATR(14) = %.2f", atr)
                # Initialize Alpaca trader for order execution
                from .alpaca_trader import AlpacaTrader
                self.alpaca_trader = AlpacaTrader()
                self.state.alpaca_ready = True
                log.info("Alpaca feed + trader ready (paper mode)")
                return
            else:
                log.warning("Alpaca feed failed — trying IBKR...")

        # 2. Try IBKR (needs TWS running, thread-based)
        ok = await self.feed.connect()
        if ok:
            atr = await self.feed.daily_atr("SPX", 14)
            if atr is not None:
                self._daily_atr = atr
                log.info("SPX D1 ATR(14) = %.2f", atr)
            self.feed.on_bar(self.handle_bar)
            await self.feed.start_spx_5m()
            self.state.backend_status = "ok"
            self.state.feed_type = "ibkr"
            # Initialize Alpaca trader for order execution even when IBKR is the
            # data feed. PAPER_BROKER=alpaca means we want Alpaca for ORDERS,
            # not necessarily for data. FIX 2026-05-24: previously alpaca_trader
            # was only created in the Alpaca-feed path, so IBKR-fed sessions had
            # broker_status=error on every trade.
            if settings.PAPER_BROKER == "alpaca":
                try:
                    from .alpaca_trader import AlpacaTrader
                    self.alpaca_trader = AlpacaTrader()
                    self.state.alpaca_ready = True
                    log.info("Alpaca trader initialized (IBKR feed + Alpaca orders)")
                except Exception as e:
                    log.warning("Alpaca trader init failed (orders will shadow): %s", e)
            return

        # 3. IBKR offline — try fallbacks
        log.warning("IBKR offline — checking fallback options...")
        self.state.backend_status = "ibkr_disconnected"

        # Opt-in mock feed (dev/testing only)
        if os.environ.get("MOCK_FEED_ENABLED", "").lower() in ("true", "1", "yes"):
            log.warning("MOCK_FEED_ENABLED=true → starting historical replay (dev mode)")
            self.state.notes.append("MOCK_FEED active (dev mode).")
            await self._start_mock_feed()
            return

        # 4. yfinance fallback (free Yahoo Finance feed via SPY scaled to SPX)
        yfinance_enabled = os.environ.get("YFINANCE_FALLBACK", "true").lower() in ("true", "1", "yes")
        if yfinance_enabled:
            log.info("Activating yfinance fallback (SPY 5m, scaled to SPX)")
            from .yfinance_feed import YFinanceFeed
            self.feed = YFinanceFeed()
            self.feed.on_bar(self.handle_bar)
            yf_ok = await self.feed.start_spx_5m()
            if yf_ok:
                self.state.backend_status = "ok"
                self.state.feed_type = "yfinance"
                self.state.notes.append(
                    "📡 Live data via Yahoo Finance fallback (SPY 5m scaled to SPX). "
                    "Real-time during market hours; ~1-2 min lag at the bar boundary. "
                    "No options-chain access (strikes use projected-boundary fallback). "
                    "Open TWS to restore IBKR + chain pricing."
                )
                atr = await self.feed.daily_atr("SPX", 14)
                if atr is not None:
                    self._daily_atr = atr
                    log.info("yfinance D1 ATR(14) = %.2f", atr)
                # Still run reconnect loop in background — when IBKR comes online,
                # we'll switch back from yfinance to it
                self._reconnect_task = asyncio.create_task(self._ibkr_reconnect_loop())
                return
            else:
                log.warning("yfinance fallback failed — going idle")

        # 5. Last resort: idle
        self.state.notes.append(
            "⚠️ All feeds unavailable (Alpaca, IBKR, yfinance) — dashboard idle."
        )
        self._reconnect_task = asyncio.create_task(self._ibkr_reconnect_loop())
        return

    async def stop(self):
        if self._reconnect_task:
            self._reconnect_task.cancel()
            try:
                await self._reconnect_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._eod_safety_task:
            self._eod_safety_task.cancel()
            try:
                await self._eod_safety_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._staleness_task:
            self._staleness_task.cancel()
            try:
                await self._staleness_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._gex_task:
            self._gex_task.cancel()
            try:
                await self._gex_task
            except (asyncio.CancelledError, Exception):
                pass
        if self.bot_poller:
            await self.bot_poller.stop()
        if self.feed is not None:
            await self.feed.disconnect()
        await self.macro.stop()
        if getattr(self, "alpaca_trader", None) is not None:
            try:
                await self.alpaca_trader.close()  # close the httpx client (was leaked)
            except Exception:
                pass

    async def _ibkr_reconnect_loop(self):
        """Try to attach to IBKR every 60s. Handles both:
          (a) idle → IBKR  (user opens TWS for the first time after backend boot)
          (b) yfinance → IBKR  (TWS comes online while we're on Yahoo fallback)

        IMPORTANT: reuses a single IbkrFeed probe to avoid thread-per-attempt
        leak.  Previous version created a new IbkrFeed() every 30s and never
        disconnected on failure — leaked one worker thread each time, exhausting
        the OS thread limit after ~2 days.
        """
        from .ibkr_feed import IbkrFeed
        from .yfinance_feed import YFinanceFeed

        ibkr_probe: Optional[IbkrFeed] = None

        while True:
            try:
                await asyncio.sleep(60)  # 60s (was 30s — no need to hammer)
                # Already on IBKR? nothing to do.
                if isinstance(self.feed, IbkrFeed) and self.feed.connected:
                    return

                # Reuse (or create) a single probe instance
                if ibkr_probe is None:
                    ibkr_probe = IbkrFeed()

                ok = await ibkr_probe.connect()
                if not ok:
                    # CRITICAL: clean up the worker thread so we don't leak
                    try:
                        await ibkr_probe.disconnect()
                    except Exception:
                        pass
                    ibkr_probe = None  # next attempt gets a fresh instance
                    continue

                # Success — tear down current feed (yfinance polling, etc.)
                log.info("IBKR reachable — switching to live feed")
                if isinstance(self.feed, YFinanceFeed):
                    await self.feed.stop()
                self.feed = ibkr_probe
                ibkr_probe = None  # ownership transferred to self.feed
                self.state.backend_status = "ok"
                self.state.notes.append("IBKR reconnected — full live feed + chain access restored.")
                atr = await self.feed.daily_atr("SPX", 14)
                if atr is not None:
                    self._daily_atr = atr
                self.feed.on_bar(self.handle_bar)
                await self.feed.start_spx_5m()
                return
            except asyncio.CancelledError:
                # Clean up probe on shutdown
                if ibkr_probe is not None:
                    try:
                        await ibkr_probe.disconnect()
                    except Exception:
                        pass
                return
            except Exception as e:
                log.warning("reconnect loop error: %s", e)
                # Also clean up probe on unexpected errors
                if ibkr_probe is not None:
                    try:
                        await ibkr_probe.disconnect()
                    except Exception:
                        pass
                    ibkr_probe = None

    async def _eod_safety_loop(self):
        """Wakes every 60s. If past 16:30 ET on a weekday and EOD summary
        hasn't fired today, fire it. Catches the case where bars stop arriving
        before the bar-driven trigger ran (e.g. IBKR quiet, backend just booted).

        BUG FIX (2026-05-13): also checks persistent dedup, not just in-memory
        flag. Before this fix, if _fire_eod_summary threw and reset the in-memory
        flag but the persistent dedup was already set, this loop would re-enter
        _fire_eod_summary which would immediately return (persistent dedup).
        Now both checks must pass before retrying."""
        try:
            while True:
                await asyncio.sleep(60)
                now_et = datetime.now(ET)
                # Weekdays only (0=Mon..4=Fri)
                if now_et.weekday() > 4:
                    continue
                # Past 16:30 ET (give 30 min buffer for late bars)
                if now_et.hour < 16 or (now_et.hour == 16 and now_et.minute < 30):
                    continue
                date_str = now_et.strftime("%Y-%m-%d")
                # Check both in-memory and persistent dedup
                if self._eod_summary_fired == date_str:
                    continue
                if dedup.already_done("eod_summary_fired", date_str):
                    # Persistent says done but in-memory doesn't — sync up
                    self._eod_summary_fired = date_str
                    continue
                # We got here — neither guard says done. Fire from safety.
                log.info("EOD safety-net firing summary for %s", date_str)
                await self._fire_eod_summary(date_str)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.exception("EOD safety loop error: %s", e)

    def _is_rth_now(self) -> bool:
        """True during regular US equity hours (Mon-Fri 09:30-16:00 ET)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:  # Sat/Sun
            return False
        m = now.hour * 60 + now.minute
        return 9 * 60 + 30 <= m < 16 * 60

    async def _feed_staleness_watchdog(self):
        """Alarm if the feed stops delivering bars during RTH.

        The poll loop re-dispatches a bar every few seconds while the feed is alive,
        so >5 min of silence during market hours means it's frozen (e.g. Alpaca
        auth/5xx makes _fetch_bars return [] while connected stays True) and open
        0DTE positions are no longer being checked for TP/STOP/EOD. No auto-failover
        — just a loud alarm (dashboard status=error + Telegram) so it can't go unnoticed.
        """
        STALE_SEC = 300
        while True:
            try:
                await asyncio.sleep(60)
                if not self._is_rth_now() or self._last_bar_wall is None:
                    continue
                age = (datetime.now(ET) - self._last_bar_wall).total_seconds()
                if age > STALE_SEC and not self._feed_stale_alerted:
                    self._feed_stale_alerted = True
                    feed_name = type(self.feed).__name__ if self.feed else "none"
                    log.error("FEED STALE: no bar in %.0fs during RTH (feed=%s connected=%s). "
                              "Open positions are NOT being managed — investigate / restart.",
                              age, feed_name, getattr(self.feed, "connected", "?"))
                    self.state.backend_status = "error"
                    try:
                        tg.ping_feed_stale(age_min=age / 60.0, feed=feed_name,
                                           pwa_url=settings.DASHBOARD_PUBLIC_URL or None)
                    except Exception as e:
                        log.warning("feed-stale ping failed: %s", e)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("staleness watchdog error: %s", e)

    async def _gex_refresh_loop(self):
        """Fetch dealer-gamma (GEX) regime from CBOE on a slow cadence during the
        premarket→close window. Stores self._gex + self.state.gex for display and
        for stamping onto trades at entry. Pure context — does not gate entries here
        (sizing is applied at entry only when GEX_SIZING_ENABLED). Never throws."""
        from .gex import fetch_gex
        first = True
        while True:
            try:
                now = datetime.now(ET)
                mins = now.hour * 60 + now.minute
                # Fetch window: 07:00–16:30 ET on weekdays (premarket + RTH).
                in_window = now.weekday() < 5 and (7 * 60) <= mins <= (16 * 60 + 30)
                if in_window or first:
                    res = await fetch_gex(settings.GEX_SYMBOL)
                    if res.ok:
                        self._gex = res
                        self.state.gex = {
                            "regime": res.regime, "net_ratio": res.net_ratio,
                            "net_gex_b": res.net_gex_b, "spot": res.spot,
                            "call_wall": res.call_wall, "put_wall": res.put_wall,
                            "summary": res.summary(), "asof": res.asof,
                        }
                        log.info("GEX refresh: %s", res.summary())
                    first = False
                await asyncio.sleep(max(60, settings.GEX_REFRESH_MIN * 60))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("GEX refresh error: %s", e)
                await asyncio.sleep(300)

    async def handle_bar(self, bar: Bar):
        # Feed liveness — record receipt time; clear any stale alert now data flows.
        self._last_bar_wall = datetime.now(ET)
        if self._feed_stale_alerted:
            self._feed_stale_alerted = False
            if self.state.backend_status == "error":
                self.state.backend_status = "ok"
            log.info("Feed recovered — bars flowing again.")
        signals = self.predictor.on_bar(bar)
        # Telegram gate: only fire pings on LIVE bars. The window is FEED-AWARE so the
        # system keeps working when Alpaca is down and it falls back to the free Yahoo
        # feed (which lags ~15 min). A fixed 10-min window silently killed EVERY ping on
        # the fallback (the "dead night" bug); real-time feeds stay strict for fast
        # stale detection. Mock-feed replay (bars days old) is excluded by either window.
        bar_age = (datetime.now(ET) - bar.time.astimezone(ET)).total_seconds()
        live_window = 1500 if getattr(self.state, "feed_type", "") == "yfinance" else 600
        self._is_live_bar = bar_age < live_window
        # Morning alive heartbeat — fires once per session at first live bar.
        # Guarantees the user always sees a Telegram ping even on no-signal days.
        self._maybe_ping_morning_alive(bar)
        # Phase 2: update mid-session volatility flag (used by entry gate)
        self._update_mid_session_volatility(bar)
        await self._refresh_state(bar, new_signals=signals)
        # Check open wave trades for TP / STOP / TIME / EOD exits → fire EXIT alerts
        self._dispatch_exit_check(bar)
        self._maybe_ping_midday_status(bar)
        await self._maybe_scan_flow(bar)
        await self._maybe_build_eod_ic(bar)
        # NEW: IC stop-loss check (Theta Profits "Breakeven IC" rule)
        await self._check_ic_stop_loss(bar)
        await self._maybe_fire_eod_summary(bar)
        await self._broadcast()

    def _maybe_ping_morning_alive(self, bar: Bar):
        """Fire a 'good morning' heartbeat on the first live bar of each session.
        Ensures the user always sees at least ONE Telegram ping per trading day,
        even when no signals, no IC build, or the regime doesn't classify.
        Added 2026-05-12 after repeated 'no telegram prompts' feedback."""
        if not getattr(self, "_is_live_bar", False):
            return
        et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        # Only fire during regular session (weekday, 09:30-10:00 ET window)
        if et.weekday() > 4:
            return
        bar_min = et.hour * 60 + et.minute
        if bar_min < 9 * 60 + 30 or bar_min > 10 * 60:
            return  # only first 30 min of session
        session_key = et.strftime("%Y-%m-%d")
        if dedup.already_done("morning_alive_pinged", session_key):
            return
        dedup.mark_done("morning_alive_pinged", session_key)
        try:
            feed_type = getattr(self.state, "feed_type", "unknown") or "unknown"
            tg.ping_morning_alive(
                underlying_price=bar.close,
                feed_type=feed_type,
                pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
            )
            log.info("Morning alive ping sent for %s", session_key)
        except Exception as e:
            log.warning("ping_morning_alive failed: %s", e)

    def _maybe_ping_midday_status(self, bar: Bar):
        """Fire a midday 'still watching' ping at ~13:00 ET when zero signals
        have fired today. Breaks the Telegram silence — tells the user the
        system is alive and WHY it's quiet (trend filter, stoch zone, etc.).
        Added 2026-05-19 after 'no telegram prompts' feedback."""
        if not getattr(self, "_is_live_bar", False):
            return
        et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        if et.weekday() > 4:
            return
        bar_min = et.hour * 60 + et.minute
        # Window: 13:00-13:10 ET (after prime window closes, before late-day)
        if bar_min < 13 * 60 or bar_min > 13 * 60 + 10:
            return
        session_key = et.strftime("%Y-%m-%d")
        if self._midday_status_pinged == session_key:
            return
        if dedup.already_done("midday_status_pinged", session_key):
            self._midday_status_pinged = session_key
            return
        # Only fire if zero signals today
        today_sigs = [s for s in self._signal_history
                      if s.triggered_at.startswith(session_key)]
        if today_sigs:
            self._midday_status_pinged = session_key
            return
        self._midday_status_pinged = session_key
        dedup.mark_done("midday_status_pinged", session_key)
        # Build explanation of WHY no signals
        ps = self.predictor.current_state()
        trend = ps.get("trend", "flat")
        stoch_k = ps.get("stoch_k")
        rsi = ps.get("rsi")
        reasons = []
        if trend == "down":
            reasons.append("downtrend → put signals blocked by trend filter; "
                           "call signals need stoch > 80 (currently low)")
        elif trend == "up":
            reasons.append("uptrend → call signals blocked by trend filter; "
                           "put signals need stoch < 20 (currently high)")
        else:
            if stoch_k is not None and 20 <= stoch_k <= 80:
                reasons.append("stoch in mid-range (no crossover from extremes yet)")
        in_bo, evt = self.macro.in_blackout_window()
        if in_bo:
            reasons.append("macro blackout active")
        if self._mid_session_volatile:
            reasons.append("mid-session vol spike (entries locked)")
        reason_str = "; ".join(reasons) if reasons else "conditions haven't aligned yet"
        try:
            tg.ping_midday_status(
                underlying_price=bar.close,
                trend=trend,
                rsi=rsi,
                stoch_k=stoch_k,
                regime=ps.get("regime", "unknown"),
                signals_today=0,
                reason_no_signals=reason_str,
                pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
            )
            log.info("Midday status ping sent for %s: %s", session_key, reason_str)
        except Exception as e:
            log.warning("ping_midday_status failed: %s", e)

    # ──────────────────────────────────────────────────────────────
    # Options flow scanner (unusual activity detection via IBKR chain)
    # ──────────────────────────────────────────────────────────────

    # Scan windows: ET times when the scanner fires (once each per session)
    _FLOW_SCAN_WINDOWS = ["10:30", "12:30", "14:30"]

    async def _maybe_scan_flow(self, bar: Bar):
        """Run the options flow scanner at scheduled windows during market hours.
        Requires IBKR connected (real options data). Pushes anomalies to Telegram."""
        if not getattr(self, "_is_live_bar", False):
            return
        if not self.feed.connected:
            return  # need IBKR for chain data

        et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        if et.weekday() > 4:
            return
        bar_min = et.hour * 60 + et.minute
        session_key = et.strftime("%Y-%m-%d")

        # Check if we're in any scan window (±5 min tolerance)
        target_window = None
        for window in self._FLOW_SCAN_WINDOWS:
            h, m = map(int, window.split(":"))
            window_min = h * 60 + m
            if window_min <= bar_min <= window_min + 5:
                dedup_key = f"{session_key}_{window}"
                if dedup_key not in self._flow_scans_fired:
                    target_window = window
                    break

        if target_window is None:
            return

        dedup_key = f"{session_key}_{target_window}"
        self._flow_scans_fired.add(dedup_key)

        try:
            from .flow_scanner import scan_options_flow, format_flow_alert

            result = await scan_options_flow(
                ibkr_feed=self.feed,
                symbol="SPX",
                underlying_price=bar.close,
                strike_pct_band=0.05,  # ±5% of price
            )

            if result is None:
                log.warning("Flow scan at %s returned None", target_window)
                return

            self._last_flow_scan = result.to_dict()

            # Always push scan result to Telegram (even if no anomalies)
            alert_text = format_flow_alert(result, max_anomalies=6)
            tg.send(
                alert_text,
                chat_id=settings.TELEGRAM_GROUP_CHAT_ID,
                message_thread_id=settings.TELEGRAM_TOPIC_ZERO_DTE,
            )
            log.info("Flow scan at %s ET: %d anomalies, P/C %.2f, net Δ %+.0f",
                     target_window, len(result.anomalies),
                     result.put_call_ratio, result.net_delta_flow)

        except Exception as e:
            log.error("Flow scan at %s failed: %s", target_window, e)

    async def _check_ic_stop_loss(self, bar: Bar):
        """If today's IC has been built, check current spread mid-price against
        the original credit collected. If buyback-cost ≥ original credit, fire
        a STOP alert (Theta Profits' Breakeven IC rule). Once-per-IC."""
        if not getattr(self, "_is_live_bar", False):
            return
        ic = self.state.iron_condor
        if not ic or not ic.available or not ic.call_leg or not ic.put_leg:
            return
        if not ic.total_credit_dollars or ic.total_credit_dollars <= 0:
            return
        # Edge-trigger: only fire once per IC build (use build_id + dedup)
        if dedup.already_done("ic_stop_fired", ic.build_id):
            return

        # Fetch current chain to mark IC to market
        instr = ic.call_leg.instrument
        instr_scale = 0.1 if instr in ("XSP", "SPY") else 1.0
        instr_underlying = bar.close * instr_scale
        try:
            chain = await self._get_chain_cached(instr, instr_underlying)
        except Exception as e:
            log.warning("IC stop check: chain fetch failed: %s", e)
            return
        if not chain or "calls" not in chain or "puts" not in chain:
            return

        # Find current mid for each leg
        def _mid(leg_data, target_strike):
            row = next((r for r in leg_data if abs(r["strike"] - target_strike) < 0.01), None)
            if row is None:
                return None
            return row.get("mid")

        sc_mid = _mid(chain["calls"], ic.call_leg.short_strike)
        lc_mid = _mid(chain["calls"], ic.call_leg.long_strike)
        sp_mid = _mid(chain["puts"],  ic.put_leg.short_strike)
        lp_mid = _mid(chain["puts"],  ic.put_leg.long_strike)
        if None in (sc_mid, lc_mid, sp_mid, lp_mid):
            return  # incomplete chain data, retry next bar

        # Mark-to-market spread cost (per-share × multiplier)
        # Buy back call spread: pay (sc_mid − lc_mid). Same for put.
        multiplier = 100
        call_spread_cost = max(0.0, (sc_mid - lc_mid)) * multiplier
        put_spread_cost  = max(0.0, (sp_mid - lp_mid)) * multiplier
        total_buyback = call_spread_cost + put_spread_cost

        # Stop trigger: buyback cost ≥ original credit (= breakeven)
        if total_buyback < ic.total_credit_dollars:
            return

        log.warning(
            "IC STOP triggered: buyback ${%.0f} ≥ credit ${%.0f} (call_cost=${%.0f}, put_cost=${%.0f})",
            total_buyback, ic.total_credit_dollars, call_spread_cost, put_spread_cost,
        )
        dedup.mark_done("ic_stop_fired", ic.build_id)
        try:
            tg.ping_ic_stop(
                underlying_price=instr_underlying,
                instrument=instr,
                short_call=ic.call_leg.short_strike,
                short_put=ic.put_leg.short_strike,
                current_spread_cost=total_buyback,
                original_credit=ic.total_credit_dollars,
                stop_threshold=ic.total_credit_dollars,
                pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
            )
        except Exception as e:
            log.warning("ping_ic_stop failed: %s", e)

    async def _maybe_fire_eod_summary(self, bar: Bar):
        """Fire the EOD wave + IC summary ONCE per session, after 16:00 ET.
        Bar-driven trigger; the safety task in start() also fires this if
        no late bars arrive (e.g. backend booted post-close)."""
        if not getattr(self, "_is_live_bar", False):
            return
        et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        # Only fire once we're past 16:00 ET on a session day
        if et.hour < 16:
            return
        date_str = et.strftime("%Y-%m-%d")
        if dedup.already_done("eod_summary_fired", date_str):
            return
        await self._fire_eod_summary(date_str)

    async def _fire_eod_summary(self, date_str: str):
        """Build + send the EOD summaries to both topics. Idempotent per date.
        Persistent dedup — backend restart won't re-fire today's summary.

        BUG FIX (2026-05-13): dedup.mark_done() now happens AFTER successful
        Telegram send, not before. Previously, if build_eod_summaries() or
        tg.ping_*() threw, the persistent dedup was already set but only the
        in-memory flag was reset — the safety loop retried but _fire_eod_summary
        saw the persistent dedup and returned immediately, permanently blocking
        the retry. Now we also check tg.ping_*() return values (None = failure).
        """
        if dedup.already_done("eod_summary_fired", date_str):
            return
        # In-memory flag to prevent concurrent fires within this process
        self._eod_summary_fired = date_str
        try:
            from .eod_summary import build_eod_summaries
            # Today's bars from predictor's buffer
            buf = list(self.predictor._buffer)
            today_bars = [b for b in buf
                          if b.time.astimezone(ET).strftime("%Y-%m-%d") == date_str]
            today_sigs = [s for s in self._signal_history
                          if s.triggered_at.startswith(date_str)]
            # Pass indicator context for "why no signals" explanation
            ps = self.predictor.current_state()
            wave_msg, ic_msg = build_eod_summaries(
                date_str, today_sigs, today_bars,
                self.state.iron_condor,
                iron_condor_history=self.state.iron_condor_history,
                trend=ps.get("trend"),
                rsi=ps.get("rsi"),
                stoch_k=ps.get("stoch_k"),
                regime=ps.get("regime"),
            )
            # Auto session debrief — append the post-mortem to the wave summary
            # so the nightly Telegram tells you WHAT went wrong, not just the P&L.
            try:
                from . import debrief as _dbf
                _db = _dbf.build_debrief(self.paper_trades, date_str)
                if _db.get("date"):
                    wave_msg = f"{wave_msg}\n\n{_dbf.format_debrief_telegram(_db)}"
            except Exception as e:
                log.warning("EOD debrief render failed: %s", e)
            # Daily reconciliation — flag any recorded trade the broker never saw.
            try:
                from . import reconcile as _rec
                _rc = await _rec.reconcile(self)
                if _rc.get("checked"):
                    wave_msg = f"{wave_msg}\n{_rec.summarize(_rc)}"
            except Exception as e:
                log.warning("EOD reconcile failed: %s", e)
            # Muted == the user WANTS silence, not a transport failure. Mark dedup
            # and return so the safety loop doesn't rebuild + retry every 60s all
            # day (the old behaviour, since both sends return None when muted).
            if tg.is_muted():
                dedup.mark_done("eod_summary_fired", date_str)
                log.info("EOD summary suppressed (muted) for %s — not retrying", date_str)
                return
            # Append the dashboard link so the EOD summaries match every other
            # alert (one tap → the Pages monitor).
            _url = settings.DASHBOARD_PUBLIC_URL
            if _url:
                wave_msg = f"{wave_msg}\n📱 {_url}"
                ic_msg = f"{ic_msg}\n📱 {_url}"
            wave_ok = tg.ping_eod_wave(wave_msg)
            ic_ok = tg.ping_eod_iron_condor(ic_msg)
            if not wave_ok and not ic_ok:
                raise RuntimeError("Both Telegram EOD sends failed (returned None)")
            if not wave_ok:
                log.warning("EOD wave Telegram send failed for %s (IC ok)", date_str)
            if not ic_ok:
                log.warning("EOD IC Telegram send failed for %s (wave ok)", date_str)
            # Mark persistent dedup ONLY after at least one send succeeded
            dedup.mark_done("eod_summary_fired", date_str)
            log.info("EOD summary fired for %s (%d signals, IC=%s, wave_ok=%s, ic_ok=%s)",
                     date_str, len(today_sigs),
                     "yes" if self.state.iron_condor.available else "no",
                     bool(wave_ok), bool(ic_ok))
        except Exception as e:
            log.exception("EOD summary failed: %s", e)
            # Reset both flags so safety loop can retry
            self._eod_summary_fired = None

    # ── Live state assembly ──────────────────────────────────────────────────

    async def _refresh_state(self, last_bar: Bar, new_signals=None):
        ps = self.predictor.current_state()

        self.state.ts = datetime.now(timezone.utc).isoformat()
        self.state.quote = LiveQuote(
            symbol="SPX", last=last_bar.close, timestamp=last_bar.time.isoformat(),
        )

        regime_str = "non_volatile" if ps.get("regime") == "NON-VOLATILE" else (
            "volatile" if ps.get("regime") == "VOLATILE" else "pre_obs"
        )
        _obs_open = ps.get("obs_open")
        _obs_close = ps.get("obs_close")
        _obs_drift_pct = None
        if _obs_open and _obs_close and _obs_open > 0:
            _obs_drift_pct = (_obs_close - _obs_open) / _obs_open * 100.0
        self.state.regime = RegimeState(
            classified=ps.get("regime") in ("NON-VOLATILE", "VOLATILE"),
            regime=regime_str,
            obs_high=ps.get("obs_high"), obs_low=ps.get("obs_low"),
            obs_open=_obs_open, obs_close=_obs_close,
            obs_range=(ps.get("obs_high") - ps.get("obs_low"))
                if ps.get("obs_high") and ps.get("obs_low") else None,
            obs_drift_pct=_obs_drift_pct,
            proj_high=ps.get("proj_high"), proj_low=ps.get("proj_low"),
        )

        # Live indicator values from predictor's current state
        self.state.indicators = IndicatorState(
            rsi=ps.get("rsi"),
            stoch_k=ps.get("stoch_k"),
            stoch_d=ps.get("stoch_d"),
            wvf=ps.get("wvf"),
            wvf_spike=ps.get("wvf_spike", False),
            atr_5m=ps.get("atr_5m"),
            atr_d1=self._daily_atr,
            # Pullback trend filter — gates live signal generation
            trend=ps.get("trend", "flat"),
            ema_fast=ps.get("ema_fast"),
            ema_slow=ps.get("ema_slow"),
            trend_filter_enabled=ps.get("trend_filter_enabled", True),
        )

        if new_signals:
            for sig in new_signals:
                ev = await self._build_signal_event(sig, last_bar)
                # Open paper trade FIRST — gates run inside _open_paper_trade.
                # If any gate fails, pt is None and we skip everything (no history,
                # no alert). The dashboard only shows signals that ACTUALLY fired.
                pt, sizing_note = self._open_paper_trade(ev)
                if pt is None:
                    log.info(
                        "Signal gated (%s): conf=%d/4 side=%s",
                        sizing_note or "no wave_strikes",
                        ev.confluence_score, ev.side,
                    )
                    continue  # gate failed — don't pollute history
                # Passed all gates — add to history
                self._signal_history.append(ev)
                # Legacy 0-contract path (small account, sizing math) — should be
                # rare with Phase 1 gates + floor-at-1 rule, but keep as safety.
                if pt.contracts == 0:
                    log.info("Trade #%d skipped — 0 contracts (small account?)", pt.trade_no)
                    pt.closed = True
                    pt.outcome = "skipped_low_conf"  # type: ignore
                    pt.exit_reason = sizing_note
                    continue
                # Entry ping. When the Alpaca submit task will run (broker=alpaca AND
                # trader present), IT owns the ping so it reflects the REAL result
                # (filled vs rejected) — Telegram == execution. Otherwise (shadow/none,
                # or trader missing) fire the immediate ping here so a signal is never silent.
                submit_owns_ping = (settings.PAPER_BROKER == "alpaca"
                                    and getattr(self, "alpaca_trader", None) is not None)
                if pt is not None and getattr(self, "_is_live_bar", False) \
                   and not submit_owns_ping:
                    try:
                        # Source alert data from the ACTUAL opened trade (pt), not
                        # ev.wave_strikes — directional trades use SPX/30Δ/BS strikes,
                        # which differ from the legacy XSP wave suggestion.
                        is_dir = pt.strategy == "directional_spread"
                        tg.ping_signal(
                            side=pt.side,
                            underlying_price=pt.underlying_at_signal,
                            short_strike=float(pt.short_strike),
                            long_strike=float(pt.long_strike) if pt.long_strike else None,
                            estimated_credit=float(pt.estimated_credit) if pt.estimated_credit else None,
                            confluence_score=ev.confluence_score,
                            confluence_max=4,  # 4 scored quality factors (macro/vix are gates, not scored)
                            confluence_factors=ev.confluence,  # faithful breakdown of the boss's decision
                            trend=self.predictor.current_state().get("trend", "flat"),
                            instrument=pt.instrument,
                            pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                            trade_no=pt.trade_no,
                            contracts=pt.contracts,
                            sizing_note=sizing_note,
                            tp_target=pt.tp_underlying_target,
                            stop_target=pt.stop_underlying_target,
                            strategy=pt.strategy,
                            tp_pct=settings.DIRECTIONAL_TP_TARGET if is_dir else None,
                            short_delta=settings.DIRECTIONAL_SHORT_DELTA if is_dir else None,
                        )
                    except Exception as e:
                        log.warning("entry-ping with mgmt fields failed: %s", e)
        self.state.last_signals = self._signal_history[-20:]
        self.state.open_positions = [t for t in self.paper_trades if not t.closed]
        # Debounced disk persist — survives backend restart mid-session
        self._persist_state()

        # Macro alerts: imminent high-impact events
        in_bo, evt_now = self.macro.in_blackout_window()
        next_evt = self.macro.next_high_impact(within_hours=24.0)
        macro_alerts = []
        if in_bo and evt_now:
            macro_alerts.append({
                "level": "blackout",
                "minutes_until": evt_now.get("_minutes_until"),
                "event": evt_now.get("event"),
                "impact": evt_now.get("impact"),
                "time": evt_now.get("time"),
                "country": evt_now.get("country"),
                "msg": "MACRO BLACKOUT — high-impact event imminent. Consider closing positions.",
            })
            # Edge-triggered Telegram: only ping ONCE on entering blackout, not every poll.
            # Also only on LIVE bars — replay floods otherwise.
            # Persistent dedup so backend restarts don't re-ping a blackout already sent.
            evt_key = f"{evt_now.get('time')}|{evt_now.get('event')}"
            if not dedup.already_done("last_blackout_pinged", evt_key) \
               and getattr(self, "_is_live_bar", False):
                dedup.mark_done("last_blackout_pinged", evt_key)
                try:
                    tg.ping_macro_blackout(
                        event=evt_now.get("event", "high-impact event"),
                        minutes_until=evt_now.get("_minutes_until", 0),
                        pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                    )
                except Exception as e:
                    log.warning("telegram ping_macro_blackout failed: %s", e)
        elif next_evt:
            mins = next_evt.get("_minutes_until", 9999)
            macro_alerts.append({
                "level": "warn" if mins < 60 else "info",
                "minutes_until": mins,
                "event": next_evt.get("event"),
                "impact": next_evt.get("impact"),
                "time": next_evt.get("time"),
                "country": next_evt.get("country"),
                "msg": f"Next high-impact: {next_evt.get('event')} in {mins} min",
            })
        # Note: don't clear the blackout dedup when no event — we want the
        # event_key match to last forever (same key = same event), and a new
        # event will have a different time-string so it gets its own ping anyway.
        self.state.macro_alerts = macro_alerts

        # Edge-triggered: session-open ping once when regime classifies (09:45 ET).
        # Only on LIVE bars — replay would ping for every historical session.
        # Persistent dedup so backend restarts + feed swaps don't re-ping
        # for the same session_date.
        if (regime_str == "non_volatile" or regime_str == "volatile") and \
           ps.get("proj_high") and ps.get("proj_low"):
            session_key = ps.get("session_date")
            if not session_key:
                return  # no session date yet (early boot / data gap) — don't dedup on None
            if not dedup.already_done("last_session_pinged", session_key) \
               and getattr(self, "_is_live_bar", False):
                dedup.mark_done("last_session_pinged", session_key)
                try:
                    tg.ping_session_open(
                        underlying_price=last_bar.close,
                        regime=regime_str,
                        proj_high=ps.get("proj_high"),
                        proj_low=ps.get("proj_low"),
                        pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                    )
                except Exception as e:
                    log.warning("telegram ping_session_open failed: %s", e)
            elif not dedup.already_done("last_session_pinged", session_key):
                # Replay/historical bar — mark as "pinged" so we don't ping
                # later when a live bar arrives for the same session
                dedup.mark_done("last_session_pinged", session_key)

    # ── Strike pricing per signal ────────────────────────────────────────────

    async def _get_chain_cached(self, instrument: str, underlying: float) -> dict:
        """Fetch + cache a chain. TTL = self._chain_ttl_seconds."""
        now = datetime.utcnow()
        last_ts = self._last_chain_ts.get(instrument)
        if last_ts and (now - last_ts).total_seconds() < self._chain_ttl_seconds:
            return self._last_chain.get(instrument, {})
        chain = await self.feed.get_options_chain_with_greeks(instrument, underlying)
        self._last_chain[instrument] = chain
        self._last_chain_ts[instrument] = now
        return chain

    async def _build_strike_suggestions(
        self,
        side: str,
        underlying_price: float,
        projected_boundary: float,
        instruments: list[str],
        modes: list[str],   # list of "wave" / "iron_condor"
    ) -> list[StrikeSuggestion]:
        """Build a flat list of StrikeSuggestions across instruments × modes."""
        out: list[StrikeSuggestion] = []
        for instr in instruments:
            # XSP/SPY chains are at instrument-scale (price/10), SPX at full scale
            instr_price_scale = 0.1 if instr in ("XSP", "SPY") else 1.0
            instr_underlying = underlying_price * instr_price_scale

            chain = {}
            if self.feed.connected:
                chain = await self._get_chain_cached(instr, instr_underlying)
            chain_ok = chain and "calls" in chain and "puts" in chain

            for mode in modes:
                target = WAVE_DELTA if mode == "wave" else IC_DELTA
                if chain_ok:
                    sp = build_strike_pair_from_chain(
                        instrument=instr, side=side, mode=mode,
                        chain=chain, target_delta=target,
                    )
                    if sp is None:
                        sp = fallback_pair_no_chain(
                            instrument=instr, side=side,
                            underlying_price=underlying_price,
                            projected_boundary=projected_boundary,
                        )
                        sp.mode = mode
                else:
                    sp = fallback_pair_no_chain(
                        instrument=instr, side=side,
                        underlying_price=underlying_price,
                        projected_boundary=projected_boundary,
                    )
                    sp.mode = mode
                out.append(_to_pydantic(sp, underlying_price=underlying_price))
        return out

    async def _build_signal_event(self, sig, last_bar) -> SignalEvent:
        boundary = sig.proj_high if sig.side == "sell_call_cs" else sig.proj_low
        instruments = ["XSP", "SPX", "SPY"]
        wave = await self._build_strike_suggestions(
            sig.side, last_bar.close, boundary, instruments, ["wave"],
        )
        ic = await self._build_strike_suggestions(
            sig.side, last_bar.close, boundary, instruments, ["iron_condor"],
        )

        # ── Confluence scoring — Phase 1: VARIABLE-ONLY factors (5 of 5 can vary) ──
        # Removed always-true factors (stoch_reversal, regime_non_volatile) since they
        # were the trigger/gate themselves, inflating the score floor to 2/5.
        # New score range is honest: 0/5 (none align) → 5/5 (all align).
        confluence = {}

        # 1. RSI directional confirmation
        if sig.side == "sell_call_cs":
            confluence["rsi_overbought"] = bool(sig.rsi is not None and sig.rsi > 65)
        else:
            confluence["rsi_oversold"] = bool(sig.rsi is not None and sig.rsi < 35)

        # 2. WVF spike (volatility confirmation)
        confluence["wvf_spike"] = bool(sig.wvf_spike)

        # 3. Macro clear (no high-impact event in blackout window)
        in_bo, _ = self.macro.in_blackout_window()
        confluence["macro_clear"] = not in_bo

        # 4. VIX bucket OK (not in stand-aside bucket)
        vix_ok, _, _ = self._check_vix_bucket_for_wave()
        confluence["vix_bucket_ok"] = vix_ok

        # 5. Near EMA10 (not over-extended — fresh setup vs late entry)
        ps = self.predictor.current_state()
        ema_fast = ps.get("ema_fast")
        if ema_fast is not None and last_bar.close > 0:
            extension_pct = abs(last_bar.close - ema_fast) / last_bar.close * 100.0
            confluence["near_ema10"] = extension_pct < 0.30
        else:
            confluence["near_ema10"] = False

        # 6. Phase 4: Prime time-of-day window (10:30-13:00 ET = canonical sweet spot)
        from datetime import datetime as _dt
        sig_time = _dt.fromisoformat(sig.time.isoformat() if hasattr(sig.time, 'isoformat') else str(sig.time))
        confluence["in_prime_window"] = self._is_prime_window(sig_time)

        # 7. TradingView enrichment — METADATA ONLY (not scored).
        # Logged alongside each signal for post-hoc analysis. After 4-6 weeks
        # of paper trading data, promote to scoring factors if they predict winners.
        # Exception: news_high_impact IS actionable — it's a safety gate (blocks
        # entries during surprise events the static blackout calendar misses).
        tv_data = {}
        if settings.TV_ENRICHMENT_ENABLED:
            try:
                tv_data = await asyncio.get_event_loop().run_in_executor(
                    None, tv_enrichment.enrich_signal,
                    sig.side, last_bar.close, settings.TV_ENRICHMENT_INTERVAL,
                )
                if tv_data.get("tv_available"):
                    # News high-impact overrides macro_clear (safety gate, not scoring)
                    if tv_data.get("news_high_impact"):
                        confluence["macro_clear"] = False
                    log.info("TV enrichment: rec=%s agrees=%s mtf=%.0f%% news_hi=%s (%dms)",
                             tv_data.get("tv_recommendation"),
                             tv_data.get("tv_agrees_with_signal"),
                             (tv_data.get("mtf_alignment", 0) or 0) * 100,
                             tv_data.get("news_high_impact"),
                             tv_data.get("enrichment_ms", 0))
            except Exception as e:
                log.debug("TV enrichment failed (non-fatal): %s", e)

        # Confluence SCORE = ONLY the validated backtest quality factors
        # (rsi-directional, wvf-spike, near-ema, prime-window). macro_clear and
        # vix_bucket_ok are GATES enforced separately in _open_paper_trade (GATE 2/4) —
        # counting them here double-dipped and added ~2 free points, firing ~13x more
        # than the backtest, which scores only these 4. (They stay in the dict for logging.)
        _QUALITY_FACTORS = ("rsi_overbought", "rsi_oversold", "wvf_spike",
                            "near_ema10", "in_prime_window")
        confluence_score = sum(1 for k in _QUALITY_FACTORS if confluence.get(k))

        # ── Telegram push: cross-device alert ──────────────────────────────
        # Pick the best XSP wave suggestion to embed in the push (most users
        # trade XSP for sizing). Fire-and-forget; failure logs but doesn't break.
        # NOTE: entry-side Telegram ping is now fired AFTER paper-trade open
        # in _refresh_state, so it can include sizing + TP/stop targets.

        return SignalEvent(
            side=sig.side,
            triggered_at=sig.time.isoformat(),
            underlying_price=last_bar.close,
            confluence=confluence,
            confluence_score=confluence_score,
            tv_enrichment=tv_data if tv_data else None,
            wave_strikes=wave,
            ic_strikes=ic,
            suggested_strikes=wave + ic,  # backwards compat
        )

    # ── End-of-day iron condor builder ───────────────────────────────────────

    async def _maybe_build_eod_ic(self, bar: Bar):
        """Auto-build the IC once per session at EOD_IC_BUILD_ET (default 09:45 ET).
        On non-volatile classified days only. Stays visible until next session.
        User can override with /icnow to rebuild any time."""
        et_time = bar.time.astimezone(ET)
        date_str = et_time.strftime("%Y-%m-%d")
        # Parse EOD_IC_BUILD_ET into hours/minutes
        try:
            hh, mm = settings.EOD_IC_BUILD_ET.split(":")
            build_h, build_m = int(hh), int(mm)
        except (ValueError, AttributeError):
            build_h, build_m = 9, 45
        build_minute = build_h * 60 + build_m
        bar_minute = et_time.hour * 60 + et_time.minute
        # Window: bar at-or-past the build time, but only fire once per session
        if bar_minute < build_minute:
            return
        if not self.state.regime.classified or self.state.regime.regime != "non_volatile":
            return
        if self._eod_ic_built_today == date_str:
            return  # already built once today

        log.info("EOD IC auto-build (target %s ET) for %s @ ET %s",
                 settings.EOD_IC_BUILD_ET, date_str, et_time.strftime("%H:%M"))
        # Mark as attempted FIRST so a failed build doesn't loop on every bar
        self._eod_ic_built_today = date_str
        try:
            await self._build_eod_iron_condor(bar)
        except Exception as e:
            log.exception("EOD IC build failed: %s", e)

    async def _maybe_build_eod_ic_force(self, bar: Bar):
        """Force-build the IC right now, bypassing the 12:30 ET gate. Used by
        the /icnow Telegram command when the trader wants to deploy early."""
        et_time = bar.time.astimezone(ET)
        date_str = et_time.strftime("%Y-%m-%d")
        if not self.state.regime.classified:
            log.warning("/icnow: regime not classified yet (still pre-obs)")
            return
        if self.state.regime.regime != "non_volatile":
            log.warning("/icnow: regime is %s, IC not recommended (would skip auto-build too)",
                        self.state.regime.regime)
            # Build it anyway — user explicitly asked
        log.info("EOD IC (FORCED via /icnow): building for %s @ ET %s",
                 date_str, et_time.strftime("%H:%M"))
        self._eod_ic_built_today = date_str
        try:
            await self._build_eod_iron_condor(bar)
        except Exception as e:
            log.exception("EOD IC build (forced) failed: %s", e)
            raise

    async def _build_ic_from_cboe(self, bar: Bar, ph: float, pl: float) -> bool:
        """Build the EOD iron condor from the live CBOE chain at a real delta.

        Returns True if it HANDLED the IC (built+alerted, or deliberately skipped
        for thin premium); False if CBOE was unavailable so the caller falls back
        to the legacy geometric picker. This is the fix for the penny-IC bug: real
        delta-based strikes + wider SPX wings + actual credit + a min-credit gate.
        """
        from . import gex as _gex
        from .models import StrikeSuggestion, IronCondorBuilder
        et_now = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time

        chain = await _gex.fetch_chain(settings.GEX_SYMBOL)
        if not chain:
            return False  # CBOE down → caller falls back to geometric
        ic = _gex.pick_iron_condor(
            chain, short_delta=settings.EOD_IC_SHORT_DELTA,
            wing=settings.EOD_IC_WING_DOLLARS, min_dte_date=et_now.strftime("%y%m%d"),
        )
        if not ic.get("ok"):
            log.warning("CBOE IC pick failed (%s) — falling back", ic.get("error"))
            return False

        dlt = int(round(settings.EOD_IC_SHORT_DELTA * 100))
        date_key = et_now.strftime("%Y-%m-%d")

        # ── Min-credit gate: skip thin premium entirely (the whole point) ──
        if ic["credit_pct_of_wing"] < settings.EOD_IC_MIN_CREDIT_PCT:
            note = (f"⚪ IC skipped — best {dlt}Δ/${settings.EOD_IC_WING_DOLLARS:.0f} wing only "
                    f"${ic['total_credit_usd']:.0f} ({ic['credit_pct_of_wing']}% of wing) "
                    f"< {settings.EOD_IC_MIN_CREDIT_PCT:.0f}% min — not worth the risk")
            log.info("EOD IC skipped (thin premium): %.1f%% < %.0f%%",
                     ic["credit_pct_of_wing"], settings.EOD_IC_MIN_CREDIT_PCT)
            self.state.notes.append(note)
            if getattr(self, "_is_live_bar", False) and not dedup.already_done("ic_thin_skip", date_key):
                dedup.mark_done("ic_thin_skip", date_key)
                try:
                    tg.ping_eod_iron_condor(
                        f"🦅 IRON CONDOR · SPX · SKIPPED (premium too thin)\n"
                        f"best {dlt}Δ / ${settings.EOD_IC_WING_DOLLARS:.0f} wing = ${ic['total_credit_usd']:.0f} "
                        f"({ic['credit_pct_of_wing']}% of wing) < {settings.EOD_IC_MIN_CREDIT_PCT:.0f}% min.\n"
                        f"Low IV → 0DTE IC not worth the wing risk today.")
                except Exception as e:
                    log.warning("ic thin-skip ping failed: %s", e)
            return True  # handled (deliberate skip)

        mult = 100
        call_leg = StrikeSuggestion(
            instrument="SPX", side="sell_call_cs", mode="iron_condor",
            short_strike=ic["short_call"], long_strike=ic["long_call"],
            wing_width=ic["call_wing"], multiplier=mult, short_delta=ic["short_call_delta"],
            estimated_credit_dollars=ic["call_credit_usd"],
            max_loss_dollars=ic["call_wing"] * mult - ic["call_credit_usd"],
        )
        put_leg = StrikeSuggestion(
            instrument="SPX", side="sell_put_cs", mode="iron_condor",
            short_strike=ic["short_put"], long_strike=ic["long_put"],
            wing_width=ic["put_wing"], multiplier=mult, short_delta=ic["short_put_delta"],
            estimated_credit_dollars=ic["put_credit_usd"],
            max_loss_dollars=ic["put_wing"] * mult - ic["put_credit_usd"],
        )
        spot = ic["spot"]
        call_pct = (ic["short_call"] / spot - 1) * 100 if spot else 0.0
        put_pct = (1 - ic["short_put"] / spot) * 100 if spot else 0.0
        already_today = any(b.build_id.startswith(f"ic_{date_key}")
                            for b in self.state.iron_condor_history)
        try:
            hh, mm = settings.EOD_IC_BUILD_ET.split(":")
            target_min = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            target_min = 10 * 60 + 15
        in_auto = abs((et_now.hour * 60 + et_now.minute) - target_min) <= 10
        trigger = "auto" if (in_auto and not already_today) else "icnow"

        new_ic = IronCondorBuilder(
            build_id=f"ic_{et_now.strftime('%Y-%m-%d_%H%M')}", built_at=et_now.isoformat(),
            trigger=trigger, available=True, expiry=ic["expiry"], underlying_price=bar.close,
            proj_high=ph, proj_low=pl, call_leg=call_leg, put_leg=put_leg,
            total_credit_dollars=ic["total_credit_usd"], total_max_loss_dollars=ic["max_loss_usd"],
            bpr_estimate_dollars=ic["max_loss_usd"], skew_direction="neutral",
            call_pct_otm=round(call_pct, 3), put_pct_otm=round(put_pct, 3),
            notes=[f"trigger={trigger}; CBOE real-{dlt}Δ ${settings.EOD_IC_WING_DOLLARS:.0f}-wing; "
                   f"credit {ic['credit_pct_of_wing']}% of wing; deploy ~13:00 ET"],
        )
        self.state.iron_condor = new_ic
        self.state.iron_condor_history.append(new_ic)
        if len(self.state.iron_condor_history) > 30:
            self.state.iron_condor_history = self.state.iron_condor_history[-30:]
        log.info("EOD IC (CBOE %dΔ): SC %s/%s SP %s/%s credit=$%.0f (%.0f%% of wing) maxloss=$%.0f",
                 dlt, ic["short_call"], ic["long_call"], ic["short_put"], ic["long_put"],
                 ic["total_credit_usd"], ic["credit_pct_of_wing"], ic["max_loss_usd"])
        # Trade management: TP = buy back at (100−TP_PCT)% of credit (capture TP_PCT%);
        # SL = SL_MULT× credit loss, capped at the wing max loss.
        credit_usd = ic["total_credit_usd"]
        tp_dollars = round(credit_usd * (1 - settings.EOD_IC_TP_PCT / 100.0))
        sl_dollars = round(min(credit_usd * settings.EOD_IC_SL_MULT, ic["max_loss_usd"]))
        if getattr(self, "_is_live_bar", False):
            try:
                tg.ping_iron_condor(
                    expiry=ic["expiry"], underlying_price=bar.close, instrument="SPX",
                    short_call=ic["short_call"], long_call=ic["long_call"],
                    short_put=ic["short_put"], long_put=ic["long_put"],
                    total_credit=ic["total_credit_usd"], max_loss=ic["max_loss_usd"],
                    bpr_estimate=ic["max_loss_usd"], pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                    skew_direction="neutral", obs_drift_pct=self.state.regime.obs_drift_pct or 0.0,
                    call_pct_otm=call_pct, put_pct_otm=put_pct,
                    tp_dollars=tp_dollars, sl_dollars=sl_dollars,
                    tp_pct=settings.EOD_IC_TP_PCT, sl_mult=settings.EOD_IC_SL_MULT,
                )
            except Exception as e:
                log.warning("ping_iron_condor (CBOE) failed: %s", e)
        return True

    async def _build_eod_iron_condor(self, bar: Bar):
        """Build the IC pair (call leg + put leg) using the melded picker:
        geometric (X% OTM, what backtest validated) as floor, delta-targeted
        upgrade when chain Greeks are healthy AND within sanity band.

        Configurable via env:
          IC_INSTRUMENT       — "XSP" (default, small accounts) or "SPX" (Henry's 10pt variant)
          IC_DEFAULT_PCT_OTM  — % OTM for short strikes (default 1.0)
          IC_WING_WIDTH       — long-leg distance from short (0 = instrument default)
        """
        ph = self.state.regime.proj_high
        pl = self.state.regime.proj_low
        if ph is None or pl is None:
            return

        # ── ADAPTIVE %OTM based on current VIX (calibrated 12mo backtest) ──
        # Replaces the old hard VIX_MAX gate with a tiered bucket rule.
        # See scripts/vix_otm_analysis.py for the empirical justification.
        adaptive_pct_otm: float | None = None
        try:
            from .vix_gate import check_iv_safe
            from .adaptive_otm import pick_pct_otm
            # Pull current VIX (failsafe-open if unavailable)
            _, vix_value, source = check_iv_safe(threshold=999.0)
            decision = pick_pct_otm(vix_value)

            log.info("Adaptive OTM: VIX=%s (%s) → %s · %s",
                     f"{vix_value:.1f}" if vix_value else "unknown",
                     source, decision.bucket_label, decision.rationale)

            if decision.pct_otm is None:
                # STAND ASIDE — extreme VIX bucket
                self.state.notes.append(
                    f"⚠️ IC skipped: {decision.bucket_label} — {decision.rationale}"
                )
                date_key = bar.time.astimezone(ET).strftime("%Y-%m-%d")
                if not dedup.already_done("iv_gate_skipped", date_key) \
                   and getattr(self, "_is_live_bar", False):
                    dedup.mark_done("iv_gate_skipped", date_key)
                    try:
                        tg.ping_iv_gate_skip(
                            vix_value=vix_value if vix_value else 0.0,
                            threshold=22.0,
                            pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                        )
                    except Exception as e:
                        log.warning("ping_iv_gate_skip failed: %s", e)
                return

            adaptive_pct_otm = decision.pct_otm
            self.state.notes.append(
                f"📊 IC adaptive: {decision.bucket_label} → {adaptive_pct_otm}% OTM"
            )
        except Exception as e:
            log.warning("adaptive OTM check failed (using static default): %s", e)
            adaptive_pct_otm = settings.IC_DEFAULT_PCT_OTM

        # ── CBOE real-delta IC (fixes the geometric-pennies bug) ──
        # Places shorts at a real delta off the live CBOE chain with wider SPX wings
        # and shows the actual credit; SKIPS when premium is too thin. Falls through
        # to the legacy geometric picker only if CBOE is unavailable.
        if settings.EOD_IC_USE_CBOE:
            handled = await self._build_ic_from_cboe(bar, ph, pl)
            if handled:
                return
            log.info("CBOE IC unavailable — falling back to geometric picker")

        instr = settings.IC_INSTRUMENT or "XSP"
        instr_price_scale = 0.1 if instr in ("XSP", "SPY") else 1.0
        instr_underlying = bar.close * instr_price_scale

        chain = {}
        if self.feed.connected:
            chain = await self._get_chain_cached(instr, instr_underlying)
        chain_ok = chain and "calls" in chain and "puts" in chain
        if chain and "error" in chain:
            log.warning("EOD IC chain fetch error for %s: %s", instr, chain["error"])

        # Wing width — env override or instrument default
        wing_width = settings.IC_WING_WIDTH if settings.IC_WING_WIDTH > 0 \
                     else DEFAULT_WING_WIDTH[instr]

        # MELDED picker: geometric floor, delta upgrade when conditions allow.
        # Works whether chain is present or not — chain just adds quote info.
        # `adaptive_pct_otm` from VIX bucket (set above) overrides the static
        # IC_DEFAULT_PCT_OTM env value.
        ic_pct_otm = adaptive_pct_otm if adaptive_pct_otm is not None else settings.IC_DEFAULT_PCT_OTM
        ic_target_delta = settings.IC_DELTA_TARGET if hasattr(settings, "IC_DELTA_TARGET") else IC_DELTA

        # ── SKEWED IC: asymmetric %OTM based on observation-window drift ──
        # If the first 15-30 min show a directional move, widen the wing on
        # the threatened side (more cushion) and tighten the safe side (more
        # premium collection from the side the market is moving AWAY from).
        #
        # Drift thresholds (% move during obs window):
        #   |drift| > 0.10%  → strong skew: threatened 1.3×, safe 0.8×
        #   |drift| > 0.05%  → mild skew:   threatened 1.15×, safe 0.9×
        #   |drift| ≤ 0.05%  → symmetric (no skew)
        # Softened from 1.5/0.7 — 0DTE mean-reverts often, tight side was
        # getting breached too easily on reversals.
        #
        # Bearish drift (negative) → put side threatened → put wider, call tighter
        # Bullish drift (positive) → call side threatened → call wider, put tighter
        drift_pct = self.state.regime.obs_drift_pct or 0.0
        STRONG_DRIFT = 0.10   # % threshold for strong skew
        MILD_DRIFT   = 0.05   # % threshold for mild skew
        abs_drift = abs(drift_pct)

        if abs_drift > STRONG_DRIFT:
            wide_mult, tight_mult = 1.3, 0.8
            skew_label = "strong"
        elif abs_drift > MILD_DRIFT:
            wide_mult, tight_mult = 1.15, 0.9
            skew_label = "mild"
        else:
            wide_mult, tight_mult = 1.0, 1.0
            skew_label = "none"

        # ── RANGE FLOOR: obs range as minimum OTM ──
        # If the obs window already established a wide range, strikes must be
        # at least that far out. Prevents building inside the day's range.
        obs_range = self.state.regime.obs_range
        obs_open  = self.state.regime.obs_open
        if obs_range and obs_open and obs_open > 0:
            obs_range_pct = obs_range / obs_open * 100.0
            range_floor   = obs_range_pct * 1.2  # 120% of obs range
            if range_floor > ic_pct_otm:
                log.info("IC range floor: obs_range=%.3f%% → floor=%.3f%% (was %.3f%%)",
                         obs_range_pct, range_floor, ic_pct_otm)
                ic_pct_otm = range_floor

        if drift_pct < -MILD_DRIFT:
            # Bearish: market drifting down → put wing needs more cushion
            call_pct_otm = ic_pct_otm * tight_mult
            put_pct_otm  = ic_pct_otm * wide_mult
            skew_direction = "bearish"
        elif drift_pct > MILD_DRIFT:
            # Bullish: market drifting up → call wing needs more cushion
            call_pct_otm = ic_pct_otm * wide_mult
            put_pct_otm  = ic_pct_otm * tight_mult
            skew_direction = "bullish"
        else:
            call_pct_otm = ic_pct_otm
            put_pct_otm  = ic_pct_otm
            skew_direction = "neutral"

        log.info(
            "IC skew: drift=%.3f%% → %s (%s) · call_otm=%.2f%% put_otm=%.2f%%",
            drift_pct, skew_direction, skew_label, call_pct_otm, put_pct_otm,
        )

        call_pair = build_strike_pair_melded(
            instrument=instr, side="sell_call_cs", mode="iron_condor",
            chain=chain if chain_ok else None,
            underlying=instr_underlying,
            default_pct_otm=call_pct_otm,
            target_delta=ic_target_delta,
            wing_width=wing_width,
        )
        put_pair = build_strike_pair_melded(
            instrument=instr, side="sell_put_cs", mode="iron_condor",
            chain=chain if chain_ok else None,
            underlying=instr_underlying,
            default_pct_otm=put_pct_otm,
            target_delta=ic_target_delta,
            wing_width=wing_width,
        )
        # Log which method each leg used (for transparency / debug)
        def _method(p):
            for w in (p.warnings if p else []):
                if w.startswith("strike_method="):
                    return w.split("=", 1)[1]
            return "?"
        log.info(
            "EOD IC built: instrument=%s wing=%.0f call_short=$%.0f(%s) put_short=$%.0f(%s)",
            instr, wing_width,
            call_pair.short_strike if call_pair else 0, _method(call_pair),
            put_pair.short_strike if put_pair else 0, _method(put_pair),
        )

        if call_pair is None or put_pair is None:
            log.warning("EOD IC: failed to build legs (call=%s put=%s)",
                        bool(call_pair), bool(put_pair))
            return

        # SANITY: refuse to publish an IC where any leg is ITM at build time.
        # Compares against the instrument-scaled underlying.
        if call_pair.short_strike <= instr_underlying:
            log.error("EOD IC REJECTED: call short ${:.2f} is at/below underlying ${:.2f} (would be ITM)".format(
                call_pair.short_strike, instr_underlying))
            self.state.notes.append(
                f"⚠️ IC build rejected: call short ${call_pair.short_strike:.2f} ≤ underlying "
                f"${instr_underlying:.2f}. Likely Greeks missing in delayed data; trying again on next bar."
            )
            self._eod_ic_built_today = None  # allow retry on next bar
            return
        if put_pair.short_strike >= instr_underlying:
            log.error("EOD IC REJECTED: put short ${:.2f} is at/above underlying ${:.2f} (would be ITM)".format(
                put_pair.short_strike, instr_underlying))
            self.state.notes.append(
                f"⚠️ IC build rejected: put short ${put_pair.short_strike:.2f} ≥ underlying "
                f"${instr_underlying:.2f}. Likely Greeks missing in delayed data; trying again on next bar."
            )
            self._eod_ic_built_today = None
            return

        total_credit = (call_pair.estimated_credit_dollars or 0.0) + \
                       (put_pair.estimated_credit_dollars or 0.0)
        # IC max-loss = max(call_max_loss, put_max_loss), since price can only breach one wing
        # (BPR ≈ wing × multiplier - total_credit)
        wing = max(call_pair.wing_width, put_pair.wing_width)
        bpr = wing * MULTIPLIERS[instr] - total_credit if total_credit > 0 else None

        # Build a unique ID + classify trigger
        et_now = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        build_id = f"ic_{et_now.strftime('%Y-%m-%d_%H%M')}"
        # Trigger: auto = first build of the day at the configured EOD_IC_BUILD_ET
        # window (±10min). Anything else = manual override via /icnow.
        try:
            hh, mm = settings.EOD_IC_BUILD_ET.split(":")
            target_min = int(hh) * 60 + int(mm)
        except (ValueError, AttributeError):
            target_min = 9 * 60 + 45
        et_minute = et_now.hour * 60 + et_now.minute
        in_auto_window = abs(et_minute - target_min) <= 10
        already_have_today = any(
            b.build_id.startswith(f"ic_{et_now.strftime('%Y-%m-%d')}")
            for b in self.state.iron_condor_history
        )
        trigger = "auto" if (in_auto_window and not already_have_today) else "icnow"

        skew_note = f"skew={skew_direction}" if skew_direction != "neutral" \
                    else "skew=symmetric"
        new_ic = IronCondorBuilder(
            build_id=build_id,
            built_at=et_now.isoformat(),
            trigger=trigger,
            available=True,
            expiry=chain.get("expiry") if chain_ok else None,
            underlying_price=bar.close,
            proj_high=ph, proj_low=pl,
            call_leg=_to_pydantic(call_pair, underlying_price=bar.close),
            put_leg=_to_pydantic(put_pair, underlying_price=bar.close),
            total_credit_dollars=total_credit if total_credit > 0 else None,
            total_max_loss_dollars=wing * MULTIPLIERS[instr] - total_credit
                if total_credit > 0 else None,
            bpr_estimate_dollars=bpr,
            skew_direction=skew_direction,
            obs_drift_pct=round(drift_pct, 3) if drift_pct else None,
            call_pct_otm=round(call_pct_otm, 3),
            put_pct_otm=round(put_pct_otm, 3),
            notes=[f"trigger={trigger}; {skew_note}; deploy ~13:00 ET"],
        )
        self.state.iron_condor = new_ic
        self.state.iron_condor_history.append(new_ic)
        # Keep history bounded (last 30 builds = ~10 trading days × 3 builds/day max)
        if len(self.state.iron_condor_history) > 30:
            self.state.iron_condor_history = self.state.iron_condor_history[-30:]

        # Telegram: fire IC ping ONCE per session when the IC builds successfully.
        # Edge-triggered via _eod_ic_built_today (set above), gated by _is_live_bar
        # so mock-feed replay doesn't spam.
        # FIX 2026-05-12: removed `total_credit > 0` gate. Geometric-fallback builds
        # (no chain data) have credit=None→0, which silently skipped ALL IC pings
        # when options chain was unavailable. The user sees nothing → "no telegram
        # prompts". Now pings always fire; credit shown as "unknown" if unavailable.
        if getattr(self, "_is_live_bar", False):
            try:
                tg.ping_iron_condor(
                    expiry=chain.get("expiry", "") if chain_ok else "",
                    underlying_price=bar.close,
                    instrument=instr,
                    short_call=call_pair.short_strike,
                    long_call=call_pair.long_strike,
                    short_put=put_pair.short_strike,
                    long_put=put_pair.long_strike,
                    total_credit=total_credit if total_credit > 0 else None,
                    max_loss=wing * MULTIPLIERS[instr] - total_credit
                        if total_credit > 0 else None,
                    bpr_estimate=bpr,
                    pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                    skew_direction=skew_direction,
                    obs_drift_pct=drift_pct,
                    call_pct_otm=call_pct_otm,
                    put_pct_otm=put_pct_otm,
                )
            except Exception as e:
                log.warning("telegram ping_iron_condor failed: %s", e)

    # ── Paper trade tracking ─────────────────────────────────────────────────

    def _next_trade_no(self, et_now: datetime) -> int:
        """Sequential per-session counter (#1, #2, ...). Resets each ET date."""
        d = et_now.strftime("%Y-%m-%d")
        if self._trade_seq_date != d:
            self._trade_seq_today = 0
            self._trade_seq_date = d
        self._trade_seq_today += 1
        return self._trade_seq_today

    def _today_realized_pnl(self) -> float:
        """Sum of P&L across today's CLOSED paper trades (signed dollars)."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        today_iso = _dt.now(_ET).strftime("%Y-%m-%d")
        return sum(
            (t.pnl or 0.0) for t in self.paper_trades
            if t.closed and t.fired_at and t.fired_at.startswith(today_iso)
        )

    def _trades_opened_today(self) -> int:
        """Count today's opened paper trades (regardless of close state).
        Used to enforce MAX_TRADES_PER_DAY before opening a new entry."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        today_iso = _dt.now(_ET).strftime("%Y-%m-%d")
        # Exclude trades that were skipped at the gate (outcome="skipped_low_conf")
        return sum(
            1 for t in self.paper_trades
            if t.fired_at and t.fired_at.startswith(today_iso)
            and t.outcome != "skipped_low_conf"
        )

    def _update_mid_session_volatility(self, bar: Bar) -> None:
        """Phase 2: detect mid-session vol spikes and flip the flag.

        Compares rolling 30-min realized range (last 6 5m bars) against expected
        per-30-min slice of D1 ATR (= ATR_D1 / sqrt(78)). If realized > MULT × expected,
        flip the flag so subsequent entries this session are blocked.

        Resets on new session. Once flipped, stays flipped till EOD.
        """
        if not settings.WAVE_MIDSESSION_REGATE:
            return
        et = bar.time.astimezone(ET) if bar.time.tzinfo else bar.time
        date_str = et.strftime("%Y-%m-%d")
        # New session — reset flag
        if self._mid_session_volatile_date != date_str:
            self._mid_session_volatile = False
            self._mid_session_volatile_date = date_str
        if self._mid_session_volatile:
            return  # already flagged today
        # Only check post-obs window, before EOD
        bar_min = et.hour * 60 + et.minute
        if bar_min < (9 * 60 + 45) or bar_min >= (16 * 60):
            return
        # Last 6 5m bars = 30 min window
        bars_30m = list(self.predictor._buffer)[-6:]
        if len(bars_30m) < 6:
            return
        # All bars must be from current session
        bars_30m = [b for b in bars_30m
                    if b.time.astimezone(ET).strftime("%Y-%m-%d") == date_str]
        if len(bars_30m) < 6:
            return
        range_30m = max(b.high for b in bars_30m) - min(b.low for b in bars_30m)
        if self._daily_atr <= 0:
            return
        # Expected 30-min slice of daily ATR. 78 = 5m bars in 6.5h session, so
        # 6 bars = 30 min. Per random-walk scaling, 30m vol ≈ ATR_D1 / sqrt(78/6) = / sqrt(13).
        # Using sqrt(78) is a conservative simpler approximation of "typical 30m".
        import math
        expected_30m = self._daily_atr / math.sqrt(78.0 / 6.0)  # ≈ ATR_D1 / 3.61
        threshold = settings.WAVE_MIDSESSION_VOL_MULT * expected_30m
        if range_30m > threshold:
            log.warning(
                "Phase 2 mid-session vol spike: 30m range %.1f > %.1f (mult=%.1f). "
                "Blocking new wave entries for rest of session %s.",
                range_30m, threshold, settings.WAVE_MIDSESSION_VOL_MULT, date_str,
            )
            self._mid_session_volatile = True

    def _compute_session_vwap(self) -> float | None:
        """Session VWAP from current ET date's bars in predictor buffer.
        Returns None if no bars yet for today's session.

        VWAP = sum(typical_price × volume) / sum(volume) cumulative from session open.
        """
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        today_iso = _dt.now(_ET).strftime("%Y-%m-%d")
        cum_pv = 0.0
        cum_v = 0.0
        for b in list(self.predictor._buffer):
            et = b.time.astimezone(_ET) if b.time.tzinfo else b.time
            if et.strftime("%Y-%m-%d") != today_iso:
                continue
            typical = (b.high + b.low + b.close) / 3.0
            v = max(b.volume or 1, 1)  # 0-volume bars use weight=1
            cum_pv += typical * v
            cum_v += v
        if cum_v <= 0:
            return None
        return cum_pv / cum_v

    def _session_realized_std(self) -> float | None:
        """Per-5m realized vol (stdev of log returns) from today's session bars
        in the predictor buffer. Used by the BS pricing engine for strike
        placement + spread repricing. Lookahead-safe (only bars up to now).
        Returns None if fewer than 5 bars are available yet."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        from . import bs_pricing as _bs
        _ET = _ZI("America/New_York")
        today_iso = _dt.now(_ET).strftime("%Y-%m-%d")
        closes = []
        for b in list(self.predictor._buffer):
            et = b.time.astimezone(_ET) if b.time.tzinfo else b.time
            if et.strftime("%Y-%m-%d") == today_iso:
                closes.append(b.close)
        if len(closes) < 5:
            return None
        return _bs.realized_5m_std(closes)

    def _check_vwap_for_side(self, side: str, current_price: float) -> tuple[bool, float | None, str]:
        """Phase 4 VWAP gate. Returns (passed, vwap_value, rationale).
          SELL CALL passes when price > VWAP (overextended → mean-revert down)
          SELL PUT  passes when price < VWAP (oversold → mean-revert up)
        Failsafe-open: if VWAP unavailable, allow entry.
        """
        if not settings.WAVE_VWAP_GATE_ENABLED:
            return True, None, "VWAP gate disabled"
        vwap = self._compute_session_vwap()
        if vwap is None:
            return True, None, "VWAP not yet computed (failsafe-open)"
        if side == "sell_call_cs":
            ok = current_price > vwap
            return ok, vwap, (
                f"price ${current_price:.2f} > VWAP ${vwap:.2f}" if ok
                else f"price ${current_price:.2f} ≤ VWAP ${vwap:.2f} (against trend)"
            )
        else:
            ok = current_price < vwap
            return ok, vwap, (
                f"price ${current_price:.2f} < VWAP ${vwap:.2f}" if ok
                else f"price ${current_price:.2f} ≥ VWAP ${vwap:.2f} (against trend)"
            )

    def _is_prime_window(self, dt: datetime | None = None) -> bool:
        """Phase 4 prime-window check: signals in 10:30-13:00 ET get bonus confluence."""
        try:
            sh, sm = settings.WAVE_PRIME_WINDOW_START.split(":")
            eh, em = settings.WAVE_PRIME_WINDOW_END.split(":")
            start_min = int(sh) * 60 + int(sm)
            end_min   = int(eh) * 60 + int(em)
        except (ValueError, AttributeError):
            start_min, end_min = 10 * 60 + 30, 13 * 60
        if dt is None:
            dt = datetime.now(ET)
        et = dt.astimezone(ET) if dt.tzinfo else dt
        bar_min = et.hour * 60 + et.minute
        return start_min <= bar_min < end_min

    def _check_vix_bucket_for_wave(self) -> tuple[bool, float | None, str]:
        """Reuse the IC adaptive_otm picker to decide if the VIX bucket allows
        wave entries. If the bucket maps to pct_otm=None ("stand aside"), wave
        also stands aside. Returns (is_allowed, vix_value, rationale).

        Failsafe-open: if VIX fetch fails, allow entries (rather than block all
        signals during a transient outage).
        """
        try:
            from .vix_gate import check_iv_safe
            from .adaptive_otm import pick_pct_otm
            _, vix_value, _ = check_iv_safe(threshold=999.0)  # threshold ignored — we want the value
            decision = pick_pct_otm(vix_value)
            return (decision.pct_otm is not None), vix_value, decision.rationale
        except Exception as e:
            log.warning("Wave VIX check failed (failsafe-open): %s", e)
            return True, None, "VIX check failed — allowing entry"

    def _daily_loss_limit_breached(self) -> tuple[bool, float, float]:
        """Returns (breached, today_pnl, limit_dollars). Breached when net P&L
        for today ≤ -DAILY_LOSS_LIMIT_PCT × ACCOUNT_SIZE_USD."""
        if settings.DAILY_LOSS_LIMIT_PCT <= 0:
            return False, 0.0, 0.0
        limit_dollars = -abs(settings.ACCOUNT_SIZE_USD * settings.DAILY_LOSS_LIMIT_PCT / 100.0)
        today_pnl = self._today_realized_pnl()
        return today_pnl <= limit_dollars, today_pnl, limit_dollars

    def _open_paper_trade(self, ev: SignalEvent) -> tuple[PaperTrade | None, str]:
        """Returns (paper_trade, sizing_note). PaperTrade is None if any gate fails.

        Phase 1 gates (in order):
          1. Confluence ≥ WAVE_MIN_CONFLUENCE_SCORE
          2. Macro blackout (if WAVE_BLACKOUT_BLOCKS_ENTRIES=true)
          3. Daily trade count ≤ MAX_TRADES_PER_DAY
          4. VIX bucket allows entries (not "stand aside" bucket)
          5. Daily loss limit (existing safety)
        """
        if not ev.wave_strikes:
            return None, ""

        # ── GATE 0: Kill switch / trading halt ──
        if settings.TRADING_HALTED:
            return None, "skipped: trading HALTED (kill switch active)"

        # ── GATE 0b: Directional book control (staged, default OFF) ──
        # The validated edge is ~96% PUT-selling; the live call book is counter-trend
        # in an up-drift and statistically unsupported. When suppressed, stand aside
        # on call-spread signals and keep selling puts. See docs/FLAWS.md.
        if settings.DIRECTIONAL_SUPPRESS_CALLS and ev.side == "sell_call_cs":
            log.info("Signal gated [suppress_calls]: call book disabled by config")
            return None, "skipped: call-selling suppressed (DIRECTIONAL_SUPPRESS_CALLS)"

        # ── GATE 0c: Dealer-gamma (GEX) stand-aside (staged, default OFF) ──
        # Strongly-negative GEX = vol-amplified = breach-prone. UNVALIDATED: there is
        # NO historical GEX (CBOE delayed quotes are current-day only), so the
        # breach-rate edge can't be backtested — this stays theory-only until live
        # GEX-stamped trades accumulate enough to measure. See docs/FLAWS.md.
        if settings.GEX_GATING_ENABLED and self._gex is not None \
                and getattr(self._gex, "ok", False) and self._gex.regime == settings.GEX_GATE_REGIME:
            log.info("Signal gated [gex]: standing aside on %s-gamma session", self._gex.regime)
            return None, f"skipped: {self._gex.regime}-GEX stand-aside (GEX_GATING_ENABLED)"

        # ── GATE 1: Confluence threshold ──
        min_score = settings.WAVE_MIN_CONFLUENCE_SCORE
        if ev.confluence_score < min_score:
            log.info(
                "Signal gated [confluence]: %d/4 < threshold %d/4 (factors=%s)",
                ev.confluence_score, min_score,
                {k: v for k, v in ev.confluence.items()},
            )
            return None, f"skipped: confluence {ev.confluence_score}/4 < {min_score}/4 threshold"

        # ── GATE 2: Macro blackout ──
        if settings.WAVE_BLACKOUT_BLOCKS_ENTRIES:
            in_bo, evt = self.macro.in_blackout_window()
            if in_bo:
                evt_name = evt.get("event", "high-impact event") if evt else "macro event"
                mins = evt.get("_minutes_until", 0) if evt else 0
                log.warning("Signal gated [macro blackout]: %s in %d min", evt_name, mins)
                return None, f"skipped: macro blackout ({evt_name})"

        # ── GATE 3: Max trades per day ──
        trades_today = self._trades_opened_today()
        if trades_today >= settings.MAX_TRADES_PER_DAY:
            log.info(
                "Signal gated [max_trades]: %d/%d trades already opened today",
                trades_today, settings.MAX_TRADES_PER_DAY,
            )
            return None, f"skipped: daily cap reached ({trades_today}/{settings.MAX_TRADES_PER_DAY})"

        # ── GATE 3b: Max concurrent OPEN positions (real limit — was never enforced) ──
        open_now = sum(1 for t in self.paper_trades if not t.closed)
        if open_now >= settings.MAX_CONCURRENT_POSITIONS:
            log.info("Signal gated [max_concurrent]: %d/%d positions already open",
                     open_now, settings.MAX_CONCURRENT_POSITIONS)
            return None, f"skipped: {open_now}/{settings.MAX_CONCURRENT_POSITIONS} positions already open"

        # ── GATE 4: VIX bucket ──
        vix_ok, vix_value, vix_rationale = self._check_vix_bucket_for_wave()
        if not vix_ok:
            log.warning("Signal gated [VIX bucket]: %s", vix_rationale)
            return None, f"skipped: VIX bucket — {vix_rationale}"

        # ── GATE 4b (Phase 2): Mid-session vol re-gate ──
        if settings.WAVE_MIDSESSION_REGATE and self._mid_session_volatile:
            log.info("Signal gated [mid-session vol]: locked for session %s",
                     self._mid_session_volatile_date)
            return None, "skipped: mid-session vol spike (session locked)"

        # ── GATE 4c (Phase 4): VWAP alignment ──
        # SELL CALL needs price > VWAP (overextended), SELL PUT needs price < VWAP (oversold).
        # Canonical 0DTE mean-reversion rule — selling against VWAP direction = bad EV.
        vwap_ok, vwap_value, vwap_reason = self._check_vwap_for_side(ev.side, ev.underlying_price)
        if not vwap_ok:
            log.info("Signal gated [VWAP]: %s — %s", ev.side, vwap_reason)
            return None, f"skipped: VWAP misaligned ({vwap_reason})"

        # ── GATE 5: Daily loss limit (existing safety) ──
        breached, today_pnl, limit_dollars = self._daily_loss_limit_breached()
        if breached:
            log.warning(
                "Daily loss limit breached: today P&L $%.0f ≤ limit $%.0f. "
                "Refusing new entry.", today_pnl, limit_dollars,
            )
            # Edge-trigger ping: fire once per day when limit first hits
            if not dedup.already_done("daily_limit_pinged", ev.triggered_at[:10]):
                dedup.mark_done("daily_limit_pinged", ev.triggered_at[:10])
                if getattr(self, "_is_live_bar", False):
                    try:
                        tg.ping_daily_loss_limit(
                            today_pnl=today_pnl,
                            daily_loss_limit_dollars=limit_dollars,
                            pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                        )
                    except Exception as e:
                        log.warning("ping_daily_loss_limit failed: %s", e)
            return None, "skipped: daily loss limit reached"
        # Default to XSP wave-mode for paper-trade tracking
        sp = next((x for x in ev.wave_strikes if x.instrument == "XSP"), ev.wave_strikes[0])
        from datetime import datetime as _dt
        sig_dt = _dt.fromisoformat(ev.triggered_at)
        et_now = sig_dt.astimezone(ET) if sig_dt.tzinfo else sig_dt
        trade_no = self._next_trade_no(et_now)

        # PIVOT (May 2026): route to new DirectionalSpreadManager when enabled.
        # Backtest verdict: DEPLOY (72/100) over 4.4y SPX data, 81% WR, profitable
        # every year 2022-2026. Runs alongside legacy wave_manager during shadow
        # validation; toggle with DIRECTIONAL_SPREAD_ENABLED in .env.
        if settings.DIRECTIONAL_SPREAD_ENABLED:
            # Build directional strikes from entry price + delta target
            from .directional_spread_manager import (
                open_directional_trade, otm_pct_for_delta, credit_dollars_for_delta,
                bs_entry_strikes,
            )
            short_delta = settings.DIRECTIONAL_SHORT_DELTA
            wing_dollars = settings.DIRECTIONAL_WING_DOLLARS
            realized_std = None
            bs_strikes = None
            if settings.DIRECTIONAL_PNL_MODEL == "bs":
                # Realized 5m vol from today's pre-entry session bars (lookahead-safe)
                realized_std = self._session_realized_std()
                if realized_std and realized_std > 0:
                    bs_strikes = bs_entry_strikes(
                        ev.side, ev.underlying_price, realized_std, sig_dt,
                        short_delta, wing_dollars,
                    )
            if bs_strikes is not None:
                short_strike = bs_strikes["short_strike"]
                long_strike = bs_strikes["long_strike"]
                credit_est = bs_strikes["credit_dollars"]
            else:
                # Fallback: legacy proxy strike placement (e.g. too few bars to price)
                realized_std = None  # ensure check_exit uses legacy path consistently
                otm_pct = otm_pct_for_delta(short_delta)
                if ev.side == "sell_call_cs":
                    short_strike = round(ev.underlying_price * (1 + otm_pct / 100.0), 2)
                    long_strike = round(short_strike + wing_dollars, 2)
                else:
                    short_strike = round(ev.underlying_price * (1 - otm_pct / 100.0), 2)
                    long_strike = round(short_strike - wing_dollars, 2)
                credit_est = credit_dollars_for_delta(short_delta, wing_dollars)
            max_loss_est = wing_dollars * 100 - credit_est
            # Use SPX instrument by default for the new strategy (Tastytrade canonical)
            from .models import StrikeSuggestion
            sp_directional = StrikeSuggestion(
                instrument="SPX",
                side=ev.side,
                mode="directional_spread",
                short_strike=short_strike,
                long_strike=long_strike,
                wing_width=wing_dollars,
                multiplier=100,
                estimated_credit_dollars=credit_est,
                max_loss_dollars=max_loss_est,
                notional_per_contract=ev.underlying_price * 100,
            )
            pt, sizing_note = open_directional_trade(ev, sp_directional, trade_no=trade_no,
                                                     short_delta=short_delta,
                                                     realized_std=realized_std)
            pt.proj_high_at_signal = self.state.regime.proj_high
            pt.proj_low_at_signal = self.state.regime.proj_low
            # Stamp dealer-gamma regime (collected for regime→outcome analysis). Sizing
            # only changes when GEX_SIZING_ENABLED — negative GEX = vol-amplified day =
            # worse for short-premium, so cut size; otherwise this is pure logging.
            if self._gex is not None and getattr(self._gex, "ok", False):
                pt.gex_regime = self._gex.regime
                pt.gex_net_ratio = self._gex.net_ratio
                if settings.GEX_SIZING_ENABLED and self._gex.regime == "negative" and pt.contracts > 1:
                    new_ct = max(1, int(pt.contracts * settings.GEX_NEG_SIZE_FACTOR))
                    if new_ct != pt.contracts:
                        log.info("GEX negative regime → trim size %dct → %dct", pt.contracts, new_ct)
                        pt.contracts = new_ct
                        sizing_note += f" | GEX neg ×{settings.GEX_NEG_SIZE_FACTOR}"
            self.paper_trades.append(pt)
            log.info("Paper trade #%d opened [DIRECTIONAL]: %s SPX short=%.2f long=%.2f "
                     "credit=$%.0f wing=$%.0f size=%dct (%s)",
                     trade_no, ev.side, sp_directional.short_strike, sp_directional.long_strike,
                     credit_est, wing_dollars, pt.contracts, sizing_note)
            # Submit to Alpaca paper broker if configured. Mark pending + register the
            # entry task synchronously so the exit path can await it (prevents a
            # same-bar stop from orphaning a not-yet-placed live order).
            if settings.PAPER_BROKER == "alpaca" and hasattr(self, "alpaca_trader") \
               and self.alpaca_trader is not None:
                pt.broker_status = "pending"
                self._broker_entry_tasks[pt.id] = asyncio.create_task(
                    self._submit_alpaca_entry(pt, ev, sizing_note,
                                              getattr(self, "_is_live_bar", False)))
            return pt, sizing_note

        # Legacy wave_manager path
        from .wave_manager import open_wave_trade
        pt, sizing_note = open_wave_trade(ev, sp, trade_no=trade_no, atr_d1=self._daily_atr)
        pt.proj_high_at_signal = self.state.regime.proj_high
        pt.proj_low_at_signal = self.state.regime.proj_low

        self.paper_trades.append(pt)
        log.info("Paper trade #%d opened [WAVE]: %s %s short=%.2f long=%.2f credit=$%.2f size=%dct (%s)",
                 trade_no, ev.side, sp.instrument, sp.short_strike, sp.long_strike,
                 sp.estimated_credit_dollars or 0.0, pt.contracts, sizing_note)
        return pt, sizing_note

    def _dispatch_exit_check(self, bar: Bar):
        """Run the per-bar exit check, optionally gated to CLOSED bars.

        Default: evaluate on every dispatch (current behaviour). With
        EXIT_ON_CLOSED_BAR_ONLY: the live feed re-dispatches the developing 5m bar
        every ~60s; evaluating exits on it can book intra-bar TPs that the
        worst-first backtest would never see. We therefore evaluate exits for a bar
        only once a strictly NEWER bar timestamp arrives — i.e. on the now-CLOSED
        previous bar — mirroring honest_backtest. (Caveat to validate before
        enabling: the final RTH bar's exits defer to the next session unless caught
        by the EOD settlement path.)"""
        if not settings.EXIT_ON_CLOSED_BAR_ONLY:
            self._check_open_wave_trades(bar)
            return
        prev = getattr(self, "_pending_exit_bar", None)
        if prev is not None and getattr(bar, "time", None) and bar.time > prev.time:
            self._check_open_wave_trades(prev)   # previous bar is now closed
        self._pending_exit_bar = bar

    def _check_open_wave_trades(self, bar: Bar):
        """Per-bar exit check across all open paper trades.
        Routes to the correct manager based on trade.strategy.
        Fires Telegram exit alerts (gated by _is_live_bar)."""
        from .wave_manager import check_exit as wave_check_exit
        from .directional_spread_manager import check_exit as directional_check_exit
        open_trades = [t for t in self.paper_trades if not t.closed]
        if not open_trades:
            return
        for pt in open_trades:
            # Route to the manager that owns this trade's strategy
            if pt.strategy == "directional_spread":
                exit_info = directional_check_exit(pt, bar)
            else:
                exit_info = wave_check_exit(pt, bar)
            if exit_info is None:
                continue
            log.info("Trade #%d closed: outcome=%s pnl=%.0f reason=%s",
                     pt.trade_no, pt.outcome, pt.pnl or 0.0, pt.exit_reason)
            self._persist_state()  # capture exit immediately
            # Submit close order to Alpaca paper broker. Gate on "had a broker entry"
            # (pt.id registered), NOT on alpaca_order_id — the entry task may not have
            # set it yet. _submit_alpaca_exit awaits that entry task before deciding.
            if settings.PAPER_BROKER == "alpaca" and pt.id in self._broker_entry_tasks \
               and hasattr(self, "alpaca_trader") and self.alpaca_trader is not None:
                asyncio.create_task(self._submit_alpaca_exit(pt))
            # Telegram exit alert — only on LIVE bars (not mock-feed replay)
            if getattr(self, "_is_live_bar", False):
                try:
                    _is_dir = pt.strategy == "directional_spread"
                    self._tg(
                        tg.ping_signal_exit,
                        trade_no=pt.trade_no,
                        side=pt.side,
                        outcome=pt.outcome or "unknown",
                        underlying_at_close=pt.underlying_at_close or bar.close,
                        underlying_at_signal=pt.underlying_at_signal,
                        short_strike=pt.short_strike,
                        contracts=pt.contracts,
                        pnl=pt.pnl or 0.0,
                        exit_reason=pt.exit_reason or "",
                        pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                        peak_pct_kept=pt.peak_pct_kept if _is_dir else None,
                        tp_pct=settings.DIRECTIONAL_TP_TARGET if _is_dir else None,
                    )
                except Exception as e:
                    log.warning("ping_signal_exit failed: %s", e)

    def _tg(self, fn, *args, **kwargs):
        """Fire a Telegram ping OFF the event loop. send() uses a synchronous
        httpx client; calling it inline on a bar would block bar processing /
        exit management for up to the client timeout on a slow/hung send. We
        run it in a thread (fire-and-forget) so the loop stays free."""
        def _safe():
            try:
                return fn(*args, **kwargs)
            except Exception as e:  # noqa: BLE001
                log.warning("telegram %s failed: %s", getattr(fn, "__name__", fn), e)
        try:
            asyncio.get_running_loop().create_task(asyncio.to_thread(_safe))
        except RuntimeError:
            _safe()  # no running loop (scripts / tests) — call inline

    # ── Alpaca paper broker ──────────────────────────────────────────────────

    async def _submit_alpaca_entry(self, pt: PaperTrade, ev=None,
                                   sizing_note: str | None = None, is_live: bool = False):
        """Submit a directional-spread entry to Alpaca paper (as SPY), then fire the
        Telegram ENTRY ping reflecting the REAL broker result — Telegram == execution.

        A signal that does NOT actually execute (outside market hours, or rejected by
        the broker) is REMOVED from the trade ledger and pinged as "NOT EXECUTED", so
        the Monitor + Telegram only ever show trades that genuinely reached the broker.
        This is what closes the old gap where the ledger logged sim signals as "trades".
        """
        from .directional_spread_manager import spy_strike_params

        def _ping(executed: bool, exec_note: str | None):
            if not (is_live and ev is not None):
                return
            try:
                is_dir = pt.strategy == "directional_spread"
                self._tg(
                    tg.ping_signal,
                    side=pt.side,
                    underlying_price=pt.underlying_at_signal,
                    short_strike=float(pt.short_strike),
                    long_strike=float(pt.long_strike) if pt.long_strike else None,
                    estimated_credit=float(pt.estimated_credit) if pt.estimated_credit else None,
                    confluence_score=ev.confluence_score,
                    confluence_max=4,
                    confluence_factors=ev.confluence,
                    trend=self.predictor.current_state().get("trend", "flat"),
                    instrument=pt.instrument,
                    pwa_url=settings.DASHBOARD_PUBLIC_URL or None,
                    trade_no=pt.trade_no,
                    contracts=pt.contracts,
                    sizing_note=sizing_note,
                    tp_target=pt.tp_underlying_target,
                    stop_target=pt.stop_underlying_target,
                    strategy=pt.strategy,
                    tp_pct=settings.DIRECTIONAL_TP_TARGET if is_dir else None,
                    short_delta=settings.DIRECTIONAL_SHORT_DELTA if is_dir else None,
                    executed=executed,
                    exec_note=exec_note,
                )
            except Exception as e:
                log.warning("entry ping (executed=%s) failed: %s", executed, e)

        def _drop_trade():
            # Never executed → remove from the ledger so Monitor/Telegram stay truthful.
            try:
                self.paper_trades.remove(pt)
            except ValueError:
                pass

        # Market-hours guard: Alpaca rejects options market orders outside RTH. Don't
        # even attempt; report the signal as fired-but-not-executed.
        now_et = datetime.now(ET)
        mod = now_et.hour * 60 + now_et.minute
        in_rth = now_et.weekday() < 5 and (9 * 60 + 30) <= mod < (16 * 60)
        if not in_rth:
            pt.broker_status = "skipped_afterhours"
            _drop_trade()
            log.info("Signal #%d outside RTH — not executed (Alpaca rejects after-hours).", pt.trade_no)
            _ping(executed=False, exec_note="after-hours — market closed, not executed")
            self._persist_state()
            return

        try:
            # Mirror the ACTUAL SPX strikes the strategy placed (1/10 scale).
            params = spy_strike_params(
                side=pt.side,
                spx_short_strike=pt.short_strike,
                spx_credit_dollars=pt.estimated_credit,
            )
            today_str = datetime.now(ET).strftime("%Y-%m-%d")
            result = await self.alpaca_trader.place_credit_spread(
                underlying="SPY",
                expiry=today_str,
                side=params["side_type"],
                short_strike=params["short_strike"],
                long_strike=params["long_strike"],
                qty=pt.contracts,
                # SPY per-share net credit ≈ SPX$credit / 10(scale) / 100(mult).
                # Only used when ALPACA_MARKETABLE_LIMIT is on (scaffold; model-derived).
                limit_credit=(pt.estimated_credit or 0) / 1000.0,
            )
            if result and not result.get("shadow"):
                pt.alpaca_order_id = result.get("id")
                pt.broker_status = "submitted"
                log.info("Alpaca paper entry: trade #%d → order %s (SPY %s %.1f/%.1f)",
                         pt.trade_no, pt.alpaca_order_id,
                         params["side_type"], params["short_strike"], params["long_strike"])
                _ping(executed=True, exec_note="submitted to Alpaca")
                asyncio.create_task(self._capture_fill(pt, pt.alpaca_order_id, "entry"))
            elif result and result.get("shadow"):
                pt.broker_status = "shadow"
                _ping(executed=True, exec_note="shadow (broker disabled)")
            else:
                pt.broker_status = "error"
                _drop_trade()
                log.error("Alpaca paper entry REJECTED for trade #%d", pt.trade_no)
                _ping(executed=False, exec_note="Alpaca rejected the order")
            self._persist_state()
        except Exception as e:
            pt.broker_status = "error"
            _drop_trade()
            log.exception("Alpaca entry failed for trade #%d: %s", pt.trade_no, e)
            _ping(executed=False, exec_note="submit failed (exception)")

    async def _submit_alpaca_exit(self, pt: PaperTrade):
        """Close Alpaca paper position when directional spread exits.

        Awaits the entry task first so we never race it — a same-bar stop can close
        the trade internally before the entry order is even placed. Once the entry
        resolves, if no live order was placed (shadow/error) there's nothing to close.
        """
        from .directional_spread_manager import spy_strike_params
        entry_task = self._broker_entry_tasks.pop(pt.id, None)
        if entry_task is not None:
            try:
                await entry_task
            except Exception:
                pass
        if not pt.alpaca_order_id:
            return  # entry placed no live order — nothing to reverse
        try:
            # Try cancel first (if order still pending)
            cancelled = await self.alpaca_trader.cancel_order(pt.alpaca_order_id)
            if cancelled:
                pt.broker_status = "closed"
                log.info("Alpaca paper exit (cancelled): trade #%d", pt.trade_no)
            else:
                # Order was filled — place reverse spread to close. Same strikes as
                # entry (derived from the trade's SPX short strike) so it reverses cleanly.
                params = spy_strike_params(
                    side=pt.side,
                    spx_short_strike=pt.short_strike,
                    spx_credit_dollars=pt.estimated_credit,
                )
                today_str = datetime.now(ET).strftime("%Y-%m-%d")
                result = await self.alpaca_trader.close_credit_spread(
                    underlying="SPY",
                    expiry=today_str,
                    side=params["side_type"],
                    short_strike=params["short_strike"],
                    long_strike=params["long_strike"],
                    qty=pt.contracts,
                )
                if result and not result.get("shadow"):
                    pt.broker_status = "closed"
                    log.info("Alpaca paper exit (reverse): trade #%d → order %s",
                             pt.trade_no, result.get("id", "?"))
                    asyncio.create_task(self._capture_fill(pt, result.get("id"), "exit"))
                else:
                    pt.broker_status = "close_error"

            self._persist_state()
        except Exception as e:
            pt.broker_status = "close_error"
            log.exception("Alpaca exit failed for trade #%d: %s", pt.trade_no, e)

    async def _capture_fill(self, pt: PaperTrade, order_id, kind: str):
        """Best-effort: read REAL Alpaca fills into broker_realized_* fields.
        Pure instrumentation — NEVER affects a trading decision. This is what turns
        the validation from 'model grading itself' into a real-fill measurement."""
        if not settings.READ_BROKER_FILLS or not order_id or self.alpaca_trader is None:
            return
        try:
            from .alpaca_trader import order_net_cashflow
            await asyncio.sleep(4.0)   # let the fill settle before reading
            order = await self.alpaca_trader.get_order(order_id)
            cf = order_net_cashflow(order)
            if cf is None:
                log.info("fill-read %s: order %s not filled yet (trade #%s)", kind, order_id, pt.trade_no)
                return
            if kind == "entry":
                pt.broker_realized_credit = cf
            else:
                base = pt.broker_realized_credit if pt.broker_realized_credit is not None else 0.0
                pt.broker_realized_pnl = round(base + cf, 2)
            log.info("fill-read %s: trade #%s real cashflow $%.2f | model credit $%.0f model pnl %s",
                     kind, pt.trade_no, cf, pt.estimated_credit or 0, pt.pnl)
            self._persist_state()
        except Exception as e:  # noqa: BLE001
            log.warning("fill-read %s failed for trade #%s: %s", kind, pt.trade_no, e)

    # ── Mock feed fallback ───────────────────────────────────────────────────

    async def _start_mock_feed(self):
        import json
        path = settings.data_dir / "historical" / "SPX_5m_60d.json"
        if not path.exists():
            log.error("Mock feed data file missing: %s", path)
            self.state.backend_status = "error"
            self.state.notes.append(f"Missing mock data file: {path}")
            return
        log.info("MOCK_FEED: loading %s", path)
        raw = json.loads(path.read_text())
        log.info("MOCK_FEED: replaying %d bars at 50ms/bar", len(raw))
        for r in raw:
            bar = Bar(
                time=datetime.fromisoformat(r["datetime"]),
                open=r["open"], high=r["high"], low=r["low"],
                close=r["close"], volume=r.get("volume", 0) or 0,
            )
            await self.handle_bar(bar)
            await asyncio.sleep(0.05)
        log.info("MOCK_FEED: replay complete")
        self.state.notes.append("Mock replay complete")
        await self._broadcast()

    # ── WebSocket plumbing ───────────────────────────────────────────────────

    async def _broadcast(self):
        if not self.subscribers:
            return
        payload = self.state.model_dump_json()
        dead = []
        for ws in list(self.subscribers):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for d in dead:
            self.subscribers.discard(d)

    def add_subscriber(self, ws):
        self.subscribers.add(ws)

    def remove_subscriber(self, ws):
        self.subscribers.discard(ws)
