"""Delta-targeted strike selection from a live options chain.

Two modes:
  WAVE       — short strike at ~25 delta (bigger credit, ~70% POP, fits the
               'sell at peaks/troughs on indicator confluence' pattern)
  IRON_CONDOR — short strike at ~8 delta (low credit, ~92% POP, fits the
               'deploy and forget for overnight expiration' pattern)

The chain comes from IbkrFeed.get_options_chain_with_greeks().
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass
class StrikePair:
    instrument: Literal["XSP", "SPX", "SPY"]
    side: Literal["sell_call_cs", "sell_put_cs"]
    mode: Literal["wave", "iron_condor"]
    short_strike: float
    long_strike: float
    short_delta: float | None
    long_delta: float | None
    wing_width: float
    multiplier: int
    # Live pricing (from chain)
    short_bid: float | None
    short_ask: float | None
    short_mid: float | None
    long_bid: float | None
    long_ask: float | None
    long_mid: float | None
    # Computed
    estimated_credit: float | None  # net credit per contract (already × multiplier? NO — per share, then × multiplier for $ )
    estimated_credit_dollars: float | None  # credit × multiplier
    max_loss_dollars: float | None
    breakeven: float | None
    roi_pct: float | None
    pop_estimate_pct: float | None  # 1 - short_delta (heuristic)
    # Notes / warnings
    warnings: list[str]


# Multipliers (contract size)
MULTIPLIERS = {"XSP": 100, "SPX": 100, "SPY": 100}

# Default wing widths
DEFAULT_WING_WIDTH = {"XSP": 5, "SPX": 5, "SPY": 1}


def _find_closest_by_delta(
    strikes_with_greeks: list[dict],
    target_delta: float,
    side: Literal["call", "put"],
    underlying_price: float | None = None,
) -> dict | None:
    """Find the strike whose abs(delta) is closest to target.

    For calls: delta is positive, range 0..1. For puts: delta is negative,
    range -1..0. We compare |delta|.

    HARD SAFETY: when underlying_price is given, only OTM strikes are eligible:
      - sell-call: strike > underlying  (call must be OTM)
      - sell-put : strike < underlying  (put must be OTM)
    This prevents the deeply-ITM-call disaster when delayed-data Greeks are
    missing or 0 (every candidate ties → min() returns first/lowest strike).

    SOFT FILTER: require sensible |delta| in (0.001, 0.99) so we never pick
    a strike where Greeks are clearly absent / unparseable / extreme.
    """
    candidates = [s for s in strikes_with_greeks if s.get("delta") is not None]
    if not candidates:
        return None
    # Sane delta filter — exclude strikes where Greeks didn't compute properly
    candidates = [s for s in candidates if 0.001 < abs(s["delta"]) < 0.99]
    if not candidates:
        return None
    # OTM-side filter (hard safety — never sell an ITM strike for credit)
    if underlying_price is not None:
        if side == "call":
            candidates = [s for s in candidates if s["strike"] > underlying_price]
        else:
            candidates = [s for s in candidates if s["strike"] < underlying_price]
    if not candidates:
        return None
    return min(candidates, key=lambda s: abs(abs(s["delta"]) - target_delta))


def _find_long_wing(
    strikes_with_greeks: list[dict],
    short_strike: float,
    wing_width: float,
    side: Literal["call", "put"],
) -> dict | None:
    """Pick the long strike at short ± wing_width, falling back to closest."""
    target = short_strike + wing_width if side == "call" else short_strike - wing_width
    available = sorted(strikes_with_greeks, key=lambda s: s["strike"])
    # Exact match first
    for s in available:
        if abs(s["strike"] - target) < 0.01:
            return s
    # Else closest in the right direction
    if side == "call":
        farther = [s for s in available if s["strike"] > short_strike]
        return min(farther, key=lambda s: abs(s["strike"] - target)) if farther else None
    else:
        closer_to_zero = [s for s in available if s["strike"] < short_strike]
        return min(closer_to_zero, key=lambda s: abs(s["strike"] - target)) if closer_to_zero else None


def build_strike_pair_from_chain(
    instrument: Literal["XSP", "SPX", "SPY"],
    side: Literal["sell_call_cs", "sell_put_cs"],
    mode: Literal["wave", "iron_condor"],
    chain: dict,
    target_delta: float,
    wing_width: float | None = None,
) -> StrikePair | None:
    """Pick short + long strikes from a live chain. Compute all economics."""
    if not chain or "calls" not in chain or "puts" not in chain:
        return None
    if wing_width is None:
        wing_width = DEFAULT_WING_WIDTH[instrument]
    is_call = side == "sell_call_cs"
    leg_data = chain["calls"] if is_call else chain["puts"]
    if not leg_data:
        return None

    underlying = chain.get("underlying_price")
    short_row = _find_closest_by_delta(
        leg_data, target_delta, "call" if is_call else "put",
        underlying_price=underlying,
    )
    if short_row is None:
        return None
    long_row = _find_long_wing(leg_data, short_row["strike"], wing_width, "call" if is_call else "put")
    if long_row is None:
        return None

    warnings: list[str] = []
    # Use mid (bid/ask midpoint) when available; falls back to last/close/model_price
    # already in the chain fetcher. Greeks should always be present (server-side).
    short_mid = short_row.get("mid")
    long_mid  = long_row.get("mid")

    credit_per_share = None
    if short_mid is not None and long_mid is not None:
        credit_per_share = max(0.0, short_mid - long_mid)
    if credit_per_share is None:
        if (short_row.get("bid") is None or short_row.get("ask") is None):
            warnings.append("after-hours — using model/close price; live bid/ask resumes at 09:30 ET")
        else:
            warnings.append("credit unavailable — check IBKR market data subscription")

    multiplier = MULTIPLIERS[instrument]
    actual_wing = abs(long_row["strike"] - short_row["strike"])
    credit_dollars = credit_per_share * multiplier if credit_per_share is not None else None
    max_loss_dollars = (
        actual_wing * multiplier - credit_dollars
        if credit_dollars is not None else None
    )
    breakeven = None
    if credit_per_share is not None:
        breakeven = (
            short_row["strike"] + credit_per_share if is_call else
            short_row["strike"] - credit_per_share
        )
    roi_pct = None
    if max_loss_dollars and max_loss_dollars > 0 and credit_dollars is not None:
        roi_pct = 100.0 * credit_dollars / max_loss_dollars

    short_delta = abs(short_row["delta"]) if short_row.get("delta") is not None else None
    pop_pct = (1.0 - short_delta) * 100.0 if short_delta is not None else None

    if short_delta is not None and abs(short_delta - target_delta) > 0.10:
        warnings.append(
            f"short delta {short_delta:.2f} far from target {target_delta:.2f}"
        )
    if credit_per_share is not None and credit_per_share < 0.10:
        warnings.append("credit < $0.10 — illiquid or zero-edge")

    return StrikePair(
        instrument=instrument, side=side, mode=mode,
        short_strike=short_row["strike"], long_strike=long_row["strike"],
        short_delta=short_delta,
        long_delta=abs(long_row["delta"]) if long_row.get("delta") is not None else None,
        wing_width=actual_wing,
        multiplier=multiplier,
        short_bid=short_row.get("bid"), short_ask=short_row.get("ask"), short_mid=short_mid,
        long_bid=long_row.get("bid"),  long_ask=long_row.get("ask"),   long_mid=long_mid,
        estimated_credit=credit_per_share,
        estimated_credit_dollars=credit_dollars,
        max_loss_dollars=max_loss_dollars,
        breakeven=breakeven,
        roi_pct=roi_pct,
        pop_estimate_pct=pop_pct,
        warnings=warnings,
    )


# Default delta targets
WAVE_DELTA = 0.12         # Phase 3 canonical: 10-15Δ short (was 0.25/25Δ).
                          # 25Δ at 0.5% OTM was negative EV per IC backtest;
                          # 10-15Δ at 1.5% OTM matches TradingBlock/CBOE canonical.
IC_DELTA   = 0.20         # 20-delta short strike — sweet spot for 0DTE IC
                          #  - 8Δ is "set-and-forget set-and-forget" (Henry's late-day number)
                          #    but premium dies if deployed earlier than 13:00 ET
                          #  - 20Δ collects $0.40-0.80 per side at 10:30-13:00 ET deploy
                          #  - matches user expectation + canonical tastytrade "1/3 wing" rule
                          #
                          # Override per-deploy via .env IC_DELTA= if you want 8Δ late-day


# ────────────────────────────────────────────────────────────────────────
# Melded strike picker — geometric floor + optional delta upgrade
# ────────────────────────────────────────────────────────────────────────
# Approach: trust the geometric strike (X% OTM, what the 12-month backtest
# validated at 96.1% WR) as the SAFE DEFAULT. When the live chain has clean
# Greeks AND the delta-pick lands within a sanity band of the geometric,
# upgrade to delta-targeted (smarter on regime-mismatched IV days). Otherwise
# stay geometric. This collapses Option 1 + Option 2 into one safe picker.

def pick_short_strike(
    side: Literal["call", "put"],
    underlying: float,
    chain: dict,
    *,
    default_pct_otm: float = 0.5,            # backtest-validated geometric default (~20Δ at 0DTE)
    target_delta: float = 0.20,              # 20Δ short = canonical 0DTE IC, ~$0.40-0.80 premium
    sanity_min_dist_pct: float = 0.3,        # delta-pick can be 30%-300% of geometric distance
    sanity_max_dist_pct: float = 3.0,        # — generous so delta-upgrade actually engages
    strike_increment: float = 1.0,           # round to nearest valid strike (1pt for XSP, 5pt for SPX)
) -> tuple[float, str, dict | None]:
    """Return (short_strike, method_label, delta_row_or_None).

    method_label ∈ {"geometric", "delta_upgraded", "geometric_fallback"}
      geometric           — chain unavailable or no usable strikes; pure geometric
      delta_upgraded      — chain healthy AND delta-pick passed sanity band
      geometric_fallback  — chain available but delta-pick was suspect; reverted to geometric

    The geometric strike is computed first, ALWAYS. That's the floor.
    Delta upgrade is layered ONLY when its result is "close enough" to geometric.
    """
    import math

    # 1. Compute geometric (always)
    geo_dist = underlying * (default_pct_otm / 100.0)
    if side == "call":
        geo_strike_raw = underlying + geo_dist
    else:
        geo_strike_raw = underlying - geo_dist
    # Round to a tradeable strike
    geo_strike = round(geo_strike_raw / strike_increment) * strike_increment

    # 2. Try chain-based delta pick
    if not chain or "calls" not in chain or "puts" not in chain:
        return geo_strike, "geometric", None

    leg_data = chain["calls"] if side == "call" else chain["puts"]
    delta_row = _find_closest_by_delta(
        leg_data, target_delta, side, underlying_price=underlying,
    )
    if delta_row is None:
        return geo_strike, "geometric", None

    delta_strike = delta_row["strike"]
    delta_dist = abs(delta_strike - underlying)
    geo_dist_actual = abs(geo_strike - underlying)
    if geo_dist_actual <= 0:
        return geo_strike, "geometric", None

    ratio = delta_dist / geo_dist_actual
    # Sanity band: delta-pick should be 50%-200% of the geometric distance.
    # Outside that band → Greeks are suspect, IV is unusual, or the picker
    # found something we shouldn't trust. Stay geometric.
    if not (sanity_min_dist_pct <= ratio <= sanity_max_dist_pct):
        return geo_strike, "geometric_fallback", delta_row

    return delta_strike, "delta_upgraded", delta_row


def build_strike_pair_melded(
    instrument: Literal["XSP", "SPX", "SPY"],
    side: Literal["sell_call_cs", "sell_put_cs"],
    mode: Literal["wave", "iron_condor"],
    chain: dict | None,
    underlying: float,
    *,
    default_pct_otm: float = 1.0,
    target_delta: float | None = None,
    wing_width: float | None = None,
) -> StrikePair | None:
    """Build a StrikePair using the melded geometric+delta picker.

    Logic:
      1. Pick short_strike via pick_short_strike (geometric floor + delta upgrade)
      2. Long_strike = short ± wing_width
      3. Pull mids from chain if available (for credit estimate); else None.
    """
    if wing_width is None:
        wing_width = DEFAULT_WING_WIDTH[instrument]
    if target_delta is None:
        target_delta = IC_DELTA if mode == "iron_condor" else WAVE_DELTA

    # XSP/SPX use 1pt or 5pt increments. Use 1pt for XSP/SPY, 5pt for SPX
    # (SPX strike grid is wider than XSP).
    strike_increment = 1.0 if instrument in ("XSP", "SPY") else 5.0

    is_call = side == "sell_call_cs"
    short_strike, method, delta_row = pick_short_strike(
        side="call" if is_call else "put",
        underlying=underlying,
        chain=chain or {},
        default_pct_otm=default_pct_otm,
        target_delta=target_delta,
        strike_increment=strike_increment,
    )
    long_strike = short_strike + wing_width if is_call else short_strike - wing_width

    multiplier = MULTIPLIERS[instrument]
    actual_wing = abs(long_strike - short_strike)

    # Pull live quotes from chain if it's there
    short_mid = short_bid = short_ask = short_delta = None
    long_mid = long_bid = long_ask = long_delta = None
    credit_per_share = None
    if chain and "calls" in chain and "puts" in chain:
        leg_data = chain["calls"] if is_call else chain["puts"]
        # Find rows by strike
        short_row = next((r for r in leg_data if abs(r["strike"] - short_strike) < 0.01), None)
        long_row  = next((r for r in leg_data if abs(r["strike"] - long_strike)  < 0.01), None)
        if short_row:
            short_mid = short_row.get("mid")
            short_bid = short_row.get("bid")
            short_ask = short_row.get("ask")
            short_delta = abs(short_row["delta"]) if short_row.get("delta") is not None else None
        if long_row:
            long_mid = long_row.get("mid")
            long_bid = long_row.get("bid")
            long_ask = long_row.get("ask")
            long_delta = abs(long_row["delta"]) if long_row.get("delta") is not None else None
        if short_mid is not None and long_mid is not None:
            credit_per_share = max(0.0, short_mid - long_mid)

    credit_dollars = credit_per_share * multiplier if credit_per_share is not None else None
    max_loss_dollars = (actual_wing * multiplier - credit_dollars) if credit_dollars is not None else None
    breakeven = None
    if credit_per_share is not None:
        breakeven = (short_strike + credit_per_share) if is_call else (short_strike - credit_per_share)
    roi_pct = None
    if max_loss_dollars and max_loss_dollars > 0 and credit_dollars is not None:
        roi_pct = 100.0 * credit_dollars / max_loss_dollars
    pop_pct = (1.0 - short_delta) * 100.0 if short_delta is not None else None

    warnings: list[str] = []
    warnings.append(f"strike_method={method}")
    if method == "geometric":
        warnings.append(f"geometric default ({default_pct_otm:.1f}% OTM) — chain unavailable")
    elif method == "geometric_fallback":
        if delta_row:
            warnings.append(
                f"delta-pick at ${delta_row['strike']:.0f} (Δ={delta_row.get('delta',0):.2f}) "
                f"out of sanity band — using geometric"
            )
    if credit_per_share is not None and credit_per_share < 0.10:
        warnings.append("credit < $0.10 — illiquid or thin")

    return StrikePair(
        instrument=instrument, side=side, mode=mode,
        short_strike=float(short_strike), long_strike=float(long_strike),
        short_delta=short_delta, long_delta=long_delta,
        wing_width=float(actual_wing), multiplier=multiplier,
        short_bid=short_bid, short_ask=short_ask, short_mid=short_mid,
        long_bid=long_bid, long_ask=long_ask, long_mid=long_mid,
        estimated_credit=credit_per_share,
        estimated_credit_dollars=credit_dollars,
        max_loss_dollars=max_loss_dollars,
        breakeven=breakeven,
        roi_pct=roi_pct,
        pop_estimate_pct=pop_pct,
        warnings=warnings,
    )


def fallback_pair_no_chain(
    instrument: Literal["XSP", "SPX", "SPY"],
    side: Literal["sell_call_cs", "sell_put_cs"],
    underlying_price: float,
    projected_boundary: float,
) -> StrikePair:
    """When no chain is available (IBKR offline or out-of-hours), produce a
    best-effort suggestion based on projected boundary alone — no Greeks."""
    import math
    increment = 5 if instrument in ("XSP", "SPX") else 1
    wing = DEFAULT_WING_WIDTH[instrument]
    multiplier = MULTIPLIERS[instrument]
    # Convert SPX-level projection to instrument scale
    if instrument == "XSP" or instrument == "SPY":
        boundary = projected_boundary * 0.1
        underlying = underlying_price * 0.1
    else:
        boundary = projected_boundary
        underlying = underlying_price
    if side == "sell_call_cs":
        short = math.ceil(boundary / increment) * increment
        if short <= underlying:
            short = math.ceil(underlying / increment) * increment + increment
        long_strike = short + wing
    else:
        short = math.floor(boundary / increment) * increment
        if short >= underlying:
            short = math.floor(underlying / increment) * increment - increment
        long_strike = short - wing

    return StrikePair(
        instrument=instrument, side=side, mode="iron_condor",  # default to IC for fallback
        short_strike=float(short), long_strike=float(long_strike),
        short_delta=None, long_delta=None,
        wing_width=float(wing), multiplier=multiplier,
        short_bid=None, short_ask=None, short_mid=None,
        long_bid=None, long_ask=None, long_mid=None,
        estimated_credit=None, estimated_credit_dollars=None,
        max_loss_dollars=wing * multiplier,
        breakeven=None, roi_pct=None, pop_estimate_pct=None,
        warnings=["no_live_chain_data — strikes anchored to projected boundary, no Greeks/credit"],
    )
