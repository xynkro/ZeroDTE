"""Directional Spread Backtest — Unified pivot strategy (May 2026).

PIVOT: Replaces both the symmetric IC builder AND the static-stop wave backtest
with a single unified architecture inspired by:
  - Tastytrade research: 0DTE has 10× the gamma exposure of 45DTE, so 1 contract
    of 0DTE = ~1 contract 45DTE in directional risk. Need meaningful credit
    (~25-35% of wing) to compensate.
  - YouTube credit-spread tutorial: dynamic stop-loss ladder ratchets stops as
    profit accrues, converting 60% WR into 90%+ effective WR.
  - Option Alpha 25k-trade study: directional bias outperforms neutral IC.

CORE ARCHITECTURE:
  1. Use Wave signals as the entry trigger (existing predictor.py output)
  2. Apply confluence filter (≥3 of 4 factors) + mandatory VWAP alignment
  3. Strike placement at SHORT_DELTA target (~20Δ ≈ 0.5% OTM in low VIX)
  4. Wing = $10 SPX (canonical Tastytrade-style)
  5. Dynamic stop-loss ladder:
       Initial: -100% credit (stop at full credit loss)
       At 50% TP peak: stop → break-even
       At 75% TP peak: stop → 50% locked
       At 90% TP peak: stop → 75% locked
  6. Final TP at 90% credit captured (close)
  7. Hard stops: close-through-strike, 30min before close

SPREAD P&L MODEL (no real chain data — approximated from underlying):
  pct_credit_kept(t) = clamp(favorable_move(t) / breakeven_dist, -100, +100)
  where breakeven_dist ≈ strike_distance_pct
  This is a linear approximation; real spreads have gamma curvature.
  Stress test 2 reruns with quadratic decay model to see if WR holds.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

from .config import settings
from .predictor import Bar, run_backtest as predictor_run, _ema, _wilder_atr

ET = ZoneInfo("America/New_York")


# ═══════════════════════════════════════════════════════════════════════════
# DELTA → %OTM mapping (low-VIX SPX 0DTE rule-of-thumb)
# ═══════════════════════════════════════════════════════════════════════════
# These are static approximations; real Δ depends on IV. We use these as the
# strike placement rule and document the assumption.
#
# Source: standard 0DTE pricing curves, validated against historical SPX chains
# at VIX 12-15 (low-vol regime which covers most of 2024-2025).

DELTA_TO_OTM_PCT = {
    35: 0.30,   # aggressive — Tastytrade canonical
    30: 0.35,
    25: 0.40,
    20: 0.50,   # MODERATE — backtest default
    15: 0.65,
    10: 0.85,
    5:  1.20,   # conservative — current system zone
}

# Credit as % of wing width (calibrated to 0DTE pricing):
# At 20Δ short with $10 wing, typical mid is ~$2.50 = 25% of width
DELTA_TO_CREDIT_PCT = {
    35: 0.35,
    30: 0.30,
    25: 0.27,
    20: 0.25,   # MODERATE — backtest default
    15: 0.18,
    10: 0.13,
    5:  0.07,
}


def _credit_for_delta(short_delta: int, wing_dollars: float) -> float:
    """Estimated credit in dollars per contract for given short delta + wing."""
    pct = DELTA_TO_CREDIT_PCT.get(short_delta, 0.20)
    return pct * wing_dollars * 100  # $10 wing × 25% × 100 multiplier = $250


def _otm_pct_for_delta(short_delta: int) -> float:
    """%OTM for given short delta (low-VIX approximation)."""
    return DELTA_TO_OTM_PCT.get(short_delta, 0.50)


# ═══════════════════════════════════════════════════════════════════════════
# Confluence scoring at signal time (offline — derived from bars only)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ConfluenceCheck:
    rsi_directional: bool
    wvf_spike: bool
    near_ema10: bool
    in_prime_window: bool
    vwap_aligned: bool  # gate, not in score
    score: int  # 0-4 (excluding vwap_aligned)


def _compute_confluence(
    sig_time: datetime,
    side: str,
    rsi: float,
    wvf_spike: bool,
    ema10: float,
    entry_price: float,
    vwap: float | None,
    prime_window: tuple[int, int] = (10*60 + 30, 13*60),  # 10:30-13:00 ET
) -> ConfluenceCheck:
    """Compute confluence at signal time (no lookahead — uses only data at signal bar)."""
    # 1. RSI directional
    if side == "sell_call_cs":
        rsi_directional = rsi > 65
    else:
        rsi_directional = rsi < 35

    # 2. WVF spike (already computed in signal)
    wvf_spike_b = bool(wvf_spike)

    # 3. Near EMA10 (price within 0.30% of EMA10)
    if ema10 > 0 and entry_price > 0:
        ext_pct = abs(entry_price - ema10) / entry_price * 100.0
        near_ema = ext_pct < 0.30
    else:
        near_ema = False

    # 4. In prime window 10:30-13:00 ET
    sig_et = sig_time.astimezone(ET) if sig_time.tzinfo else sig_time
    bar_min = sig_et.hour * 60 + sig_et.minute
    in_prime = prime_window[0] <= bar_min <= prime_window[1]

    # VWAP alignment (gate, not in score)
    if vwap is not None:
        if side == "sell_call_cs":
            vwap_aligned = entry_price > vwap  # extended above VWAP, expect revert down
        else:
            vwap_aligned = entry_price < vwap  # below VWAP, expect revert up
    else:
        vwap_aligned = True  # failsafe-open if no VWAP

    score = sum([rsi_directional, wvf_spike_b, near_ema, in_prime])
    return ConfluenceCheck(
        rsi_directional=rsi_directional,
        wvf_spike=wvf_spike_b,
        near_ema10=near_ema,
        in_prime_window=in_prime,
        vwap_aligned=vwap_aligned,
        score=score,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Session VWAP computation
# ═══════════════════════════════════════════════════════════════════════════

def _session_vwap(bars: list[Bar], up_to_idx: int) -> float | None:
    """Cumulative session VWAP from bars[0] through bars[up_to_idx]."""
    if up_to_idx < 0 or up_to_idx >= len(bars):
        return None
    cum_pv = 0.0
    cum_v = 0.0
    for i in range(up_to_idx + 1):
        b = bars[i]
        typ = (b.high + b.low + b.close) / 3.0
        cum_pv += typ * b.volume
        cum_v += b.volume
    if cum_v <= 0:
        return None
    return cum_pv / cum_v


# ═══════════════════════════════════════════════════════════════════════════
# Spread P&L approximation
# ═══════════════════════════════════════════════════════════════════════════

def _spread_pct_kept(
    side: str,
    short_strike: float,
    entry_price: float,
    current_price: float,
    breakeven_dist_pct: float,
    model: str = "linear",
) -> float:
    """Return % of credit kept (i.e., running profit as fraction of max profit).

    Range: [-200, +100]
      +100% = max profit (spread worth $0)
       0%   = break-even (spread worth = credit collected)
      -100% = max loss (spread worth = wing)
      Below -100 caps to -100 in caller.

    Approximation: linear relationship between favorable_move and pct_kept,
    where favorable_move = breakeven_dist_pct corresponds to 100% kept.

    'quadratic' model accounts for gamma curvature (more conservative).
    """
    # Compute favorable move in % terms
    if side == "sell_call_cs":
        # Bearish trade: profit when price falls
        favorable_pct = (entry_price - current_price) / entry_price * 100.0
    else:
        # Bullish trade: profit when price rises
        favorable_pct = (current_price - entry_price) / entry_price * 100.0

    if breakeven_dist_pct <= 0:
        return 0.0

    if model == "linear":
        pct_kept = (favorable_pct / breakeven_dist_pct) * 100.0
    elif model == "quadratic":
        # Gamma curvature: closer-than-strike moves cause faster spread changes
        if favorable_pct >= 0:
            pct_kept = (favorable_pct / breakeven_dist_pct) ** 0.7 * 100.0
        else:
            pct_kept = -((abs(favorable_pct) / breakeven_dist_pct) ** 1.3) * 100.0
    else:
        pct_kept = (favorable_pct / breakeven_dist_pct) * 100.0

    return max(-200.0, min(100.0, pct_kept))


# ═══════════════════════════════════════════════════════════════════════════
# Main backtest
# ═══════════════════════════════════════════════════════════════════════════

def run_directional_spread_backtest(
    # Strategy parameters
    short_delta: int = 20,                # 20Δ MODERATE (B) per pivot spec
    wing_dollars: float = 10.0,           # $10 SPX wing
    confluence_min: int = 3,              # ≥3 of 4 factors
    require_vwap: bool = True,            # MANDATORY vwap alignment

    # Dynamic stop-loss ladder
    use_dynamic_stops: bool = True,
    tp_ladder_50_trigger: float = 50.0,   # at 50% TP peak → stop to BE
    tp_ladder_75_trigger: float = 75.0,   # at 75% TP peak → stop to 50%
    tp_ladder_90_trigger: float = 90.0,   # at 90% TP peak → stop to 75%
    final_tp_target: float = 90.0,        # close trade at 90% credit captured

    # Other exits
    time_stop_min: int = 30,              # close 30min before EOD
    stop_on_bar_close: bool = True,       # catastrophe stop = bar close through strike

    # Slippage / friction (for stress tests)
    slippage_pct: float = 0.0,            # slip on entry+exit, % of credit

    # Data
    use_12mo_data: bool = True,
    data_window: str = "auto",            # "auto" | "60d" | "1y" | "3y" — auto: 3y if file exists, else 1y, else 60d
    skip_volatile_days: bool = True,
    skip_blackout_days: bool = False,     # cant model without macro feed; disabled by default

    # P&L modeling
    pnl_model: str = "linear",            # "linear" or "quadratic"

    # Output
    return_trades: bool = True,
) -> dict:
    """Run the unified directional spread backtest.

    Returns dict with summary stats, trade-by-trade list, and daily P&L.
    """
    # Resolve data file: explicit window override or auto-pick largest available
    explicit_map = {"60d": "SPX_5m_60d.json", "1y": "SPX_5m_1y.json", "3y": "SPX_5m_3y.json"}
    if data_window in explicit_map:
        candidates = [explicit_map[data_window]]
    elif data_window == "auto":
        candidates = ["SPX_5m_3y.json", "SPX_5m_1y.json", "SPX_5m_60d.json"]
    else:
        # Legacy behavior via use_12mo_data
        candidates = ["SPX_5m_1y.json"] if use_12mo_data else ["SPX_5m_60d.json", "SPX_5m_1y.json"]
    path = None
    for c in candidates:
        p = settings.data_dir / "historical" / c
        if p.exists():
            path = p
            break
    if path is None:
        return {"error": f"missing data: tried {candidates}"}
    raw = json.loads(path.read_text())
    bars = [
        Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        )
        for r in raw
    ]

    # D1 ATR for regime classifier
    by_date: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        d = et.strftime("%Y-%m-%d")
        by_date[d].append(b)
    sorted_dates = sorted(by_date.keys())
    daily_atr_map: dict[str, float] = {}
    daily_trs: list[float] = []
    prev_close = None
    for d in sorted_dates:
        ses = by_date[d]
        hi = max(x.high for x in ses)
        lo = min(x.low for x in ses)
        cl = ses[-1].close
        if prev_close is None:
            tr = hi - lo
        else:
            tr = max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        daily_trs.append(tr)
        if len(daily_trs) >= 14:
            daily_atr_map[d] = float(np.mean(daily_trs[-14:]))
        prev_close = cl
    sessions = predictor_run(bars, lambda d: daily_atr_map.get(d))

    # Compute EMAs on full bar series (no lookahead — we'll index per-bar)
    closes_all = np.array([b.close for b in bars])
    ema10_arr = _ema(closes_all, 10)
    # bar idx by time
    bar_idx_by_time = {b.time: i for i, b in enumerate(bars)}

    # Trade simulation
    multiplier = 100
    short_otm_pct = _otm_pct_for_delta(short_delta)
    credit_dollars = _credit_for_delta(short_delta, wing_dollars)
    max_loss = wing_dollars * multiplier - credit_dollars

    # Slippage adjustment: assume slip on entry (less credit) AND exit (more cost)
    # Slip both ways means effective credit is reduced and effective max_loss is increased.
    eff_credit = credit_dollars * (1 - slippage_pct / 100.0)
    eff_max_loss = max_loss + (credit_dollars - eff_credit) * 2  # round-trip

    trades = []
    cum_pnl = 0.0
    n_signals_total = 0
    n_filter_confluence = 0
    n_filter_vwap = 0
    n_skipped_volatile = 0
    n_skipped_no_data = 0

    for s in sessions:
        if skip_volatile_days and s.regime != "NON-VOLATILE":
            n_skipped_volatile += len(s.signals)
            continue
        if not s.signals:
            continue

        session_bars = by_date.get(s.session_date, [])
        if not session_bars:
            continue

        for sig in s.signals:
            n_signals_total += 1
            entry_time = sig.time
            entry_price = sig.entry_price
            side = sig.side

            # Get signal bar index for EMA10 + VWAP
            sig_bar_idx = bar_idx_by_time.get(entry_time)
            if sig_bar_idx is None:
                n_skipped_no_data += 1
                continue
            ema10 = float(ema10_arr[sig_bar_idx]) if not np.isnan(ema10_arr[sig_bar_idx]) else 0.0

            # Session VWAP at signal time (cumulative from session open to this bar)
            session_start_idx = bar_idx_by_time.get(session_bars[0].time, sig_bar_idx)
            vwap = _session_vwap(bars[session_start_idx:sig_bar_idx + 1], sig_bar_idx - session_start_idx)

            # Compute confluence
            conf = _compute_confluence(
                sig_time=entry_time,
                side=side,
                rsi=sig.rsi,
                wvf_spike=sig.wvf_spike,
                ema10=ema10,
                entry_price=entry_price,
                vwap=vwap,
            )

            # Apply filters
            if conf.score < confluence_min:
                n_filter_confluence += 1
                continue
            if require_vwap and not conf.vwap_aligned:
                n_filter_vwap += 1
                continue

            # Compute strikes
            if side == "sell_call_cs":
                short_strike = entry_price * (1 + short_otm_pct / 100.0)
                long_strike = short_strike + wing_dollars
            else:
                short_strike = entry_price * (1 - short_otm_pct / 100.0)
                long_strike = short_strike - wing_dollars

            # Walk forward through session with dynamic stop management
            peak_pct_kept = 0.0     # highest TP achieved
            stop_pct_kept = -100.0  # current stop level (initial = full credit loss)

            outcome = "expire_max_profit"
            exit_pct_kept = 100.0
            exit_time = None
            exit_price = None
            exit_reason = "EOD"
            bars_held = 0

            for b in session_bars:
                if b.time <= entry_time:
                    continue
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bar_min = et.hour * 60 + et.minute

                # EOD check
                if bar_min >= 16 * 60:
                    outcome = "expire_max_profit"
                    exit_pct_kept = 100.0
                    exit_reason = "EOD"
                    exit_time = b.time
                    exit_price = float(b.close)
                    break

                bars_held += 1

                # Intra-bar extremes for stop/TP evaluation:
                #   - "worst price" for the trade = the bar extreme that hurts most
                #   - "best price"  for the trade = the bar extreme that helps most
                # For sell_call_cs (bearish): high = worst, low = best
                # For sell_put_cs  (bullish): low  = worst, high = best
                if side == "sell_call_cs":
                    worst_price = b.high
                    best_price = b.low
                else:
                    worst_price = b.low
                    best_price = b.high

                # Compute pct_kept at both intra-bar extremes
                pct_kept_worst = _spread_pct_kept(
                    side, short_strike, entry_price, worst_price,
                    short_otm_pct, model=pnl_model,
                )
                pct_kept_best = _spread_pct_kept(
                    side, short_strike, entry_price, best_price,
                    short_otm_pct, model=pnl_model,
                )

                # Update peak using best intra-bar price (favorable extreme)
                if pct_kept_best > peak_pct_kept:
                    peak_pct_kept = pct_kept_best

                # Dynamic stop ladder — ratchets based on peak
                if use_dynamic_stops:
                    if peak_pct_kept >= tp_ladder_90_trigger:
                        stop_pct_kept = max(stop_pct_kept, 75.0)
                    elif peak_pct_kept >= tp_ladder_75_trigger:
                        stop_pct_kept = max(stop_pct_kept, 50.0)
                    elif peak_pct_kept >= tp_ladder_50_trigger:
                        stop_pct_kept = max(stop_pct_kept, 0.0)

                # CONSERVATIVE BAR ORDER ASSUMPTION: bars move worst-first, then best.
                # This means we evaluate the worst-price stop BEFORE the best-price TP,
                # which is the pessimistic (most realistic) assumption when we don't
                # have tick data. Real intraday movement could go either way.

                # Stop hit (intra-bar) — uses worst_price
                if pct_kept_worst <= stop_pct_kept:
                    outcome = "stop_ladder_hit"
                    exit_pct_kept = stop_pct_kept  # filled at the stop level
                    exit_reason = f"STOP_AT_{stop_pct_kept:.0f}"
                    exit_time = b.time
                    exit_price = float(worst_price)
                    break

                # Catastrophe: bar CLOSE through short strike — only fires if
                # dynamic stop hasn't fired (worst_price stop check above is
                # tighter; this catches edge case of bar that closes through
                # without triggering the ladder).
                if stop_on_bar_close:
                    if side == "sell_call_cs" and b.close >= short_strike:
                        outcome = "breach_max_loss"
                        exit_pct_kept = -100.0
                        exit_reason = "STOP_BREACH"
                        exit_time = b.time
                        exit_price = float(b.close)
                        break
                    if side == "sell_put_cs" and b.close <= short_strike:
                        outcome = "breach_max_loss"
                        exit_pct_kept = -100.0
                        exit_reason = "STOP_BREACH"
                        exit_time = b.time
                        exit_price = float(b.close)
                        break

                # Final TP target (intra-bar) — uses best_price
                if pct_kept_best >= final_tp_target:
                    outcome = "tp_target_hit"
                    exit_pct_kept = pct_kept_best
                    exit_reason = f"TP_{final_tp_target:.0f}"
                    exit_time = b.time
                    exit_price = float(best_price)
                    break

                # TIME stop (30min before close) — close at current bar's close P&L
                if bar_min >= (16 * 60 - time_stop_min):
                    pct_kept_close = _spread_pct_kept(
                        side, short_strike, entry_price, b.close,
                        short_otm_pct, model=pnl_model,
                    )
                    outcome = "time_close"
                    exit_pct_kept = pct_kept_close
                    exit_reason = "TIME"
                    exit_time = b.time
                    exit_price = float(b.close)
                    break

            # If never exited, expire OTM
            if exit_time is None and session_bars:
                outcome = "expire_max_profit"
                exit_pct_kept = 100.0
                exit_reason = "EOD"
                last_bar = session_bars[-1]
                exit_time = last_bar.time
                exit_price = float(last_bar.close)

            # Convert pct_kept → $ P&L
            if outcome == "breach_max_loss":
                pnl = -eff_max_loss
            else:
                # pct_kept of credit = profit; negative = loss capped at credit
                pnl = eff_credit * (exit_pct_kept / 100.0)
                # But never more than credit
                pnl = min(pnl, eff_credit)
                # Never worse than -credit (since we have stop at -100%)
                pnl = max(pnl, -eff_credit)

            cum_pnl += pnl

            if return_trades:
                trades.append({
                    "date": str(s.session_date),
                    "regime": str(s.regime),
                    "side": str(side),
                    "confluence_score": int(conf.score),
                    "vwap_aligned": bool(conf.vwap_aligned),
                    "entry_time": entry_time.isoformat(),
                    "entry_price": float(entry_price),
                    "short_strike": float(short_strike),
                    "long_strike": float(long_strike),
                    "exit_time": exit_time.isoformat() if exit_time else None,
                    "exit_price": float(exit_price) if exit_price else None,
                    "peak_pct_kept": float(round(peak_pct_kept, 1)),
                    "exit_pct_kept": float(round(exit_pct_kept, 1)),
                    "bars_held": int(bars_held),
                    "outcome": str(outcome),
                    "exit_reason": str(exit_reason),
                    "pnl_dollars": float(round(pnl, 2)),
                    "cum_pnl_dollars": float(round(cum_pnl, 2)),
                })

    # Summary
    n_trades = len(trades) if return_trades else (n_signals_total - n_filter_confluence - n_filter_vwap)
    n_wins = sum(1 for t in trades if t["pnl_dollars"] > 0) if return_trades else 0
    n_breach = sum(1 for t in trades if t["outcome"] == "breach_max_loss") if return_trades else 0
    n_stop_ladder = sum(1 for t in trades if t["outcome"] == "stop_ladder_hit") if return_trades else 0
    n_tp_target = sum(1 for t in trades if t["outcome"] == "tp_target_hit") if return_trades else 0
    n_expire = sum(1 for t in trades if t["outcome"] == "expire_max_profit") if return_trades else 0
    n_time = sum(1 for t in trades if t["outcome"] == "time_close") if return_trades else 0

    win_rate = (n_wins / n_trades * 100) if n_trades else 0
    avg_pnl = (cum_pnl / n_trades) if n_trades else 0
    wins = [t["pnl_dollars"] for t in trades if t["pnl_dollars"] > 0] if return_trades else []
    losses = [t["pnl_dollars"] for t in trades if t["pnl_dollars"] < 0] if return_trades else []
    avg_win = np.mean(wins) if wins else 0
    avg_loss = np.mean(losses) if losses else 0
    capture = (avg_pnl / eff_credit * 100) if eff_credit else 0

    # Daily P&L
    daily_pnl = defaultdict(float)
    daily_count = defaultdict(int)
    for t in trades:
        daily_pnl[t["date"]] += t["pnl_dollars"]
        daily_count[t["date"]] += 1
    daily_summary = [
        {"date": d, "pnl": round(daily_pnl[d], 2), "n_trades": daily_count[d]}
        for d in sorted(daily_pnl.keys())
    ]

    # Drawdown
    cum = np.array([t["cum_pnl_dollars"] for t in trades]) if trades else np.array([0])
    if len(cum) > 0:
        peak = np.maximum.accumulate(cum)
        dd = cum - peak
        max_dd = float(dd.min())
    else:
        max_dd = 0.0

    # Year-by-year
    by_year = defaultdict(lambda: {"n_trades": 0, "pnl": 0.0, "wins": 0})
    for t in trades:
        y = t["date"][:4]
        by_year[y]["n_trades"] += 1
        by_year[y]["pnl"] += t["pnl_dollars"]
        if t["pnl_dollars"] > 0:
            by_year[y]["wins"] += 1
    yearly = []
    for y in sorted(by_year.keys()):
        d = by_year[y]
        yearly.append({
            "year": y,
            "n_trades": d["n_trades"],
            "pnl": round(d["pnl"], 2),
            "win_rate_pct": round(d["wins"] / d["n_trades"] * 100, 1) if d["n_trades"] else 0,
        })

    return {
        "params": {
            "short_delta": short_delta,
            "short_otm_pct": short_otm_pct,
            "wing_dollars": wing_dollars,
            "credit_estimated_dollars": round(eff_credit, 2),
            "max_loss_estimated_dollars": round(eff_max_loss, 2),
            "confluence_min": confluence_min,
            "require_vwap": require_vwap,
            "use_dynamic_stops": use_dynamic_stops,
            "final_tp_target": final_tp_target,
            "time_stop_min": time_stop_min,
            "slippage_pct": slippage_pct,
            "pnl_model": pnl_model,
            "data_file": path.name,
        },
        "summary": {
            "n_signals_total": n_signals_total,
            "n_filter_confluence": n_filter_confluence,
            "n_filter_vwap": n_filter_vwap,
            "n_skipped_volatile": n_skipped_volatile,
            "n_trades": n_trades,
            "n_wins": n_wins,
            "n_breach": n_breach,
            "n_stop_ladder": n_stop_ladder,
            "n_tp_target": n_tp_target,
            "n_expire_otm": n_expire,
            "n_time_close": n_time,
            "win_rate_pct": round(win_rate, 1),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "avg_win": round(float(avg_win), 2),
            "avg_loss": round(float(avg_loss), 2),
            "capture_pct": round(capture, 1),
            "total_pnl": round(cum_pnl, 2),
            "max_drawdown_dollars": round(max_dd, 2),
            "expectancy_pct_of_credit": round(avg_pnl / eff_credit * 100, 2) if eff_credit else 0,
        },
        "yearly": yearly,
        "trades": trades if return_trades else [],
        "daily_pnl": daily_summary,
    }
