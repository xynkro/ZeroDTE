"""Unusual Options Activity (UOA) scanner — IBKR chain data.

Scans the 0DTE option chain for anomalies that suggest large or informed
positioning:
  - Volume/OI ratio spikes (new positions being opened aggressively)
  - Premium concentration (institutional clustering at specific strikes)
  - IV skew anomalies (single-strike IV deviating from neighbors)
  - Large order sizes on bid/ask (whale orders sitting in the book)
  - Net delta flow (aggregate directional conviction)

Designed to run periodically during market hours via the orchestrator.
Results push to Telegram and are available via API.

Data source: IBKR TWS via ib_insync (reuses IbkrFeed connection).
Requires tick types 100 (OptionVolume), 101 (OpenInterest), 106 (IV).
"""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════════════
# Data models
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class FlowAnomaly:
    """Single anomaly detected in the options chain."""
    anomaly_type: str        # high_vol_oi | premium_cluster | iv_spike | large_size | delta_skew
    side: str                # "call" or "put"
    strike: float
    score: float             # 0-100 intensity score
    description: str         # human-readable explanation
    volume: int = 0
    open_interest: int = 0
    premium_usd: float = 0   # volume × mid × 100 (notional premium traded)
    iv: float | None = None
    delta: float | None = None


@dataclass
class FlowScanResult:
    """Full scan result for one underlying + expiry."""
    symbol: str
    expiry: str
    underlying_price: float
    scan_time: str
    # Aggregates
    total_call_volume: int = 0
    total_put_volume: int = 0
    total_call_premium: float = 0      # $ premium traded on calls
    total_put_premium: float = 0       # $ premium traded on puts
    put_call_ratio: float = 0          # put_vol / call_vol
    net_delta_flow: float = 0          # positive = bullish, negative = bearish
    # Anomalies
    anomalies: list[FlowAnomaly] = field(default_factory=list)
    # Raw data (for API)
    top_call_strikes: list[dict] = field(default_factory=list)  # top 5 by volume
    top_put_strikes: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        return d


# ═══════════════════════════════════════════════════════════════════════
# Scanner configuration (defaults; overridable via .env later)
# ═══════════════════════════════════════════════════════════════════════

# Volume/OI ratio thresholds
VOL_OI_MODERATE = 3.0    # V/OI > 3 = notable
VOL_OI_HIGH = 5.0        # V/OI > 5 = significant
VOL_OI_EXTREME = 10.0    # V/OI > 10 = very unusual

# Minimum absolute volume to flag (filters out noise from illiquid strikes)
MIN_VOLUME_TO_FLAG = 200

# Premium concentration: flag if single strike > this % of total side premium
PREMIUM_CLUSTER_PCT = 0.20   # 20% of all call or put premium at one strike

# IV deviation: flag if strike IV deviates > this from neighbor average
IV_SPIKE_THRESHOLD = 0.20    # 20% deviation from local IV

# Large order: flag if bid_size or ask_size exceeds this
LARGE_SIZE_THRESHOLD = 100   # 100 contracts sitting on bid/ask

# Net delta: flag if aggregate delta skews heavily one direction
DELTA_SKEW_THRESHOLD = 500   # net delta > ±500 = strong directional conviction


# ═══════════════════════════════════════════════════════════════════════
# Core scanner
# ═══════════════════════════════════════════════════════════════════════

