"""Alpaca market data feed — pure async REST, zero threads.

Replaces the thread-heavy IBKR + yfinance stack with a clean httpx-based
feed.  Uses SPY bars (IEX free / SIP paid) scaled ×10 to SPX-equivalent
so the predictor's range-based logic works unchanged.

KEY ADVANTAGES:
- Pure async (httpx) — no threads, no socket drops, no leak risk
- Options chain with real bid/ask + Greeks (for strike pricing)
- Same REST client can place orders (see alpaca_trader.py)
- Free IEX feed is real-time during market hours

FEED PRIORITY in orchestrator:
  1. Alpaca (this) — primary, always-on
  2. IBKR — if TWS is running (live chain + execution)
  3. yfinance — last-resort fallback
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone, timedelta
from typing import Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from .config import settings
from .predictor import Bar


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# SPY ×10 ≈ SPX.  If Alpaca adds index data later, switch to direct.
SYMBOL = "SPY"
SCALE = 10.0
POLL_SECONDS = 60  # Alpaca is generous with rate limits; 60s is safe


class AlpacaFeed:
    """Async Alpaca market data feed.

    Public API matches IbkrFeed / YFinanceFeed so orchestrator can swap in:
      .connected          bool property
      .on_bar(callback)   register bar handler
      .start_spx_5m()     start polling; returns True on success
      .stop()             cancel polling loop
      .disconnect()       alias for .stop() (orchestrator shutdown calls this)
      .daily_atr(sym, n)  D1 ATR for the predictor
      .get_options_chain() options chain lookup (bonus vs yfinance)
    """

    def __init__(self):
        self._connected = False
        self._on_new_bar: Optional[Callable[[Bar], Awaitable[None]]] = None
        self._task: Optional[asyncio.Task] = None
        self._last_bar_ts: Optional[datetime] = None
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def connected(self) -> bool:
        return self._connected

    def on_bar(self, callback: Callable[[Bar], Awaitable[None]]):
        self._on_new_bar = callback

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": settings.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers(),
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
        return self._client

    # ──────────────────────────────────────────────────────────────
    # Bar feed
    # ──────────────────────────────────────────────────────────────

    async def start_spx_5m(self) -> bool:
        """Warm up with today's bars, then start polling. Returns True on success."""
        try:
            warmup = await self._fetch_bars(limit=80)
            if not warmup:
                log.error("Alpaca warmup returned no bars for %s", SYMBOL)
                return False
            log.info("Alpaca feed warmed: %d %s 5m bars (scaled ×%.0f → SPX)",
                      len(warmup), SYMBOL, SCALE)
            for b in warmup:
                await self._dispatch_bar(b)
            self._connected = True
            self._task = asyncio.create_task(self._poll_loop())
            return True
        except Exception as e:
            log.error("Alpaca warmup failed: %s", e)
            return False

    async def get_options_chain_with_greeks(
        self, instrument: str, underlying: float
    ) -> dict:
        """Stub — Alpaca free tier doesn't include options chain.
        Returns empty dict so the IC builder falls back to geometric picker."""
        return {}

    async def stop(self):
        """Cancel the polling loop and close HTTP client."""
        self._connected = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    # Compat with IbkrFeed / YFinanceFeed API called by orchestrator.stop()
    async def disconnect(self):
        await self.stop()

    async def _dispatch_bar(self, b: Bar):
        if self._last_bar_ts == b.time:
            pass  # intra-bar update — fall through to re-dispatch
        elif self._last_bar_ts is not None and b.time < self._last_bar_ts:
            return  # older than last seen — skip
        self._last_bar_ts = b.time
        if self._on_new_bar:
            try:
                await self._on_new_bar(b)
            except Exception as e:
                log.warning("Alpaca on_bar callback error: %s", e)

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(POLL_SECONDS)
                latest = await self._fetch_bars(limit=5)
                if not latest:
                    continue
                if self._last_bar_ts:
                    new_bars = [b for b in latest if b.time > self._last_bar_ts]
                else:
                    new_bars = latest[-1:]
                # Always re-dispatch most recent bar for intra-bar updates
                if not new_bars and latest:
                    new_bars = [latest[-1]]
                for b in new_bars:
                    await self._dispatch_bar(b)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Alpaca poll error: %s", e)

    async def _fetch_bars(self, limit: int = 80) -> list[Bar]:
        """Fetch SPY 5m bars from Alpaca, scale to SPX-equivalent.

        BUG FIX (2026-05-13): For small-limit poll calls, start from the last
        known bar time (minus small buffer) instead of yesterday 09:30.
        Alpaca returns bars in ascending order from `start`, so limit=5 with
        start=yesterday would return the *first* 5 bars of yesterday — never
        reaching today's session. The poll loop was stuck getting stale bars.
        """
        try:
            client = await self._ensure_client()
            if self._last_bar_ts and limit <= 10:
                # Polling: start from near the last known bar to get recent data
                poll_start = self._last_bar_ts - timedelta(minutes=10)
                start = poll_start.isoformat()
            else:
                # Warmup: fetch today + yesterday for full coverage
                start = (datetime.now(ET) - timedelta(days=1)).replace(
                    hour=9, minute=30, second=0, microsecond=0
                ).isoformat()
            url = f"{settings.ALPACA_DATA_URL}/v2/stocks/{SYMBOL}/bars"
            params = {
                "timeframe": "5Min",
                "limit": str(limit),
                "start": start,
                "feed": settings.ALPACA_FEED,
                "adjustment": "raw",
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            bars_raw = data.get("bars", [])
            if not bars_raw:
                return []

            bars: list[Bar] = []
            for b in bars_raw:
                ts = datetime.fromisoformat(b["t"].replace("Z", "+00:00"))
                ts_et = ts.astimezone(ET)
                o, h, l, c = b["o"], b["h"], b["l"], b["c"]
                vol = b.get("v", 0)
                if any(math.isnan(x) if isinstance(x, float) else False
                       for x in (o, h, l, c)):
                    continue
                bars.append(Bar(
                    time=ts_et,
                    open=o * SCALE,
                    high=h * SCALE,
                    low=l * SCALE,
                    close=c * SCALE,
                    volume=float(vol),
                ))
            return bars
        except httpx.HTTPStatusError as e:
            log.error("Alpaca bars HTTP %d: %s", e.response.status_code,
                      e.response.text[:200])
            return []
        except Exception as e:
            log.error("Alpaca bars fetch failed: %s", e)
            return []

    # ──────────────────────────────────────────────────────────────
    # Daily ATR (for predictor + vol-scaling)
    # ──────────────────────────────────────────────────────────────

    async def daily_atr(self, symbol: str = "SPX", n: int = 14) -> float | None:
        """Compute ATR(n) on daily bars from Alpaca."""
        try:
            client = await self._ensure_client()
            start = (datetime.now(ET) - timedelta(days=n + 10)).strftime("%Y-%m-%d")
            url = f"{settings.ALPACA_DATA_URL}/v2/stocks/{SYMBOL}/bars"
            params = {
                "timeframe": "1Day",
                "limit": str(n + 5),
                "start": start,
                "feed": settings.ALPACA_FEED,
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("bars", [])
            if len(daily) < n + 1:
                return None

            # Compute ATR(n) manually
            trs: list[float] = []
            for i in range(1, len(daily)):
                h, l, pc = daily[i]["h"], daily[i]["l"], daily[i - 1]["c"]
                tr = max(h - l, abs(h - pc), abs(l - pc))
                trs.append(tr * SCALE)  # scale to SPX

            if len(trs) < n:
                return None
            atr = sum(trs[-n:]) / n
            return atr
        except Exception as e:
            log.warning("Alpaca daily_atr failed: %s", e)
            return None

    # ──────────────────────────────────────────────────────────────
    # Options chain (unique to Alpaca — IBKR/yfinance can't do this via REST)
    # ──────────────────────────────────────────────────────────────

    async def get_options_chain(
        self,
        underlying: str = "SPY",
        expiry: str | None = None,
        option_type: str = "call",
        strike_min: float | None = None,
        strike_max: float | None = None,
        limit: int = 20,
    ) -> list[dict]:
        """Fetch available options contracts from Alpaca.

        Returns list of contract dicts with symbol, strike, open_interest, etc.
        Use get_option_quote() for live bid/ask on specific contracts.
        """
        try:
            client = await self._ensure_client()
            if expiry is None:
                expiry = datetime.now(ET).strftime("%Y-%m-%d")

            url = f"{settings.ALPACA_BASE_URL}/v2/options/contracts"
            params = {
                "underlying_symbols": underlying,
                "expiration_date": expiry,
                "type": option_type,
                "limit": str(limit),
                "status": "active",
            }
            if strike_min is not None:
                params["strike_price_gte"] = str(strike_min)
            if strike_max is not None:
                params["strike_price_lte"] = str(strike_max)

            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            return data.get("option_contracts", [])
        except Exception as e:
            log.warning("Alpaca options chain failed: %s", e)
            return []

    async def get_option_quote(self, option_symbol: str) -> dict | None:
        """Get live snapshot (bid/ask/last) for a single option contract.

        option_symbol format: SPY260511C00740000
        Returns dict with 'bid', 'ask', 'last', 'volume', etc.
        """
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_DATA_URL}/v1beta1/options/snapshots"
            params = {
                "symbols": option_symbol,
                "feed": "indicative",
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            snap = data.get("snapshots", {}).get(option_symbol)
            if not snap:
                return None

            quote = snap.get("latestQuote", {})
            trade = snap.get("latestTrade", {})
            greeks = snap.get("greeks", {})
            return {
                "symbol": option_symbol,
                "bid": quote.get("bp"),
                "ask": quote.get("ap"),
                "bid_size": quote.get("bs"),
                "ask_size": quote.get("as"),
                "last": trade.get("p"),
                "last_size": trade.get("s"),
                "volume": snap.get("dailyBar", {}).get("v"),
                "delta": greeks.get("delta"),
                "gamma": greeks.get("gamma"),
                "theta": greeks.get("theta"),
                "iv": greeks.get("implied_volatility"),
            }
        except Exception as e:
            log.warning("Alpaca option quote failed for %s: %s", option_symbol, e)
            return None

    async def get_option_quotes_bulk(
        self,
        option_symbols: list[str],
    ) -> dict[str, dict]:
        """Batch-fetch snapshots for multiple option symbols."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_DATA_URL}/v1beta1/options/snapshots"
            params = {
                "symbols": ",".join(option_symbols),
                "feed": "indicative",
            }
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            data = resp.json()
            result = {}
            for sym, snap in data.get("snapshots", {}).items():
                quote = snap.get("latestQuote", {})
                trade = snap.get("latestTrade", {})
                greeks = snap.get("greeks", {})
                result[sym] = {
                    "symbol": sym,
                    "bid": quote.get("bp"),
                    "ask": quote.get("ap"),
                    "last": trade.get("p"),
                    "volume": snap.get("dailyBar", {}).get("v"),
                    "delta": greeks.get("delta"),
                    "theta": greeks.get("theta"),
                    "iv": greeks.get("implied_volatility"),
                }
            return result
        except Exception as e:
            log.warning("Alpaca bulk option quotes failed: %s", e)
            return {}

    # ──────────────────────────────────────────────────────────────
    # Account info (for position sizing, buying power checks)
    # ──────────────────────────────────────────────────────────────

    async def get_account(self) -> dict | None:
        """Fetch Alpaca account summary (buying power, equity, etc.)."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/account"
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("Alpaca account fetch failed: %s", e)
            return None

    async def get_positions(self) -> list[dict]:
        """Fetch current open positions."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/positions"
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning("Alpaca positions fetch failed: %s", e)
            return []
