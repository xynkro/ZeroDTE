"""VIX1D / VIX gate for the IC builder.

Volatility Box research (and Theta Profits' implicit handling) shows that
selling 0DTE premium in high-IV environments is asymmetrically risky — the
breach rate jumps disproportionately to the premium increase.

This module:
  - Pulls VIX1D (preferred — intraday relevant) or VIX (daily fallback) from Yahoo
  - Caches for 5 minutes to respect Yahoo's rate limits
  - Returns (is_safe, current_vix, threshold) so caller can gate IC builds

Default threshold: 25 (configurable via settings.IC_MAX_VIX). Above this:
  - Refuse IC build with note + Telegram alert
  - Continue wave trading (faster TP, less holding-period risk)
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import httpx

_ET = ZoneInfo("America/New_York")


log = logging.getLogger(__name__)

# Cache (5 min)
_VIX_CACHE: dict[str, tuple[float, float]] = {}  # symbol → (value, fetched_at)
_CACHE_TTL = 300.0


def _fetch_vix(symbol: str = "^VIX1D") -> float | None:
    """Fetch latest VIX value. Returns None on failure."""
    cached = _VIX_CACHE.get(symbol)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]
    try:
        r = httpx.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "5m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5.0,
        )
        r.raise_for_status()
        data = r.json()
        # Pull most recent close
        result = data["chart"]["result"][0]
        meta = result.get("meta", {})
        # Prefer regularMarketPrice (most current)
        price = meta.get("regularMarketPrice")
        if price is None:
            # Fallback to last close in the indicators
            closes = result.get("indicators", {}).get("quote", [{}])[0].get("close", [])
            closes = [c for c in closes if c is not None]
            price = closes[-1] if closes else None
        if price is None:
            return None
        v = float(price)
        _VIX_CACHE[symbol] = (v, time.time())
        log.info("%s = %.2f", symbol, v)
        return v
    except Exception as e:
        log.warning("VIX fetch (%s) failed: %s", symbol, e)
        return None


_VIX_DAILY_CACHE: dict[str, tuple[str, float, float]] = {}  # sym → (date_str, open, prior_close)


def vix_up_at_open(symbol: str = "^VIX") -> tuple[bool | None, float | None, float | None, str]:
    """VIX-up-at-open signal: today's VIX OPEN vs the PRIOR day's CLOSE — both fixed
    once the market opens, so the signal is stable all session and lookahead-safe at
    any intraday entry. Uses ^VIX (NOT VIX1D) to match scripts/vix_up_validation.py,
    which is the series the filter was validated on.

    Returns (is_up, vix_open, prior_close, source). is_up is None when data is
    unavailable — callers FAIL OPEN (treat None as 'up' = do not stand aside) so a
    transient Yahoo outage never silently blocks the whole wave book.

    Cache is keyed on the trading date (not a rolling TTL) so the HTTP call fires
    at most once per session. Pre-market (today's open bar not yet printed) the
    function returns None → fail-open rather than comparing the wrong date's rows."""
    today = datetime.now(_ET).strftime("%Y-%m-%d")
    cached = _VIX_DAILY_CACHE.get(symbol)
    if cached and cached[0] == today:
        _, o, pc = cached
        return (o > pc, o, pc, "cache")
    try:
        r = httpx.get(
            f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
            params={"interval": "1d", "range": "5d"},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=5.0,
        )
        r.raise_for_status()
        res = r.json()["chart"]["result"][0]
        q = res.get("indicators", {}).get("quote", [{}])[0]
        timestamps = res.get("timestamp", [])
        # Zip timestamps so we can validate which date each bar belongs to
        rows = [
            (ts, o, c)
            for ts, o, c in zip(timestamps, q.get("open", []), q.get("close", []))
            if o is not None and c is not None
        ]
        if len(rows) < 2:
            return None, None, None, "insufficient"
        # Validate that the most recent row is actually today, not yesterday's bar
        # (pre-market, today's open bar may not yet be published by Yahoo)
        last_date = datetime.fromtimestamp(rows[-1][0], tz=_ET).strftime("%Y-%m-%d")
        if last_date != today:
            log.info("vix_up_at_open: last bar is %s, not today %s — pre-market, fail-open",
                     last_date, today)
            return None, None, None, "pre-market"
        today_open = rows[-1][1]
        prior_close = rows[-2][2]
        _VIX_DAILY_CACHE[symbol] = (today, today_open, prior_close)
        log.info("%s up-at-open: open %.2f vs prior close %.2f → %s",
                 symbol, today_open, prior_close, "UP" if today_open > prior_close else "down")
        return (today_open > prior_close, today_open, prior_close, symbol)
    except Exception as e:  # noqa: BLE001
        log.warning("vix_up_at_open(%s) failed: %s", symbol, e)
        return None, None, None, "unavailable"


def check_iv_safe(threshold: float = 25.0) -> tuple[bool, float | None, str]:
    """Return (is_safe, current_vix, source_used).

    Tries VIX1D first (intraday-relevant); falls back to VIX (daily) if VIX1D
    unavailable. If BOTH fail, defaults to is_safe=True with a warning so a
    transient outage doesn't permanently block the system.
    """
    # Try VIX1D first (more relevant for 0DTE)
    v1d = _fetch_vix("^VIX1D")
    if v1d is not None:
        return v1d < threshold, v1d, "VIX1D"
    # Fall back to VIX
    vix = _fetch_vix("^VIX")
    if vix is not None:
        return vix < threshold, vix, "VIX"
    # Both unavailable — fail open (don't block IC if we can't measure)
    log.warning("Both VIX1D and VIX fetch failed — defaulting to safe=True (failsafe-open)")
    return True, None, "unavailable"