async def scan_options_flow(
    ibkr_feed,
    symbol: str = "SPX",
    underlying_price: float | None = None,
    strike_pct_band: float = 0.05,   # ±5% (wider than default ±2.5% for flow scanning)
) -> FlowScanResult | None:
    """Run a full options flow scan using IBKR chain data.

    Args:
        ibkr_feed: Connected IbkrFeed instance.
        symbol: "SPX", "SPY", or "XSP".
        underlying_price: Current price of underlying. If None, uses last bar.
        strike_pct_band: Strike range as fraction of price (0.05 = ±5%).

    Returns:
        FlowScanResult with anomalies, or None on failure.
    """
    if not ibkr_feed or not ibkr_feed.connected:
        log.warning("Flow scanner: IBKR not connected")
        return None

    if underlying_price is None:
        log.warning("Flow scanner: no underlying price provided")
        return None

    now = datetime.now(ET)
    log.info("Flow scan starting: %s @ $%.2f (±%.1f%%)",
             symbol, underlying_price, strike_pct_band * 100)

    # Fetch chain with volume + OI (reuses existing method)
    chain = await ibkr_feed.get_options_chain_with_greeks(
        symbol=symbol,
        underlying_price=underlying_price,
        strike_pct_band=strike_pct_band,
    )

    if "error" in chain:
        log.error("Flow scan chain fetch failed: %s", chain["error"])
        return None

    calls = chain.get("calls", [])
    puts = chain.get("puts", [])
    expiry = chain.get("expiry", "unknown")

    if not calls and not puts:
        log.warning("Flow scan: empty chain returned")
        return None

    result = FlowScanResult(
        symbol=symbol,
        expiry=expiry,
        underlying_price=underlying_price,
        scan_time=now.isoformat(),
    )

    # ── Aggregate stats ──────────────────────────────────────────────
    _compute_aggregates(result, calls, puts)

    # ── Detect anomalies ─────────────────────────────────────────────
    _detect_vol_oi_anomalies(result, calls, "call")
    _detect_vol_oi_anomalies(result, puts, "put")
    _detect_premium_clusters(result, calls, "call")
    _detect_premium_clusters(result, puts, "put")
    _detect_iv_spikes(result, calls, "call")
    _detect_iv_spikes(result, puts, "put")
    _detect_large_sizes(result, calls, "call")
    _detect_large_sizes(result, puts, "put")
    _detect_delta_skew(result)

    # ── Top strikes by volume ────────────────────────────────────────
    result.top_call_strikes = sorted(calls, key=lambda x: x.get("volume", 0), reverse=True)[:5]
    result.top_put_strikes = sorted(puts, key=lambda x: x.get("volume", 0), reverse=True)[:5]

    # Sort anomalies by score (highest first)
    result.anomalies.sort(key=lambda a: a.score, reverse=True)

    log.info("Flow scan complete: %d anomalies found, P/C ratio=%.2f, net delta=%.0f",
             len(result.anomalies), result.put_call_ratio, result.net_delta_flow)

    return result


# ═══════════════════════════════════════════════════════════════════════
# Aggregate computation
# ═══════════════════════════════════════════════════════════════════════

def _compute_aggregates(result: FlowScanResult, calls: list[dict], puts: list[dict]):
    """Compute total volumes, premium, P/C ratio, net delta."""
    for row in calls:
        vol = row.get("volume", 0) or 0
        mid = row.get("mid") or row.get("last") or 0
        delta = row.get("delta") or 0
        result.total_call_volume += vol
        result.total_call_premium += vol * (mid or 0) * 100  # × 100 multiplier
        result.net_delta_flow += vol * delta  # call delta is positive

    for row in puts:
        vol = row.get("volume", 0) or 0
        mid = row.get("mid") or row.get("last") or 0
        delta = row.get("delta") or 0
        result.total_put_volume += vol
        result.total_put_premium += vol * (mid or 0) * 100
        result.net_delta_flow += vol * delta  # put delta is negative

    if result.total_call_volume > 0:
        result.put_call_ratio = result.total_put_volume / result.total_call_volume
    else:
        result.put_call_ratio = 999.0 if result.total_put_volume > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════════
# Anomaly detectors
# ═══════════════════════════════════════════════════════════════════════

