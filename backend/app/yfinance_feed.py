"""Fallback bar feed via Yahoo Finance — runs when IBKR/TWS is offline.

Polls SPY 5m bars (free, real-time during market hours, ~15min delay outside).
Scales to SPX (multiplies by 10) so the predictor's logic is unchanged.

Activated by orchestrator when ibkr_feed.connect() fails. Auto-disengages
when IBKR reconnects via the reconnect loop.

LIMITATIONS vs IBKR:
- Bars arrive ~30s-2min after the 5m boundary (Yahoo polling delay)
- Volume can be approximate / NaN on some bars
- Outside RTH (pre/post-market): bars are sparse or stale
- Yahoo throttles aggressive polling — we poll every 60s, well within their TOS

Symbols you can use:
- "SPY"   = SPDR S&P 500 ETF (most reliable, free real-time)
- "^SPX"  = the index itself (free but ~15min delayed for free tier)
- "^GSPC" = same as ^SPX, sometimes more reliable
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import yfinance as yf

from .predictor import Bar


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


YFINANCE_SYMBOL = os.environ.get("YFINANCE_FALLBACK_SYMBOL", "SPY")
YFINANCE_SCALE = 10.0 if YFINANCE_SYMBOL.upper() == "SPY" else 1.0
YFINANCE_POLL_SECONDS = 60


class YFinanceFeed:
    """Polls Yahoo Finance for SPY 5m bars; calls on_bar for each new one.

    Mirrors the public API of IbkrFeed so orchestrator can swap it in:
      .connected     property
      .on_bar(cb)
      .start_spx_5m()
      .daily_atr(...)  optional
    """

    def __init__(self):
        self._connected = False
        self._on_new_bar: Optional[Callable[[Bar], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None
        self._last_bar_ts: Optional[datetime] = None
        self._symbol = YFINANCE_SYMBOL
        self._scale = YFINANCE_SCALE

    @property
    def connected(self) -> bool:
        return self._connected

    def on_bar(self, callback: Callable[[Bar], Awaitable[None]]):
        self._on_new_bar = callback

    async def start_spx_5m(self) -> bool:
        """Start polling Yahoo for 5m bars on the configured symbol.
        Returns True once a successful first fetch completes (warmup loaded)."""
        # First fetch: warmup ~80 bars (about a session's worth)
        warmup = await asyncio.to_thread(self._fetch_bars, "1d", "5m")
        if not warmup:
            log.error("yfinance warmup returned no bars (symbol=%s)", self._symbol)
            return False
        log.info("yfinance feed warmed: %d %s 5m bars", len(warmup), self._symbol)
        for b in warmup:
            await self._dispatch_bar(b)
        self._connected = True
        # Background polling loop
        self._task = asyncio.create_task(self._poll_loop())
        return True

    async def _dispatch_bar(self, b: Bar):
        # Dedupe by timestamp — Yahoo returns the running bar repeatedly until close
        if self._last_bar_ts == b.time:
            # Update intra-bar
            pass
        elif self._last_bar_ts is not None and b.time < self._last_bar_ts:
            # Older than what we already have — skip
            return
        self._last_bar_ts = b.time
        if self._on_new_bar:
            try:
                await self._on_new_bar(b)
            except Exception as e:
                log.warning("on_bar callback error: %s", e)

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(YFINANCE_POLL_SECONDS)
                latest = await asyncio.to_thread(self._fetch_bars, "1d", "5m")
                if not latest:
                    continue
                # Only dispatch bars newer than last seen
                if self._last_bar_ts:
                    new_bars = [b for b in latest if b.time > self._last_bar_ts]
                else:
                    new_bars = latest[-1:]
                # Always re-dispatch the most recent bar to update intra-bar
                if not new_bars and latest:
                    new_bars = [latest[-1]]
                for b in new_bars:
                    await self._dispatch_bar(b)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("yfinance poll error: %s", e)

    def _fetch_bars(self, period: str, interval: str) -> list[Bar]:
        """Synchronous Yahoo Finance fetch (runs in thread executor).
        Scales SPY → SPX equivalent (×10) so downstream predictor logic
        sees the SPX scale it was trained on.
        """
        try:
            df = yf.download(
                self._symbol,
                period=period,
                interval=interval,
                progress=False,
                auto_adjust=False,
                prepost=False,
            )
            if df is None or df.empty:
                return []
            bars: list[Bar] = []
            for idx, row in df.iterrows():
                # Index is timezone-aware (Yahoo returns UTC for SPY 5m)
                ts = idx.to_pydatetime()
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                ts_et = ts.astimezone(ET)
                # Multi-index columns (newer yfinance) — flatten
                def _val(col):
                    v = row[col]
                    try:
                        return float(v.item() if hasattr(v, "item") else v)
                    except (TypeError, ValueError):
                        return float("nan")
                o, h, l, c = _val("Open"), _val("High"), _val("Low"), _val("Close")
                vol = _val("Volume")
                if any(x != x for x in (o, h, l, c)):  # NaN check
                    continue
                bars.append(Bar(
                    time=ts_et,
                    open=o * self._scale,
                    high=h * self._scale,
                    low=l * self._scale,
                    close=c * self._scale,
                    volume=vol if vol == vol else 0,
                ))
            return bars
        except Exception as e:
            log.error("yfinance fetch failed (%s %s %s): %s",
                      self._symbol, period, interval, e)
            return []

    async def daily_atr(self, symbol: str = "SPX", n: int = 14) -> float | None:
        """ATR(14) on D1 from Yahoo. Symbol arg ignored — uses our symbol."""
        try:
            df = await asyncio.to_thread(
                yf.download, self._symbol,
                period=f"{n + 5}d", interval="1d",
                progress=False, auto_adjust=False,
            )
            if df is None or df.empty or len(df) < n + 1:
                return None
            highs = df["High"].values.flatten()
            lows = df["Low"].values.flatten()
            closes = df["Close"].values.flatten()
            trs = []
            for i in range(1, len(highs)):
                tr = max(highs[i] - lows[i],
                         abs(highs[i] - closes[i - 1]),
                         abs(lows[i] - closes[i - 1]))
                trs.append(tr)
            atr = sum(trs[:n]) / n
            for tr in trs[n:]:
                atr = (atr * (n - 1) + tr) / n
            return float(atr) * self._scale
        except Exception as e:
            log.error("yfinance daily_atr failed: %s", e)
            return None

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._connected = False

    # Compat with IbkrFeed methods called by orchestrator
    async def connect(self) -> bool:
        return True  # no-op; "connection" = polling activation

    async def disconnect(self):
        await self.stop()

    async def get_options_chain_with_greeks(self, *args, **kwargs):
        # Not supported via Yahoo — orchestrator falls back to projected-boundary strikes
        return {"error": "no_chain_via_yfinance"}
