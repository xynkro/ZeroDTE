"""IBKR live data feed — runs ib_insync in a dedicated worker thread.

ib_insync's asyncio internals conflict with FastAPI/uvicorn's event loop.
The worker-thread pattern isolates them: each loop owns its own thread.
Cross-thread comms via asyncio.run_coroutine_threadsafe() + asyncio.Queue.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Awaitable, Callable, Optional

from ib_insync import IB, Index, Option, Stock

from .config import settings
from .predictor import Bar


log = logging.getLogger(__name__)


class IbkrFeed:
    """ib_insync runs in a worker thread; bars are dispatched back to the main
    asyncio loop via run_coroutine_threadsafe."""

    def __init__(self):
        self._main_loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_loop: Optional[asyncio.AbstractEventLoop] = None
        self._worker_thread: Optional[threading.Thread] = None
        self._ib: Optional[IB] = None
        self._connected = False
        self._on_new_bar: Optional[Callable[[Bar], Awaitable[None]]] = None
        self._spx_contract = None

    @property
    def connected(self) -> bool:
        return self._connected and self._ib is not None and self._ib.isConnected()

    def _start_worker(self) -> None:
        """Spawn the worker thread that owns the ib_insync event loop."""
        ready = threading.Event()

        def runner():
            loop = asyncio.new_event_loop()
            self._worker_loop = loop
            asyncio.set_event_loop(loop)
            self._ib = IB()
            ready.set()
            loop.run_forever()

        self._worker_thread = threading.Thread(
            target=runner, name="ibkr-worker", daemon=True
        )
        self._worker_thread.start()
        ready.wait(timeout=5)

    def _run_in_worker(self, coro):
        """Submit a coroutine to the worker loop; return the concurrent.future."""
        if self._worker_loop is None:
            raise RuntimeError("worker not started")
        return asyncio.run_coroutine_threadsafe(coro, self._worker_loop)

    async def connect(self) -> bool:
        # Capture main loop reference so worker thread can dispatch bars back
        self._main_loop = asyncio.get_running_loop()
        if self._worker_loop is None:
            self._start_worker()

        async def _do_connect():
            await self._ib.connectAsync(
                host=settings.IBKR_HOST,
                port=settings.IBKR_PORT,
                clientId=settings.IBKR_CLIENT_ID,
                timeout=10,
            )

        try:
            fut = self._run_in_worker(_do_connect())
            await asyncio.wrap_future(fut)
            self._connected = True
            log.info("IBKR connected: %s:%d clientId=%d (worker thread)",
                     settings.IBKR_HOST, settings.IBKR_PORT, settings.IBKR_CLIENT_ID)
            return True
        except Exception as e:
            self._connected = False
            log.error("IBKR connect failed: %s", e)
            return False

    async def disconnect(self):
        if self._ib is not None and self._connected:
            try:
                fut = self._run_in_worker(self._ib_disconnect())
                await asyncio.wrap_future(fut)
            except Exception:
                pass
        self._connected = False
        if self._worker_loop is not None:
            self._worker_loop.call_soon_threadsafe(self._worker_loop.stop)

    async def _ib_disconnect(self):
        self._ib.disconnect()

    def on_bar(self, callback: Callable[[Bar], Awaitable[None]]):
        self._on_new_bar = callback

    def _to_bar(self, ib_bar) -> Bar:
        d = ib_bar.date
        if not isinstance(d, datetime):
            d = datetime.fromisoformat(str(d))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        # Predictor expects bar times in America/New_York so session-hour
        # detection (09:30/09:45 ET) matches. Convert here at the boundary.
        d = d.astimezone(ZoneInfo("America/New_York"))
        return Bar(
            time=d, open=float(ib_bar.open), high=float(ib_bar.high),
            low=float(ib_bar.low), close=float(ib_bar.close),
            volume=float(ib_bar.volume),
        )

    def _dispatch_bar_to_main(self, bar: Bar):
        """Called from worker thread; safely schedule on main loop."""
        if self._main_loop is None or self._on_new_bar is None:
            return
        coro = self._on_new_bar(bar)
        asyncio.run_coroutine_threadsafe(coro, self._main_loop)

    async def start_spx_5m(self) -> bool:
        if not self.connected:
            return False

        async def _subscribe():
            contract = Index("SPX", "CBOE")
            await self._ib.qualifyContractsAsync(contract)
            self._spx_contract = contract
            # Use the async variant so we don't try to nest event loops.
            # keepUpToDate=True returns a BarDataList that auto-updates via events.
            bars = await self._ib.reqHistoricalDataAsync(
                contract=contract,
                endDateTime="",
                durationStr="2 D",
                barSizeSetting="5 mins",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
                keepUpToDate=True,
            )
            # Replay warmup history
            for ib_bar in bars:
                self._dispatch_bar_to_main(self._to_bar(ib_bar))
            # Subscribe to live updates (events fire in worker thread)
            def on_update(bars_arg, has_new_bar):
                if not bars_arg:
                    return
                self._dispatch_bar_to_main(self._to_bar(bars_arg[-1]))
            bars.updateEvent += on_update
            log.info("SPX 5m feed live (warmup %d bars)", len(bars))

        fut = self._run_in_worker(_subscribe())
        await asyncio.wrap_future(fut)
        return True

    async def daily_atr(self, symbol: str = "SPX", n: int = 14) -> float | None:
        if not self.connected:
            return None

        async def _fetch():
            contract = Index(symbol, "CBOE")
            await self._ib.qualifyContractsAsync(contract)
            bars = await self._ib.reqHistoricalDataAsync(
                contract=contract,
                endDateTime="",
                durationStr=f"{n + 5} D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=1,
            )
            if not bars or len(bars) < n + 1:
                return None
            trs = []
            for i in range(1, len(bars)):
                b, p = bars[i], bars[i - 1]
                tr = max(b.high - b.low, abs(b.high - p.close), abs(b.low - p.close))
                trs.append(tr)
            atr = sum(trs[:n]) / n
            for tr in trs[n:]:
                atr = (atr * (n - 1) + tr) / n
            return float(atr)

        try:
            fut = self._run_in_worker(_fetch())
            return await asyncio.wrap_future(fut)
        except Exception as e:
            log.error("daily_atr failed: %s", e)
            return None

    # ── Options chain + Greeks ───────────────────────────────────────────────

    @staticmethod
    def _next_0dte_expiry(symbol: str, all_expiries: list[str]) -> str | None:
        """Pick the soonest expiry from the chain definition.
        SPX/SPY have 0DTE every weekday; XSP has Mon/Wed/Fri.
        Format: YYYYMMDD.
        """
        if not all_expiries:
            return None
        today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        # Sort and find the first expiry >= today
        sorted_e = sorted(all_expiries)
        for e in sorted_e:
            if e >= today:
                return e
        return sorted_e[-1] if sorted_e else None

    async def get_options_chain_with_greeks(
        self,
        symbol: str,
        underlying_price: float,
        strike_pct_band: float = 0.025,   # ±2.5% of price
        expiry: str | None = None,
    ) -> dict:
        """Fetch live 0DTE chain near the money with Greeks.

        Returns:
          {
            "expiry": "20260508",
            "underlying_price": 7337.10,
            "calls": [{strike, bid, ask, mid, delta, theta, gamma, vega, iv}, ...],
            "puts":  [{strike, bid, ask, mid, delta, theta, gamma, vega, iv}, ...],
          }
        """
        if not self.connected:
            return {"error": "not_connected"}

        async def _fetch():
            # Identify underlying contract
            if symbol == "SPX":
                underlying = Index("SPX", "CBOE", currency="USD")
            elif symbol == "XSP":
                underlying = Index("XSP", "CBOE", currency="USD")
            elif symbol == "SPY":
                underlying = Stock("SPY", "ARCA", currency="USD")
            else:
                return {"error": f"unknown_symbol:{symbol}"}

            await self._ib.qualifyContractsAsync(underlying)

            # Resolve chain definition (strikes + expiries).
            # For SPX we get TWO chains back: SPX (AM-settled monthlies) and
            # SPXW (PM-settled weeklies). 0DTE traders want SPXW. Pick by tradingClass.
            chains = await self._ib.reqSecDefOptParamsAsync(
                underlyingSymbol=underlying.symbol,
                futFopExchange="",
                underlyingSecType=underlying.secType,
                underlyingConId=underlying.conId,
            )
            if not chains:
                return {"error": "no_chain_definition"}
            # Filter to the SPXW chain when on SPX (has every-weekday expiries)
            if underlying.symbol == "SPX":
                spxw_chains = [c for c in chains if getattr(c, "tradingClass", "") == "SPXW"]
                chain = spxw_chains[0] if spxw_chains else chains[0]
            else:
                chain = chains[0]
            chosen_expiry = expiry or self._next_0dte_expiry(symbol, list(chain.expirations))
            if chosen_expiry is None:
                return {"error": "no_0dte_expiry_available"}

            # Filter strikes to band around price
            band_low  = underlying_price * (1.0 - strike_pct_band)
            band_high = underlying_price * (1.0 + strike_pct_band)
            strikes_in_band = [s for s in chain.strikes if band_low <= s <= band_high]
            if not strikes_in_band:
                return {"error": "no_strikes_in_band", "chosen_expiry": chosen_expiry}

            # SPX has two distinct option chains:
            #   SPX  — AM-settled, monthly Friday expiries only
            #   SPXW — PM-settled, every-weekday expiries (this is what 0DTE traders use)
            # We MUST disambiguate via tradingClass for SPX. XSP and SPY only have
            # one chain each, so no tradingClass needed there.
            trading_class = "SPXW" if underlying.symbol == "SPX" else None

            # Build C + P contracts for each strike
            contracts: list[Option] = []
            for s in strikes_in_band:
                for r in ("C", "P"):
                    opt = Option(
                        symbol=underlying.symbol,
                        lastTradeDateOrContractMonth=chosen_expiry,
                        strike=float(s),
                        right=r,
                        exchange="SMART",
                        currency="USD",
                    )
                    if trading_class:
                        opt.tradingClass = trading_class
                    contracts.append(opt)
            qualified = await self._ib.qualifyContractsAsync(*contracts)
            qualified = [c for c in qualified if c is not None and c.conId]

            # Try real-time first (mode 1). If account has no OPRA subscription,
            # IB returns "market data not subscribed" errors — fall back to
            # delayed (mode 3 = ~15min lag, free, good enough for delta picks).
            # We blanket-set mode 3 here so chain fetch works on accounts without
            # paid options data subscriptions. Greeks are still computed by IB's
            # server-side BS model so delta/theta/etc. arrive even on delayed.
            try:
                self._ib.reqMarketDataType(3)  # 3 = delayed
            except Exception:
                pass

            # Subscribe to market data with extra tick types for after-hours coverage
            # genericTickList="100,101,106" requests OptionVolume, OpenInterest, ImpliedVol
            tickers = []
            for c in qualified:
                t = self._ib.reqMktData(c, "100,101,106", False, False)
                tickers.append((c, t))

            # Wait for ticks/Greeks (model computation usually arrives in 1-3s,
            # delayed data may take a touch longer)
            await asyncio.sleep(4.0)

            calls, puts = [], []
            for c, t in tickers:
                # Greeks: prefer modelGreeks (always populated server-side post-BS)
                g = t.modelGreeks or t.lastGreeks or t.askGreeks or t.bidGreeks
                # Bid/ask: -1 means "no quote available"; treat as None
                bid = float(t.bid) if t.bid and t.bid > 0 else None
                ask = float(t.ask) if t.ask and t.ask > 0 else None
                mid = ((bid + ask) / 2.0) if (bid is not None and ask is not None) else None
                # After-hours fallback: use last trade or close, finally model option price
                last = float(t.last) if t.last and t.last > 0 else None
                close = float(t.close) if t.close and t.close > 0 else None
                model_px = float(g.optPrice) if (g and g.optPrice and g.optPrice > 0) else None
                if mid is None:
                    mid = last or close or model_px
                # Volume + Open Interest (tick types 100, 101 — requested above)
                vol = int(t.volume) if t.volume and t.volume >= 0 else 0
                oi  = int(t.openInterest) if hasattr(t, "openInterest") and t.openInterest and t.openInterest >= 0 else 0
                # Bid/ask sizes (for flow scanner whale detection)
                bid_sz = int(t.bidSize) if hasattr(t, "bidSize") and t.bidSize and t.bidSize > 0 else 0
                ask_sz = int(t.askSize) if hasattr(t, "askSize") and t.askSize and t.askSize > 0 else 0
                row = {
                    "strike": float(c.strike),
                    "bid": bid, "ask": ask, "mid": mid,
                    "bid_size": bid_sz, "ask_size": ask_sz,
                    "last": last, "close": close, "model_price": model_px,
                    "volume": vol, "open_interest": oi,
                    "delta": float(g.delta) if g and g.delta else None,
                    "theta": float(g.theta) if g and g.theta else None,
                    "gamma": float(g.gamma) if g and g.gamma else None,
                    "vega":  float(g.vega) if g and g.vega else None,
                    "iv":    float(g.impliedVol) if g and g.impliedVol else None,
                }
                if c.right == "C":
                    calls.append(row)
                else:
                    puts.append(row)

            # Cancel market data subscriptions to free lines
            for c, t in tickers:
                self._ib.cancelMktData(c)

            calls.sort(key=lambda x: x["strike"])
            puts.sort(key=lambda x: x["strike"])

            return {
                "expiry": chosen_expiry,
                "underlying_price": underlying_price,
                "calls": calls,
                "puts": puts,
                "n_strikes_returned": len(calls),
            }

        try:
            fut = self._run_in_worker(_fetch())
            return await asyncio.wrap_future(fut)
        except Exception as e:
            log.error("get_options_chain_with_greeks(%s) failed: %s", symbol, e)
            return {"error": str(e)}