def _detect_vol_oi_anomalies(result: FlowScanResult, strikes: list[dict], side: str):
    """Flag strikes where Volume/OI ratio is abnormally high."""
    for row in strikes:
        vol = row.get("volume", 0) or 0
        oi = row.get("open_interest", 0) or 0
        strike = row.get("strike", 0)

        if vol < MIN_VOLUME_TO_FLAG:
            continue

        # V/OI ratio (handle zero OI)
        if oi == 0:
            if vol >= MIN_VOLUME_TO_FLAG:
                ratio = vol  # treat as vol/1
            else:
                continue
        else:
            ratio = vol / oi

        if ratio < VOL_OI_MODERATE:
            continue

        # Score: scale 0-100 based on ratio thresholds
        if ratio >= VOL_OI_EXTREME:
            score = min(100, 70 + (ratio - VOL_OI_EXTREME) * 2)
            intensity = "EXTREME"
        elif ratio >= VOL_OI_HIGH:
            score = 50 + (ratio - VOL_OI_HIGH) / (VOL_OI_EXTREME - VOL_OI_HIGH) * 20
            intensity = "HIGH"
        else:
            score = 30 + (ratio - VOL_OI_MODERATE) / (VOL_OI_HIGH - VOL_OI_MODERATE) * 20
            intensity = "MODERATE"

        mid = row.get("mid") or row.get("last") or 0
        premium = vol * (mid or 0) * 100

        result.anomalies.append(FlowAnomaly(
            anomaly_type="high_vol_oi",
            side=side,
            strike=strike,
            score=score,
            description=f"{intensity} V/OI: {vol:,} vol vs {oi:,} OI (ratio {ratio:.1f}×) — "
                        f"${premium:,.0f} premium traded",
            volume=vol,
            open_interest=oi,
            premium_usd=premium,
            iv=row.get("iv"),
            delta=row.get("delta"),
        ))


def _detect_premium_clusters(result: FlowScanResult, strikes: list[dict], side: str):
    """Flag strikes where premium concentration is unusually high."""
    total_premium = sum(
        (r.get("volume", 0) or 0) * ((r.get("mid") or r.get("last") or 0)) * 100
        for r in strikes
    )
    if total_premium <= 0:
        return

    for row in strikes:
        vol = row.get("volume", 0) or 0
        mid = row.get("mid") or row.get("last") or 0
        if vol < MIN_VOLUME_TO_FLAG // 2:
            continue

        premium = vol * mid * 100
        pct = premium / total_premium

        if pct < PREMIUM_CLUSTER_PCT:
            continue

        score = min(100, 40 + pct * 200)  # 20% = 80 score, 30% = 100

        result.anomalies.append(FlowAnomaly(
            anomaly_type="premium_cluster",
            side=side,
            strike=row.get("strike", 0),
            score=score,
            description=f"{pct:.0%} of total {side} premium concentrated here — "
                        f"${premium:,.0f} of ${total_premium:,.0f}",
            volume=vol,
            premium_usd=premium,
            iv=row.get("iv"),
            delta=row.get("delta"),
        ))


def _detect_iv_spikes(result: FlowScanResult, strikes: list[dict], side: str):
    """Flag strikes where IV deviates significantly from neighbors."""
    # Need at least 5 strikes for meaningful comparison
    valid = [(r.get("strike", 0), r.get("iv")) for r in strikes if r.get("iv")]
    if len(valid) < 5:
        return

    ivs = [iv for _, iv in valid]
    median_iv = statistics.median(ivs)
    if median_iv <= 0:
        return

    for row in strikes:
        strike = row.get("strike", 0)
        iv = row.get("iv")
        vol = row.get("volume", 0) or 0
        if iv is None or vol < MIN_VOLUME_TO_FLAG // 2:
            continue

        deviation = abs(iv - median_iv) / median_iv
        if deviation < IV_SPIKE_THRESHOLD:
            continue

        direction = "above" if iv > median_iv else "below"
        score = min(100, 30 + deviation * 200)

        result.anomalies.append(FlowAnomaly(
            anomaly_type="iv_spike",
            side=side,
            strike=strike,
            score=score,
            description=f"IV {iv:.1%} is {deviation:.0%} {direction} chain median "
                        f"({median_iv:.1%}) — {vol:,} vol",
            volume=vol,
            iv=iv,
            delta=row.get("delta"),
        ))


