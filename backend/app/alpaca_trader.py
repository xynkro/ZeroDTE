"""Alpaca order execution — place credit spreads and iron condors.

This is the EXECUTION layer.  The orchestrator decides WHAT to trade;
this module handles HOW to place and manage the orders on Alpaca.

Alpaca options order types:
  - Single leg: simple buy/sell
  - Multi-leg (spreads): vertical spreads, iron condors via legs[]
  - Options level 3 required for spreads (user has this ✅)

SAFETY:
  - All orders go through Alpaca paper first (ALPACA_BASE_URL = paper-api)
  - Orchestrator's TRADING_ENABLED flag must be True
  - Max loss checks happen BEFORE order submission
  - Every order is logged to Telegram

For 0DTE SPY vertical spreads:
  - SELL credit spread = sell short strike + buy long strike (same expiry)
  - Iron condor = sell OTM call spread + sell OTM put spread
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

from .config import settings


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


class AlpacaTrader:
    """Async order execution via Alpaca REST API.

    Designed for 0DTE SPY credit spreads and iron condors.
    All methods are async (httpx) — no threads.
    """

    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    def _headers(self) -> dict:
        return {
            "APCA-API-KEY-ID": settings.ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": settings.ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                headers=self._headers(),
                timeout=httpx.Timeout(15.0, connect=10.0),
            )
        return self._client

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # ──────────────────────────────────────────────────────────────
    # Credit spread (single-side wave trade)
    # ──────────────────────────────────────────────────────────────

    async def place_credit_spread(
        self,
        underlying: str,
        expiry: str,           # "2026-05-11"
        side: str,             # "call" or "put"
        short_strike: float,
        long_strike: float,
        qty: int = 1,
        order_class: str = "oto",  # one-triggers-other
        limit_credit: float | None = None,  # per-share net credit (model mid) for marketable-limit
    ) -> dict | None:
        """Place a vertical credit spread.

        For SELL CALL credit spread:
          - Sell short_strike call (collect premium)
          - Buy long_strike call (cap risk), long > short

        For SELL PUT credit spread:
          - Sell short_strike put (collect premium)
          - Buy long_strike put (cap risk), long < short

        Returns the order response or None on failure.
        """
        if not settings.TRADING_ENABLED:
            log.warning("TRADING_ENABLED=false — credit spread NOT placed (shadow mode)")
            return {"shadow": True, "would_place": {
                "underlying": underlying, "side": side,
                "short": short_strike, "long": long_strike, "qty": qty,
            }}

        try:
            client = await self._ensure_client()
            expiry_fmt = expiry.replace("-", "")  # "20260511"

            # Build OCC-style symbols: SPY260511C00740000
            short_sym = self._occ_symbol(underlying, expiry_fmt, side, short_strike)
            long_sym = self._occ_symbol(underlying, expiry_fmt, side, long_strike)

            # Multi-leg (mleg) order. Alpaca rejects a top-level `symbol` and per-leg
            # `qty`/`type` on mleg orders ("symbol is not allowed for mleg order").
            # Top-level qty = number of spreads; legs use ratio_qty + position_intent.
            order = {
                "qty": str(qty),
                "type": "market",        # market for immediate fill
                "time_in_force": "day",
                "order_class": "mleg",   # multi-leg
                "legs": [
                    {"symbol": short_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                    {"symbol": long_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_open"},
                ],
            }
            # Marketable-limit (staged, default OFF): a net-credit limit 1 tick below
            # the model mid — fills like a market order but caps adverse slippage.
            # SCAFFOLD: limit_credit comes from the BS model (no live NBBO feed), so
            # the real benefit needs a genuine option-quote source first.
            if settings.ALPACA_MARKETABLE_LIMIT and limit_credit and limit_credit > 0.02:
                order["type"] = "limit"
                order["limit_price"] = str(round(max(0.01, limit_credit - 0.01), 2))

            url = f"{settings.ALPACA_BASE_URL}/v2/orders"
            resp = await client.post(url, json=order)
            resp.raise_for_status()
            result = resp.json()
            log.info("Credit spread placed: %s %s %s/%s ×%d → order %s",
                     underlying, side, short_strike, long_strike, qty,
                     result.get("id", "?"))
            return result

        except httpx.HTTPStatusError as e:
            log.error("Alpaca order HTTP %d: %s",
                      e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            log.error("Alpaca credit spread failed: %s", e)
            return None

    # ──────────────────────────────────────────────────────────────
    # Iron Condor (both sides)
    # ──────────────────────────────────────────────────────────────

    async def place_iron_condor(
        self,
        underlying: str,
        expiry: str,
        call_short: float,
        call_long: float,
        put_short: float,
        put_long: float,
        qty: int = 1,
    ) -> dict | None:
        """Place a 4-leg iron condor.

        Legs:
          1. Sell call @ call_short (collect premium)
          2. Buy call @ call_long  (cap upside risk)
          3. Sell put  @ put_short  (collect premium)
          4. Buy put  @ put_long   (cap downside risk)
        """
        if not settings.TRADING_ENABLED:
            log.warning("TRADING_ENABLED=false — IC NOT placed (shadow mode)")
            return {"shadow": True, "would_place": {
                "underlying": underlying,
                "call_short": call_short, "call_long": call_long,
                "put_short": put_short, "put_long": put_long,
                "qty": qty,
            }}

        try:
            client = await self._ensure_client()
            expiry_fmt = expiry.replace("-", "")

            cs = self._occ_symbol(underlying, expiry_fmt, "call", call_short)
            cl = self._occ_symbol(underlying, expiry_fmt, "call", call_long)
            ps = self._occ_symbol(underlying, expiry_fmt, "put", put_short)
            pl = self._occ_symbol(underlying, expiry_fmt, "put", put_long)

            order = {
                "qty": str(qty),
                "type": "market",
                "time_in_force": "day",
                "order_class": "mleg",
                "legs": [
                    {"symbol": cs, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                    {"symbol": cl, "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
                    {"symbol": ps, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                    {"symbol": pl, "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
                ],
            }

            url = f"{settings.ALPACA_BASE_URL}/v2/orders"
            resp = await client.post(url, json=order)
            resp.raise_for_status()
            result = resp.json()
            log.info("Iron condor placed: %s C%s/%s P%s/%s ×%d → order %s",
                     underlying, call_short, call_long, put_short, put_long,
                     qty, result.get("id", "?"))
            return result

        except httpx.HTTPStatusError as e:
            log.error("Alpaca IC order HTTP %d: %s",
                      e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            log.error("Alpaca IC order failed: %s", e)
            return None

    async def place_iron_condor_limit_ladder(
        self,
        underlying: str,
        expiry: str,
        call_short: float, call_long: float,
        put_short: float,  put_long: float,
        mid_credit_per_share: float,    # CBOE-mid net credit per share (target $/SPY)
        qty: int = 1,
        tick_give_cents: int = 1,
        ladder_steps_cents: list[int] | None = None,
        wait_sec: int = 8,
    ) -> dict | None:
        """LIVE marketable-limit IC execution with a reprice ladder.

        Scaffold — NOT invoked until shadow proves out. The orchestrator picks
        market vs ladder via IC_LIMIT_LIVE_ENABLED; this function does not check
        the flag itself so it stays unit-testable.

        Sequence per rung:
          1. submit mleg limit at (mid − give − step) cents
          2. poll for `wait_sec`s; if filled → return
          3. cancel and reprice to the next rung

        After all rungs exhaust, submit a MARKET mleg as final fallback. We
        cancel any stale limit before each new submit so we never have two
        condor entries chasing each other (the leg-risk failure mode the user
        flagged). Alpaca treats mleg as atomic — partial leg-fills are not
        expected — but we defend in depth by checking status before reprice.
        """
        steps = list(ladder_steps_cents or [0])
        if not steps:
            steps = [0]

        for rung_idx, extra_cents in enumerate(steps):
            give = (tick_give_cents + extra_cents) / 100.0
            limit_price = round(max(0.01, mid_credit_per_share - give), 2)
            log.info("IC limit-ladder rung %d/%d: limit $%.2f (mid %.3f − give %.2f)",
                     rung_idx + 1, len(steps), limit_price, mid_credit_per_share, give)
            result = await self._submit_ic_limit(
                underlying, expiry,
                call_short, call_long, put_short, put_long,
                qty=qty, limit_price=limit_price,
            )
            if not result or result.get("shadow"):
                return result
            order_id = result.get("id")
            if not order_id:
                continue
            filled = await self._await_fill(order_id, wait_sec=wait_sec)
            if filled and filled.get("status") == "filled":
                log.info("IC limit-ladder rung %d FILLED at $%.2f → order %s",
                         rung_idx + 1, limit_price, order_id)
                return filled
            # Cancel before reprice — defend against any partial leg-fill leaving
            # naked risk (Alpaca says mleg is atomic, but we don't trust on faith).
            await self.cancel_order(order_id)
            status = (filled or {}).get("status") or "unknown"
            log.warning("IC limit-ladder rung %d unfilled (status=%s) — repricing",
                        rung_idx + 1, status)

        log.warning("IC limit-ladder exhausted — falling back to MARKET mleg")
        return await self.place_iron_condor(
            underlying, expiry,
            call_short=call_short, call_long=call_long,
            put_short=put_short,   put_long=put_long,
            qty=qty,
        )

    async def _submit_ic_limit(
        self,
        underlying: str, expiry: str,
        call_short: float, call_long: float,
        put_short: float,  put_long: float,
        qty: int, limit_price: float,
    ) -> dict | None:
        """Submit a 4-leg mleg LIMIT order. Internal helper for the ladder."""
        if not settings.TRADING_ENABLED:
            return {"shadow": True, "would_place": {
                "underlying": underlying, "limit_price": limit_price,
                "call_short": call_short, "call_long": call_long,
                "put_short": put_short, "put_long": put_long, "qty": qty,
            }}
        try:
            client = await self._ensure_client()
            expiry_fmt = expiry.replace("-", "")
            cs = self._occ_symbol(underlying, expiry_fmt, "call", call_short)
            cl = self._occ_symbol(underlying, expiry_fmt, "call", call_long)
            ps = self._occ_symbol(underlying, expiry_fmt, "put", put_short)
            pl = self._occ_symbol(underlying, expiry_fmt, "put", put_long)
            order = {
                "qty": str(qty),
                "type": "limit",
                "limit_price": f"{limit_price:.2f}",
                "time_in_force": "day",
                "order_class": "mleg",
                "legs": [
                    {"symbol": cs, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                    {"symbol": cl, "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
                    {"symbol": ps, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_open"},
                    {"symbol": pl, "ratio_qty": "1", "side": "buy",  "position_intent": "buy_to_open"},
                ],
            }
            url = f"{settings.ALPACA_BASE_URL}/v2/orders"
            resp = await client.post(url, json=order)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            log.error("IC limit submit HTTP %d: %s",
                      e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:  # noqa: BLE001
            log.error("IC limit submit failed: %s", e)
            return None

    async def _await_fill(self, order_id: str, wait_sec: int = 8,
                          poll_interval: float = 1.0) -> dict | None:
        """Poll a single order until status is terminal or wait_sec elapses."""
        deadline = wait_sec
        elapsed = 0.0
        order = None
        while elapsed < deadline:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            order = await self.get_order(order_id)
            status = (order or {}).get("status")
            if status in ("filled", "canceled", "rejected", "expired"):
                return order
        return order

    # ──────────────────────────────────────────────────────────────
    # Close credit spread (reverse order)
    # ──────────────────────────────────────────────────────────────

    async def close_credit_spread(
        self,
        underlying: str,
        expiry: str,
        side: str,
        short_strike: float,
        long_strike: float,
        qty: int = 1,
    ) -> dict | None:
        """Close a credit spread by placing the reverse multi-leg order."""
        if not settings.TRADING_ENABLED:
            log.warning("TRADING_ENABLED=false — close NOT placed (shadow mode)")
            return {"shadow": True}

        try:
            client = await self._ensure_client()
            expiry_fmt = expiry.replace("-", "")
            short_sym = self._occ_symbol(underlying, expiry_fmt, side, short_strike)
            long_sym = self._occ_symbol(underlying, expiry_fmt, side, long_strike)

            # Reverse the spread to close: buy back the short, sell the long.
            order = {
                "qty": str(qty),
                "type": "market",
                "time_in_force": "day",
                "order_class": "mleg",
                "legs": [
                    {"symbol": short_sym, "ratio_qty": "1", "side": "buy", "position_intent": "buy_to_close"},
                    {"symbol": long_sym, "ratio_qty": "1", "side": "sell", "position_intent": "sell_to_close"},
                ],
            }

            url = f"{settings.ALPACA_BASE_URL}/v2/orders"
            resp = await client.post(url, json=order)
            resp.raise_for_status()
            result = resp.json()
            log.info("Credit spread closed: %s %s %s/%s x%d → order %s",
                     underlying, side, short_strike, long_strike, qty,
                     result.get("id", "?"))
            return result

        except httpx.HTTPStatusError as e:
            log.error("Alpaca close HTTP %d: %s",
                      e.response.status_code, e.response.text[:300])
            return None
        except Exception as e:
            log.error("Alpaca close credit spread failed: %s", e)
            return None

    # ──────────────────────────────────────────────────────────────
    # Close position / cancel order
    # ──────────────────────────────────────────────────────────────

    async def close_position(self, symbol: str) -> dict | None:
        """Close an open position by symbol."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/positions/{symbol}"
            resp = await client.delete(url)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("Alpaca close position failed for %s: %s", symbol, e)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/orders/{order_id}"
            resp = await client.delete(url)
            return resp.status_code in (200, 204)
        except Exception as e:
            log.error("Alpaca cancel order failed for %s: %s", order_id, e)
            return False

    async def get_orders(self, status: str = "open") -> list[dict]:
        """List orders by status (open, closed, all)."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/orders"
            params = {"status": status, "limit": "50"}
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.error("Alpaca list orders failed: %s", e)
            return []

    async def get_order(self, order_id: str) -> dict | None:
        """Fetch a single order WITH its legs (nested) — for reading real fills."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/orders/{order_id}"
            resp = await client.get(url, params={"nested": "true"})
            resp.raise_for_status()
            return resp.json()
        except Exception as e:  # noqa: BLE001
            log.warning("Alpaca get_order %s failed: %s", order_id, e)
            return None


    async def close_all_positions(self) -> dict:
        """Emergency flatten — liquidate ALL open positions AND cancel ALL open
        orders in a single call (Alpaca DELETE /v2/positions?cancel_orders=true)."""
        try:
            client = await self._ensure_client()
            url = f"{settings.ALPACA_BASE_URL}/v2/positions"
            resp = await client.delete(url, params={"cancel_orders": "true"})
            resp.raise_for_status()
            data = resp.json()
            return {"ok": True, "closed": len(data) if isinstance(data, list) else 0}
        except Exception as e:
            log.error("Alpaca close-all-positions failed: %s", e)
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────────────────────────
    # Helpers
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _occ_symbol(underlying: str, expiry: str, opt_type: str, strike: float) -> str:
        """Build OCC option symbol: SPY260511C00740000

        Format: {root}{YYMMDD}{C/P}{strike×1000 zero-padded to 8}
        OCC standard uses 2-digit year (YYMMDD), NOT 4-digit (YYYYMMDD).

        FIX 2026-05-24: was passing full YYYYMMDD which Alpaca rejected,
        causing broker_status=error on every trade since inception.
        """
        root = underlying.upper()
        cp = "C" if opt_type.lower() in ("call", "c") else "P"
        # Ensure 6-digit YYMMDD: strip leading "20" if 8 digits passed
        if len(expiry) == 8 and expiry[:2] in ("19", "20"):
            expiry = expiry[2:]  # "20260511" → "260511"
        # Strike in cents (×1000, 8 digits): 740.50 → 00740500
        strike_int = int(round(strike * 1000))
        return f"{root}{expiry}{cp}{strike_int:08d}"

