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


def pick_iron_condor(chain: dict, short_delta: float = 0.16, wing: float = 25.0,
                     min_dte_date: str | None = None) -> dict:
    """Pick a real, delta-based iron condor from a CBOE delayed-quotes chain.

    Places both shorts at ~short_delta using the chain's REAL deltas (not a
    geometric %OTM guess), longs one `wing` away (nearest listed strike), and
    computes the actual fillable credit from bid/ask. Uses the NEAREST expiry
    (the 0DTE on a live trading day). Returns a dict with ok=False + error on
    failure so callers degrade gracefully.

    Credit convention (conservative / fillable): you SELL the short (collect its
    bid) and BUY the long (pay its ask), per side.
    """
    try:
        data = chain.get("data") or {}
        spot = float(data.get("current_price") or data.get("close") or 0.0)
        options = data.get("options") or []
        if spot <= 0 or not options:
            return {"ok": False, "error": "empty chain / no spot"}

        # Parse + group by expiry
        by_exp: dict[str, dict] = {}
        for o in options:
            sym = o.get("option", "")
            m = _OCC.search(sym)
            if not m:
                continue
            exp, cp, strike8 = m.group(1), m.group(2), m.group(3)
            strike = int(strike8) / 1000.0
            rec = by_exp.setdefault(exp, {"calls": {}, "puts": {}})
            leg = {"strike": strike, "delta": o.get("delta") or 0.0,
                   "bid": o.get("bid") or 0.0, "ask": o.get("ask") or 0.0}
            (rec["calls"] if cp == "C" else rec["puts"])[strike] = leg

        if not by_exp:
            return {"ok": False, "error": "no parseable options"}
        # Nearest expiry ≥ min_dte_date (default: earliest expiry present = 0DTE live)
        exps = sorted(by_exp.keys())
        if min_dte_date:
            future = [e for e in exps if e >= min_dte_date]
            exp = future[0] if future else exps[0]
        else:
            exp = exps[0]
        calls, puts = by_exp[exp]["calls"], by_exp[exp]["puts"]
        call_strikes = sorted(calls.keys())
        put_strikes = sorted(puts.keys())
        if not call_strikes or not put_strikes:
            return {"ok": False, "error": f"expiry {exp} missing a side"}

        # Short call: OTM call (strike>spot) with delta nearest +short_delta
        otm_calls = [s for s in call_strikes if s > spot and calls[s]["delta"] > 0]
        otm_puts = [s for s in put_strikes if s < spot and puts[s]["delta"] < 0]
        if not otm_calls or not otm_puts:
            return {"ok": False, "error": "no OTM strikes with greeks"}
        sc = min(otm_calls, key=lambda s: abs(calls[s]["delta"] - short_delta))
        sp = min(otm_puts, key=lambda s: abs(abs(puts[s]["delta"]) - short_delta))
        # Longs: nearest listed strike ≥/≤ short ± wing
        lc_candidates = [s for s in call_strikes if s >= sc + wing]
        lp_candidates = [s for s in put_strikes if s <= sp - wing]
        if not lc_candidates or not lp_candidates:
            return {"ok": False, "error": "wing strikes not listed"}
        lc = min(lc_candidates)
        lp = max(lp_candidates)

        call_credit = (calls[sc]["bid"] or 0.0) - (calls[lc]["ask"] or 0.0)
        put_credit = (puts[sp]["bid"] or 0.0) - (puts[lp]["ask"] or 0.0)
        total_credit = call_credit + put_credit
        call_wing = lc - sc
        put_wing = sp - lp
        max_wing = max(call_wing, put_wing)
        max_loss = max_wing * 100 - total_credit * 100   # one side breaches
        credit_usd = total_credit * 100
        credit_pct = (credit_usd / (max_wing * 100) * 100) if max_wing > 0 else 0.0

        return {
            "ok": True, "expiry": exp, "spot": spot,
            "short_call": sc, "long_call": lc, "short_put": sp, "long_put": lp,
            "short_call_delta": round(calls[sc]["delta"], 3),
            "short_put_delta": round(puts[sp]["delta"], 3),
            "call_credit_usd": round(call_credit * 100, 2),
            "put_credit_usd": round(put_credit * 100, 2),
            "total_credit_usd": round(credit_usd, 2),
            "max_loss_usd": round(max_loss, 2),
            "credit_pct_of_wing": round(credit_pct, 1),
            "call_wing": call_wing, "put_wing": put_wing,
        }
    except Exception as e:
        log.warning("pick_iron_condor failed: %s", e)
        return {"ok": False, "error": str(e)}


async def fetch_chain(symbol: str = "_SPX", timeout: float = 20.0) -> dict | None:
    """Fetch the raw CBOE delayed-quotes chain. Returns None on failure."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as client:
            resp = await client.get(CBOE_URL.format(sym=symbol))
            resp.raise_for_status()
            return resp.json()
    except Exception as e:
        log.warning("fetch_chain(%s) failed: %s", symbol, e)
        return None


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


def chain_mids_for_expiry(chain: dict, expiry_yymmdd: str) -> dict:
    """Adapt a CBOE delayed-quotes payload to {'calls': [{strike, mid}], 'puts': […]}
    for ONE expiry — lets the IC breakeven stop-rule mark to market when the primary
    feed has no options chain (Alpaca free tier / yfinance)."""
    out = {"calls": [], "puts": []}
    options = (chain.get("data") or {}).get("options") or []
    for o in options:
        sym = o.get("option", "")
        if expiry_yymmdd not in sym:
            continue
        parsed = _parse_occ(sym)
        if parsed is None:
            continue
        is_call, strike = parsed
        bid, ask = o.get("bid") or 0.0, o.get("ask") or 0.0
        if bid <= 0 and ask <= 0:
            continue
        out["calls" if is_call else "puts"].append(
            {"strike": strike, "mid": (bid + ask) / 2.0})
    return out


def condor_net_credit_from_chain(
    chain_mids: dict,
    call_short: float, call_long: float,
    put_short: float,  put_long: float,
) -> float | None:
    """Net per-share credit at CBOE mids for a 4-leg short iron condor.

    `chain_mids` is the dict returned by chain_mids_for_expiry() — one expiry,
    pre-split into {'calls': [...], 'puts': [...]}. Returns the SAME scale as
    the chain's strikes (SPX strikes → SPX per-share credit). Returns None if
    any leg's mid is unavailable — the caller decides how to fall back.

    Net credit = (call_short_mid − call_long_mid) + (put_short_mid − put_long_mid)
    i.e. premium collected on the sold legs minus premium paid for the wings.
    """
    if not chain_mids:
        return None

    def _mid(rows, target):
        for r in rows or ():
            if abs((r.get("strike") or 0.0) - target) < 0.01:
                m = r.get("mid")
                if m is None or m <= 0:
                    return None
                return float(m)
        return None

    sc = _mid(chain_mids.get("calls"), call_short)
    lc = _mid(chain_mids.get("calls"), call_long)
    sp = _mid(chain_mids.get("puts"),  put_short)
    lp = _mid(chain_mids.get("puts"),  put_long)
    if None in (sc, lc, sp, lp):
        return None
    return round((sc - lc) + (sp - lp), 4)
