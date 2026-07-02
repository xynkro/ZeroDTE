"""Black-Scholes pricing for honest 0DTE spread backtesting.

Design choice: every function takes **total volatility to expiry** `tv = sigma*sqrt(T)`
as a single input, instead of (sigma, T) separately. This eliminates the intraday
annualization ambiguity (calendar vs trading minutes, overnight gaps) that plagues
0DTE modeling — `tv` is the only thing BS actually needs, and we can build it
directly from realized 5-minute volatility and the number of 5m periods left to expiry.

Assumes r=0 and no dividends (negligible for same-day expiry). All prices are
per-share (multiply by 100 for one contract).
"""
from __future__ import annotations

import math

SQRT_2 = math.sqrt(2.0)
SQRT_2PI = math.sqrt(2.0 * math.pi)


def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / SQRT_2))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _d1(S: float, K: float, tv: float) -> float:
    # tv = sigma*sqrt(T); d1 = (ln(S/K) + 0.5*tv^2) / tv
    return (math.log(S / K) + 0.5 * tv * tv) / tv


def call_price(S: float, K: float, tv: float) -> float:
    if tv <= 0.0:
        return max(0.0, S - K)
    d1 = _d1(S, K, tv)
    d2 = d1 - tv
    return S * _ncdf(d1) - K * _ncdf(d2)


def put_price(S: float, K: float, tv: float) -> float:
    if tv <= 0.0:
        return max(0.0, K - S)
    d1 = _d1(S, K, tv)
    d2 = d1 - tv
    return K * _ncdf(-d2) - S * _ncdf(-d1)


def call_delta(S: float, K: float, tv: float) -> float:
    if tv <= 0.0:
        return 1.0 if S > K else 0.0
    return _ncdf(_d1(S, K, tv))


def put_delta(S: float, K: float, tv: float) -> float:
    if tv <= 0.0:
        return -1.0 if S < K else 0.0
    return _ncdf(_d1(S, K, tv)) - 1.0


# ── Strike placement by target delta ────────────────────────────────────────

def strike_for_call_delta(S: float, tv: float, target_delta: float) -> float:
    """Find OTM call strike K>S whose delta == target_delta (0<target<0.5 typ).

    Call delta = N(d1) is strictly decreasing in K. Bisection.
    """
    if tv <= 0.0:
        return S  # degenerate
    lo, hi = S, S * 3.0
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if call_delta(S, mid, tv) > target_delta:
            lo = mid   # delta too high → push strike further OTM (up)
        else:
            hi = mid
    return 0.5 * (lo + hi)


def strike_for_put_delta(S: float, tv: float, target_delta: float) -> float:
    """Find OTM put strike K<S whose |delta| == target_delta.

    |put delta| = N(-d1), increasing as K rises toward S. Bisection on K<S.
    """
    if tv <= 0.0:
        return S
    lo, hi = S * 0.3, S
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        if abs(put_delta(S, mid, tv)) > target_delta:
            hi = mid   # |delta| too high → push strike further OTM (down)
        else:
            lo = mid
    return 0.5 * (lo + hi)


def implied_tv(price: float, S: float, K: float, is_call: bool) -> float | None:
    """Invert BS for total-vol-to-expiry from an observed option price.

    Per-strike inversion (each strike carries its OWN tv) keeps deltas
    smile-consistent — a flat ATM vol under-prices the put tail and places
    'target-delta' strikes too close on skewed days. Monotone in tv → bisection.
    Returns None when the price is at/below intrinsic or outside the bracket
    (stale/crossed quote) so callers can drop the strike instead of trusting it.
    """
    if price is None or price <= 0.0 or S <= 0.0 or K <= 0.0:
        return None
    intrinsic = max(0.0, S - K) if is_call else max(0.0, K - S)
    if price <= intrinsic + 1e-6:
        return None
    fn = call_price if is_call else put_price
    lo, hi = 1e-6, 0.5   # 0DTE tv rarely exceeds ~0.10 even on FOMC; 0.5 is generous
    if fn(S, K, hi) < price:
        return None      # price above bracket → not a sane same-day quote
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if fn(S, K, mid) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def spread_value(side: str, S: float, short_K: float, long_K: float, tv: float) -> float:
    """Per-share value (cost to close) of the SHORT vertical credit spread."""
    if side == "sell_call_cs":           # bear call: short short_K, long long_K (>short_K)
        return max(0.0, call_price(S, short_K, tv) - call_price(S, long_K, tv))
    else:                                 # bull put: short short_K, long long_K (<short_K)
        return max(0.0, put_price(S, short_K, tv) - put_price(S, long_K, tv))


# ── Volatility helpers ───────────────────────────────────────────────────────

def total_vol_to_expiry(realized_5m_std: float, periods_remaining: float,
                        premium_mult: float = 1.20) -> float:
    """tv = sigma*sqrt(T) built directly from 5m realized vol.

    realized_5m_std : stdev of 5-minute log returns (per-period, NOT annualized)
    periods_remaining: number of 5m periods until expiry (mins_to_close / 5)
    premium_mult     : IV/RV ratio (option IV typically exceeds realized vol)
    """
    return realized_5m_std * math.sqrt(max(periods_remaining, 0.0)) * premium_mult


def realized_5m_std(closes: list[float]) -> float:
    """Stdev of 5-minute log returns from a list of consecutive closes."""
    if len(closes) < 3:
        return 0.0
    rets = []
    for i in range(1, len(closes)):
        if closes[i - 1] > 0 and closes[i] > 0:
            rets.append(math.log(closes[i] / closes[i - 1]))
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    var = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var)