def order_net_cashflow(order: dict, multiplier: int = 100) -> float | None:
    """Net signed $ cashflow of a FILLED multi-leg order: +received on sold legs,
    −paid on bought legs. Returns None if no leg has a fill price yet (still
    pending). Defensive about Alpaca's response shape — verify on first live fill."""
    if not order:
        return None
    legs = order.get("legs") or [order]
    total, any_fill = 0.0, False
    for leg in legs:
        px = leg.get("filled_avg_price")
        if px in (None, "", "0", "0.0"):
            continue
        try:
            price = float(px)
        except (TypeError, ValueError):
            continue
        qty = leg.get("filled_qty") or leg.get("qty") or order.get("filled_qty") or 0
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            qty = 0.0
        if qty <= 0:
            continue
        any_fill = True
        sign = 1.0 if (leg.get("side") == "sell") else -1.0
        # ×100 multiplier is for OPTION legs only (OCC symbols, e.g.
        # SPY260611C00736000). Equity orders (e.g. the CasaaFinance robo buys
        # sharing this paper account) are 1× — applying 100× made a $408 stock
        # buy read as −$40,791.
        sym = str(leg.get("symbol") or "")
        mult = multiplier if len(sym) > 12 else 1.0
        total += sign * price * qty * mult
    return round(total, 2) if any_fill else None
