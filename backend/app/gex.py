"""Gamma Exposure (GEX) — dealer-positioning regime signal for sizing.

Data source: CBOE free delayed-quotes API, which returns per-option gamma + open
interest + greeks directly (no yfinance, no subscription, no key):
    https://cdn.cboe.com/api/global/delayed_quotes/options/_SPX.json   (index → underscore)
    https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json

What this gives us (the academically-supported part — Buis et al. JEDC 2024,
Egebjerg & Kokholm SSRN 2024): the dealer-gamma VOLATILITY REGIME, not direction.
  • Net GEX > 0  → dealers long gamma → hedge counter-cyclically → vol SUPPRESSED
                   (range/pin day → favorable for short-premium / TP90 theta harvest)
  • Net GEX < 0  → dealers short gamma → hedge pro-cyclically → vol AMPLIFIED
                   (trend/whipsaw day → short strike more likely breached before TP)

Naive convention (SqueezeMetrics): dealers long calls / short puts →
    net GEX = Σ_calls(gamma·OI) − Σ_puts(gamma·OI),  scaled to $ per 1% move.
This is unreliable for DIRECTION (it doesn't separate dealer vs customer) but the
regime sign + magnitude is the defensible, evidence-backed read. We use it ONLY for
sizing/regime — never as a directional trigger.
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

CBOE_URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{sym}.json"
MULTIPLIER = 100
# OCC tail: 6-digit YYMMDD, C/P, 8-digit strike×1000  e.g. SPXW260530C07500000
_OCC = re.compile(r"(\d{6})([CP])(\d{8})$")


@dataclass
class GexResult:
    ok: bool
    symbol: str
    spot: float = 0.0
    net_gex_b: float = 0.0           # net GEX, $ billions per 1% move (our convention's scale)
    net_ratio: float = 0.0           # net/gross — calibration-free balance in [-1, +1]
    regime: str = "unknown"          # "positive" | "negative" | "neutral" | "unknown"
    call_wall: float | None = None   # strike with the most call gamma·OI (resistance magnet)
    put_wall: float | None = None    # strike with the most put gamma·OI (support magnet)
    n_contracts: int = 0
    asof: str | None = None
    error: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"GEX unavailable ({self.error})"
        arrow = {"positive": "🟢 vol-suppressed (range/pin)",
                 "negative": "🔴 vol-amplified (trend/whipsaw)",
                 "neutral": "⚪ near gamma-flip"}.get(self.regime, "?")
        cw = f" callwall {self.call_wall:.0f}" if self.call_wall else ""
        pw = f" putwall {self.put_wall:.0f}" if self.put_wall else ""
        return f"GEX {self.net_gex_b:+.2f}B/1% · {arrow}{cw}{pw}"


def _parse_occ(sym: str):
    """Return (is_call, strike) from an OCC symbol, or None."""
    m = _OCC.search(sym or "")
    if not m:
        return None
    return (m.group(2) == "C", int(m.group(3)) / 1000.0)


def compute_gex(chain: dict, neutral_band: float = 0.05) -> GexResult:
    """Compute net GEX + regime + walls from a CBOE delayed-quotes payload.

    Regime is classified on net/gross ratio (calibration-free): |ratio| < neutral_band
    means calls and puts roughly balance → near the gamma flip → weak signal that
    shouldn't drive sizing. Using a ratio (not an absolute $B threshold) keeps the
    classification robust to the convention's scale.
    """
    try:
        data = chain.get("data") or {}
        symbol = data.get("symbol") or chain.get("symbol") or "?"
        spot = float(data.get("current_price") or data.get("close") or 0.0)
        options = data.get("options") or []
        if spot <= 0 or not options:
            return GexResult(ok=False, symbol=symbol, error="empty chain / no spot")

        s2 = spot * spot * 0.01  # $ per 1% move scaler
        net = 0.0
        gross = 0.0
        call_by_strike: dict[float, float] = {}
        put_by_strike: dict[float, float] = {}
        n = 0
        for o in options:
            oi = o.get("open_interest") or 0
            gamma = o.get("gamma") or 0.0
            if oi <= 0 or gamma == 0.0:
                continue
            parsed = _parse_occ(o.get("option", ""))
            if parsed is None:
                continue
            is_call, strike = parsed
            dollar_gamma = gamma * oi * MULTIPLIER * s2
            gross += dollar_gamma
            n += 1
            if is_call:
                net += dollar_gamma
                call_by_strike[strike] = call_by_strike.get(strike, 0.0) + dollar_gamma
            else:
                net -= dollar_gamma
                put_by_strike[strike] = put_by_strike.get(strike, 0.0) + dollar_gamma

        net_b = net / 1e9
        net_ratio = (net / gross) if gross > 0 else 0.0
        if net_ratio > neutral_band:
            regime = "positive"
        elif net_ratio < -neutral_band:
            regime = "negative"
        else:
            regime = "neutral"

        call_wall = max(call_by_strike, key=call_by_strike.get) if call_by_strike else None
        put_wall = max(put_by_strike, key=put_by_strike.get) if put_by_strike else None

        return GexResult(
            ok=True, symbol=symbol, spot=spot, net_gex_b=round(net_b, 3),
            net_ratio=round(net_ratio, 4),
            regime=regime, call_wall=call_wall, put_wall=put_wall,
            n_contracts=n, asof=data.get("last_trade_time"),
        )
    except Exception as e:
        log.warning("compute_gex failed: %s", e)
        return GexResult(ok=False, symbol="?", error=str(e))


async def fetch_gex(symbol: str = "_SPX", timeout: float = 20.0) -> GexResult:
    """Fetch the CBOE chain and compute GEX. Async (httpx). Never raises — returns
    GexResult(ok=False, ...) on any failure so callers degrade gracefully."""
    import httpx
    url = CBOE_URL.format(sym=symbol)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return compute_gex(resp.json())
    except Exception as e:
        log.warning("fetch_gex(%s) failed: %s", symbol, e)
        return GexResult(ok=False, symbol=symbol, error=str(e))
