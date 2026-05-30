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
from typing import Optional

import httpx


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
