"""Backtest endpoints — both iron-condor (one trade per day) and wave-trading
(multiple intraday entries on indicator signals).

For each strategy:
  - Entry uses ONLY data available at entry time (no lookahead bias)
  - Exit walks forward through subsequent bars to determine outcome
  - Credit is approximated as % of wing (historical 0DTE chain unavailable)
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from .config import settings
from .predictor import Bar, run_backtest as predictor_run

ET = ZoneInfo("America/New_York")


# Approximate IC credit as % of wing width (heuristic since no historical chain).
# 8-delta short strike on SPX 0DTE typically collects $0.40-0.80 on $5 wing.
# Default 12% = $0.60 per side, so $1.20 total credit per IC, $3.80 max-loss.
DEFAULT_CREDIT_PCT_PER_SIDE = 12.0  # %; user can override via query param


def _wilder_atr(bars: list[Bar], length: int = 14) -> dict[str, float]:
    """Compute D1 ATR for each session date so the regime classifier has a value.
    Returns {YYYY-MM-DD: atr_value}.
    """
    by_date: dict[str, list[Bar]] = {}
    for b in bars:
        d = b.time.strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)
    sorted_dates = sorted(by_date.keys())
    daily = []
    for d in sorted_dates:
        ses = by_date[d]
        daily.append({
            "date": d,
            "high": max(x.high for x in ses),
            "low": min(x.low for x in ses),
            "close": ses[-1].close,
        })
    n = len(daily)
    trs = [daily[0]["high"] - daily[0]["low"]]
    for i in range(1, n):
        trs.append(max(
            daily[i]["high"] - daily[i]["low"],
            abs(daily[i]["high"] - daily[i - 1]["close"]),
            abs(daily[i]["low"] - daily[i - 1]["close"]),
        ))
    atr_vals = [float("nan")] * n
    if n > length:
        atr_vals[length] = float(np.mean(trs[1:length + 1]))
        for i in range(length + 1, n):
            atr_vals[i] = (atr_vals[i - 1] * (length - 1) + trs[i]) / length
    out = {}
    for i, d in enumerate(daily):
        if i > 0 and not np.isnan(atr_vals[i - 1]):
            out[d["date"]] = atr_vals[i - 1]
    return out


def run_iron_condor_backtest(
    credit_pct_per_side: float = DEFAULT_CREDIT_PCT_PER_SIDE,
    wing_width_spx: float = 5.0,
    skip_volatile_days: bool = True,
    instrument: str = "XSP",
    strike_placement: str = "projected",     # "projected" or "distance_pct"
    strike_distance_pct: float = 0.0,        # if strike_placement="distance_pct", X% OTM from obs-end close
) -> dict:
    """Returns session outcomes + summary stats for the iron-condor strategy.

    strike_placement:
      "projected"     — place short strikes at projected day high / low
                        (uses our indicator's volatility-based forecast)
      "distance_pct"  — place short strikes at obs_end_close ± strike_distance_pct
                        (matches tastytrade-style 'X% OTM' canonical placements)

    Suggested distance_pct values:
      0.5% — very aggressive, ~25-30Δ short, ~50% wing credit
      1.0% — moderate, ~15-20Δ short, ~30% wing credit (tastytrade '1/3 wing')
      1.5% — conservative, ~8-12Δ short, ~15-20% wing credit
      2.0% — very conservative, ~5Δ short, ~8-12% wing credit
    """
    path = settings.data_dir / "historical" / "SPX_5m_60d.json"
    if not path.exists():
        return {"error": f"missing data: {path}"}

    raw = json.loads(path.read_text())
    bars = [
        Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        )
        for r in raw
    ]

    atr_map = _wilder_atr(bars, 14)
    sessions = predictor_run(bars, lambda d: atr_map.get(d))

    # Per-session IC outcome
    multiplier = 100  # all three (XSP, SPX, SPY) are 100x
    # XSP/SPY use 1/10 SPX scale; their wing width is also 5pt for XSP, $1 for SPY
    if instrument == "XSP":
        scale = 0.1
        wing = 5.0
    elif instrument == "SPY":
        scale = 0.1
        wing = 1.0
    else:  # SPX
        scale = 1.0
        wing = wing_width_spx

    # Credit per side, in $ (per share × multiplier)
    # Approximation: credit_pct of wing on SPX scale gives the per-side $ credit
    # For XSP/SPY that's already at 1/10 scale so use scaled wing
    credit_per_side_dollars = (credit_pct_per_side / 100.0) * wing * multiplier
    credit_total_per_ic = credit_per_side_dollars * 2  # call + put leg
    max_loss_per_ic = wing * multiplier - credit_total_per_ic
    # (if both legs breach in same session — extremely rare for IC; usually only one wing)
    # Actually IC max loss = wing × multiplier - total_credit (only ONE wing breaches at most)

    outcomes = []
    cum_pnl = 0.0
    n_total = 0
    n_traded = 0
    n_both_held = 0
    n_call_breach = 0
    n_put_breach = 0
    n_volatile_skipped = 0
    n_no_data = 0

    for s in sessions:
        n_total += 1
        deployed = s.regime == "NON-VOLATILE" or not skip_volatile_days
        if not deployed:
            n_volatile_skipped += 1
            outcomes.append({
                "date": s.session_date, "regime": s.regime,
                "deployed": False,
                "skipped_reason": "volatile_day",
                "obs_high": s.obs_high, "obs_low": s.obs_low,
                "proj_high": s.proj_high, "proj_low": s.proj_low,
                "session_high": s.session_high, "session_low": s.session_low,
                "high_held": None, "low_held": None,
                "outcome": "skip",
                "pnl_dollars": 0.0,
                "cum_pnl_dollars": cum_pnl,
            })
            continue
        if s.proj_high is None or s.proj_low is None:
            n_no_data += 1
            outcomes.append({
                "date": s.session_date, "regime": s.regime,
                "deployed": False, "skipped_reason": "no_projection",
                "outcome": "skip", "pnl_dollars": 0.0,
                "cum_pnl_dollars": cum_pnl,
            })
            continue

        n_traded += 1
        # Determine actual short strikes used for this backtest
        if strike_placement == "distance_pct" and strike_distance_pct > 0:
            # Place strikes ±X% from the observation-end close (= obs_high if cleaner;
            # for 0DTE we anchor at the close that ends observation window)
            anchor = (s.obs_high + s.obs_low) / 2.0  # midpoint of obs range as price proxy
            short_call_strike = anchor * (1.0 + strike_distance_pct / 100.0)
            short_put_strike  = anchor * (1.0 - strike_distance_pct / 100.0)
        else:
            short_call_strike = s.proj_high
            short_put_strike  = s.proj_low

        # Coerce all to Python native (numpy bools/floats break JSON encoder)
        high_held = bool(s.session_high < short_call_strike)
        low_held = bool(s.session_low > short_put_strike)
        both_held = high_held and low_held

        if both_held:
            n_both_held += 1
            pnl = credit_total_per_ic
            outcome = "max_profit_both_held"
        else:
            if not high_held:
                n_call_breach += 1
                outcome = "call_wing_breached"
            else:
                n_put_breach += 1
                outcome = "put_wing_breached"
            pnl = -max_loss_per_ic

        cum_pnl += pnl
        outcomes.append({
            "date": s.session_date, "regime": s.regime,
            "deployed": True,
            "obs_high": float(s.obs_high) if s.obs_high is not None else None,
            "obs_low":  float(s.obs_low)  if s.obs_low  is not None else None,
            "proj_high": float(s.proj_high) if s.proj_high is not None else None,
            "proj_low":  float(s.proj_low)  if s.proj_low  is not None else None,
            "short_call_strike": float(short_call_strike),
            "short_put_strike":  float(short_put_strike),
            "session_high": float(s.session_high),
            "session_low":  float(s.session_low),
            "high_held": high_held, "low_held": low_held,
            "outcome": outcome,
            "pnl_dollars": float(pnl),
            "cum_pnl_dollars": float(cum_pnl),
        })

    win_rate = (n_both_held / n_traded * 100) if n_traded else 0
    avg_pnl = (cum_pnl / n_traded) if n_traded else 0

    return {
        "params": {
            "credit_pct_per_side": credit_pct_per_side,
            "wing_width_spx": wing_width_spx,
            "skip_volatile_days": skip_volatile_days,
            "instrument": instrument,
            "wing_width_used": wing,
            "credit_per_side_dollars": round(credit_per_side_dollars, 2),
            "credit_total_per_ic": round(credit_total_per_ic, 2),
            "max_loss_per_ic": round(max_loss_per_ic, 2),
            "multiplier": multiplier,
            "strike_placement": strike_placement,
            "strike_distance_pct": strike_distance_pct,
            "explainer": "IC strikes placed at 09:45 ET using observation-window data only "
                         "(no lookahead). Outcome = walk-forward bar check until 16:00 ET. "
                         "Both bounds held → max profit; either breached → max loss. "
                         "Credit estimated from credit_pct (historical chain unavailable).",
        },
        "summary": {
            "n_sessions_total": n_total,
            "n_sessions_traded": n_traded,
            "n_skipped_volatile": n_volatile_skipped,
            "n_skipped_no_data": n_no_data,
            "n_both_held": n_both_held,
            "n_call_wing_breached": n_call_breach,
            "n_put_wing_breached": n_put_breach,
            "win_rate_pct": round(win_rate, 1),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "total_pnl": round(cum_pnl, 2),
            "expectancy_pct": round(100 * avg_pnl / max(max_loss_per_ic, 1e-9), 2)
                if n_traded else 0,
        },
        "outcomes": outcomes,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Wave backtest — multiple intraday entries on indicator signals
# ═══════════════════════════════════════════════════════════════════════════

def run_wave_backtest(
    credit_pct_per_side: float = 12.0,    # Phase 3 default: 12% = 1.5% OTM canonical
    wing_width_spx: float = 5.0,
    instrument: str = "XSP",
    strike_distance_pct: float = 1.5,     # Phase 3: 1.5% OTM (canonical 10-15Δ)
    profit_target_pct: float = 75.0,      # close at 75% credit kept (= 25% of credit remaining)
    favorable_move_pct: float = 0.3,      # underlying must move 0.3% favorably to trigger TP
    stop_loss_breach: bool = True,
    stop_on_close: bool = True,           # Phase 3: STOP only on bar close through strike (canonical)
    time_stop_min: int = 30,              # Phase 3: 30min buffer (was 15)
    skip_volatile_days: bool = True,
    use_12mo_data: bool = True,           # Phase 3: default to 12mo (includes tail events)
) -> dict:
    """Wave backtest: each indicator signal = one SINGLE-SIDE credit spread trade.

    Per signal:
      Entry  : at signal bar's close
      Exit (priority STOP > TP > TIME > EOD):
        STOP — Phase 3: bar.close through short strike. Legacy: intra-bar wick.
               Loss = wing × multiplier - credit (real max loss)
        TP   — underlying moves favorably by favorable_move_pct
               Profit = credit × (profit_target_pct / 100)
        TIME — within time_stop_min of close (default 30 min before 16:00 ET)
               Estimated 40% credit kept (rough breakeven)
        EOD  — at/past 16:00 ET, expire OTM
               Full credit kept (max profit)

    PHASE 3 FIX (2026-05-09):
      - credit_total = credit_per_side (was: × 2 — modeled IC, not wave!)
      - max_loss = wing × multiplier - credit (single-side, not both legs)
      - Default strike_distance_pct = 1.5% (canonical 10-15Δ, was aggressive 0.5%/25Δ)
      - Default credit_pct_per_side = 12% (matches IC conservative_10d preset)
      - Added STOP-on-close (Phase 3b — TradingBlock canonical)
      - Added TIME stop modeling (was missing entirely)
      - Default 12mo data (was 60d benign window)

    Honest simplifications still in play:
      - Option premium decay is approximated via underlying price moves
      - Real wave trading watches the spread's mid-price; we use SPX move as proxy
    """
    data_file = "SPX_5m_1y.json" if use_12mo_data else "SPX_5m_60d.json"
    path = settings.data_dir / "historical" / data_file
    if not path.exists():
        # Fallback to 60d if 12mo missing
        path = settings.data_dir / "historical" / "SPX_5m_60d.json"
        if not path.exists():
            return {"error": f"missing data: {path}"}
    raw = json.loads(path.read_text())
    bars = [
        Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        )
        for r in raw
    ]

    atr_map = _wilder_atr(bars, 14)
    sessions = predictor_run(bars, lambda d: atr_map.get(d))

    multiplier = 100
    if instrument == "XSP":
        scale = 0.1
        wing = 5.0
    elif instrument == "SPY":
        scale = 0.1
        wing = 1.0
    else:
        scale = 1.0
        wing = wing_width_spx

    # PHASE 3 FIX: wave is a SINGLE credit spread (not both call+put like IC).
    # Old code did `credit_total = credit_per_side * 2` which doubled the credit
    # and effectively modeled IC P&L. That's why the old wave backtest looked
    # unrealistically profitable.
    credit_per_side_dollars = (credit_pct_per_side / 100.0) * wing * multiplier
    credit_total = credit_per_side_dollars   # ← was × 2 (the bug we fixed)
    max_loss = wing * multiplier - credit_total
    profit_target_dollars = credit_total * (profit_target_pct / 100.0)
    time_stop_pnl = credit_total * 0.40        # rough breakeven at TIME stop

    # Index bars by ET date for fast post-signal lookup
    bars_by_date: dict[str, list[Bar]] = {}
    for b in bars:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        d = et.strftime("%Y-%m-%d")
        bars_by_date.setdefault(d, []).append(b)

    trades = []
    cum_pnl = 0.0
    n_signals_total = 0
    n_skipped_volatile_session = 0

    for s in sessions:
        if skip_volatile_days and s.regime != "NON-VOLATILE":
            n_skipped_volatile_session += len(s.signals)
            continue
        if not s.signals:
            continue

        session_bars = bars_by_date.get(s.session_date, [])
        if not session_bars:
            continue

        for sig in s.signals:
            n_signals_total += 1
            entry_time = sig.time
            entry_price = sig.entry_price
            side = sig.side  # "sell_call_cs" or "sell_put_cs"

            # Strike placement at signal time (no lookahead)
            if side == "sell_call_cs":
                short_strike = entry_price * (1 + strike_distance_pct / 100.0)
            else:
                short_strike = entry_price * (1 - strike_distance_pct / 100.0)

            # Walk forward through bars in same session, until 16:00 ET
            outcome = "expire_max_profit"
            pnl = credit_total
            exit_time = None
            exit_price = None
            bars_held = 0
            breach_high = None
            breach_low = None

            for b in session_bars:
                if b.time <= entry_time:
                    continue
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bar_min = et.hour * 60 + et.minute
                if bar_min >= 16 * 60:
                    # EOD — let expire OTM if not already exited
                    outcome = "expire_max_profit"
                    pnl = credit_total
                    exit_time = b.time
                    exit_price = float(b.close)
                    break
                bars_held += 1

                # PHASE 3: STOP on close-through-strike (canonical) vs intra-bar wick (legacy)
                if stop_on_close:
                    call_breach = side == "sell_call_cs" and b.close >= short_strike
                    put_breach  = side == "sell_put_cs"  and b.close <= short_strike
                else:
                    call_breach = side == "sell_call_cs" and b.high >= short_strike
                    put_breach  = side == "sell_put_cs"  and b.low  <= short_strike

                if call_breach:
                    outcome = "breach_max_loss"
                    pnl = -max_loss
                    exit_time = b.time
                    exit_price = float(b.close) if stop_on_close else short_strike
                    breach_high = float(b.high)
                    break
                if put_breach:
                    outcome = "breach_max_loss"
                    pnl = -max_loss
                    exit_time = b.time
                    exit_price = float(b.close) if stop_on_close else short_strike
                    breach_low = float(b.low)
                    break

                # Check managed close (favorable price move)
                if side == "sell_call_cs":
                    if b.low <= entry_price * (1 - favorable_move_pct / 100.0):
                        outcome = "managed_profit"
                        pnl = profit_target_dollars
                        exit_time = b.time
                        exit_price = float(b.low)
                        break
                else:
                    if b.high >= entry_price * (1 + favorable_move_pct / 100.0):
                        outcome = "managed_profit"
                        pnl = profit_target_dollars
                        exit_time = b.time
                        exit_price = float(b.high)
                        break

                # PHASE 3: TIME stop (was completely missing!)
                # Forces close `time_stop_min` minutes before 16:00 to avoid pin/gamma risk.
                if bar_min >= (16 * 60 - time_stop_min) and bar_min < 16 * 60:
                    outcome = "time_close"
                    pnl = time_stop_pnl
                    exit_time = b.time
                    exit_price = float(b.close)
                    break

            # If loop didn't exit → assume EOD-equivalent
            if exit_time is None and session_bars:
                outcome = "expire_max_profit"
                pnl = credit_total
                last_bar = session_bars[-1]
                exit_time = last_bar.time
                exit_price = float(last_bar.close)

            cum_pnl += pnl
            trades.append({
                "date": s.session_date,
                "regime": s.regime,
                "side": side,
                "entry_time": entry_time.isoformat(),
                "entry_price": float(entry_price),
                "short_strike": float(short_strike),
                "exit_time": exit_time.isoformat() if exit_time else None,
                "exit_price": float(exit_price) if exit_price else None,
                "bars_held": bars_held,
                "outcome": outcome,
                "pnl_dollars": float(pnl),
                "cum_pnl_dollars": float(cum_pnl),
            })

    n_trades = len(trades)
    n_wins = sum(1 for t in trades if t["pnl_dollars"] > 0)
    n_breaches = sum(1 for t in trades if t["outcome"] == "breach_max_loss")
    n_managed = sum(1 for t in trades if t["outcome"] == "managed_profit")
    n_expire = sum(1 for t in trades if t["outcome"] == "expire_max_profit")
    n_time = sum(1 for t in trades if t["outcome"] == "time_close")
    win_rate = (n_wins / n_trades * 100) if n_trades else 0
    avg_pnl = (cum_pnl / n_trades) if n_trades else 0

    # Group by date for daily P&L
    daily_pnl: dict[str, float] = {}
    for t in trades:
        daily_pnl[t["date"]] = daily_pnl.get(t["date"], 0.0) + t["pnl_dollars"]
    daily_summary = [
        {"date": d, "pnl": round(v, 2), "n_trades": sum(1 for t in trades if t["date"] == d)}
        for d, v in sorted(daily_pnl.items())
    ]

    return {
        "params": {
            "instrument": instrument,
            "wing_width_used": wing,
            "credit_per_side_dollars": round(credit_per_side_dollars, 2),
            "credit_total_per_trade": round(credit_total, 2),
            "max_loss_per_trade": round(max_loss, 2),
            "profit_target_dollars": round(profit_target_dollars, 2),
            "strike_distance_pct": strike_distance_pct,
            "favorable_move_pct": favorable_move_pct,
            "profit_target_pct": profit_target_pct,
            "credit_pct_per_side": credit_pct_per_side,
            "skip_volatile_days": skip_volatile_days,
            "explainer": "Wave backtest: each indicator signal = one credit spread. "
                         "Entry at signal bar; exit on first of breach (max-loss), "
                         "favorable price move (managed close at profit_target_pct), "
                         "or EOD (max profit). NO lookahead at entry. "
                         "NOTE: option premium decay approximated via underlying price moves "
                         "since historical 0DTE chain data unavailable.",
        },
        "summary": {
            "n_signals_total": n_signals_total,
            "n_signals_skipped_volatile": n_skipped_volatile_session,
            "n_trades": n_trades,
            "n_wins": n_wins,
            "n_breaches": n_breaches,
            "n_managed": n_managed,
            "n_expire_otm": n_expire,
            "n_time_close": n_time,
            "win_rate_pct": round(win_rate, 1),
            "avg_pnl_per_trade": round(avg_pnl, 2),
            "total_pnl": round(cum_pnl, 2),
        },
        "trades": trades,
        "daily_pnl": daily_summary,
    }