def _detect_large_sizes(result: FlowScanResult, strikes: list[dict], side: str):
    """Flag strikes with unusually large bid/ask sizes (institutional orders)."""
    for row in strikes:
        bid_sz = row.get("bid_size") or 0
        ask_sz = row.get("ask_size") or 0
        strike = row.get("strike", 0)

        max_size = max(bid_sz, ask_sz)
        if max_size < LARGE_SIZE_THRESHOLD:
            continue

        which = "bid" if bid_sz >= ask_sz else "ask"
        score = min(100, 30 + (max_size / LARGE_SIZE_THRESHOLD) * 15)

        mid = row.get("mid") or row.get("last") or 0
        sitting_premium = max_size * mid * 100

        result.anomalies.append(FlowAnomaly(
            anomaly_type="large_size",
            side=side,
            strike=strike,
            score=score,
            description=f"{max_size:,} contracts on {which} @ ${strike:.0f} {side} — "
                        f"${sitting_premium:,.0f} premium sitting",
            volume=row.get("volume", 0) or 0,
            premium_usd=sitting_premium,
            delta=row.get("delta"),
        ))


def _detect_delta_skew(result: FlowScanResult):
    """Flag if net delta flow is heavily skewed (strong directional conviction)."""
    nd = result.net_delta_flow
    if abs(nd) < DELTA_SKEW_THRESHOLD:
        return

    direction = "BULLISH" if nd > 0 else "BEARISH"
    score = min(100, 40 + abs(nd) / DELTA_SKEW_THRESHOLD * 20)

    result.anomalies.append(FlowAnomaly(
        anomaly_type="delta_skew",
        side="aggregate",
        strike=0,
        score=score,
        description=f"Net delta flow is {direction}: {nd:+,.0f} deltas — "
                    f"P/C ratio {result.put_call_ratio:.2f}, "
                    f"call premium ${result.total_call_premium:,.0f} vs "
                    f"put premium ${result.total_put_premium:,.0f}",
    ))


# ═══════════════════════════════════════════════════════════════════════
# Telegram formatting
# ═══════════════════════════════════════════════════════════════════════

def format_flow_alert(result: FlowScanResult, max_anomalies: int = 8) -> str:
    """Format scan result for Telegram push."""
    lines = []

    # Header
    bias = "🟢 BULLISH" if result.net_delta_flow > 0 else "🔴 BEARISH"
    lines.append(f"🔍 OPTIONS FLOW SCAN — {result.symbol}")
    lines.append(f"Price: ${result.underlying_price:,.2f} | Expiry: {result.expiry}")
    lines.append(f"Bias: {bias} (net Δ {result.net_delta_flow:+,.0f})")
    lines.append(f"P/C: {result.put_call_ratio:.2f} | "
                 f"Call $: ${result.total_call_premium:,.0f} | "
                 f"Put $: ${result.total_put_premium:,.0f}")
    lines.append("")

    if not result.anomalies:
        lines.append("✅ No unusual activity detected.")
        return "\n".join(lines)

    # Top anomalies
    lines.append(f"⚠️ {len(result.anomalies)} anomalies detected:")
    lines.append("")
    for a in result.anomalies[:max_anomalies]:
        icon = {
            "high_vol_oi": "📊",
            "premium_cluster": "💰",
            "iv_spike": "📈",
            "large_size": "🐋",
            "delta_skew": "🧭",
        }.get(a.anomaly_type, "❗")

        side_label = a.side.upper() if a.side != "aggregate" else ""
        strike_label = f" ${a.strike:.0f}" if a.strike > 0 else ""
        lines.append(f"{icon} [{a.score:.0f}] {side_label}{strike_label}")
        lines.append(f"   {a.description}")
        lines.append("")

    # Top volume strikes
    if result.top_call_strikes:
        top_c = result.top_call_strikes[0]
        lines.append(f"📞 Top call: ${top_c['strike']:.0f} — {top_c.get('volume', 0):,} vol")
    if result.top_put_strikes:
        top_p = result.top_put_strikes[0]
        lines.append(f"📉 Top put: ${top_p['strike']:.0f} — {top_p.get('volume', 0):,} vol")

    return "\n".join(lines)


def format_flow_summary_short(result: FlowScanResult) -> str:
    """One-line summary for EOD or dashboard."""
    n = len(result.anomalies)
    high = sum(1 for a in result.anomalies if a.score >= 70)
    bias = "Bull" if result.net_delta_flow > 0 else "Bear"
    return (f"Flow: {n} anomalies ({high} high), P/C {result.put_call_ratio:.2f}, "
            f"net Δ {result.net_delta_flow:+,.0f} ({bias})")
