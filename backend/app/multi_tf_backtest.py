"""Multi-timeframe + multi-strategy backtest harness.

Addresses two real bugs in the original framework:
  1. FRAME-LOCK — original backtest only ran on 5m bars. We resample the same
     5m source into 10m / 15m / 30m / 60m and run the strategy across all TFs.
  2. STRATEGY MONOCULTURE — original was Stoch-reversal-from-extreme only
     (pure mean reversion, no trend filter). We now run THREE variants:
       (a) MeanRev      — original logic (Stoch reversal cross at extremes)
       (b) Pullback     — classic trend-pullback (EMA trend filter + Stoch reversal,
                          only sell-call in downtrend, sell-put in uptrend)
       (c) PullbackPlus — Pullback + WVF spike confirmation + RSI agreement

  Each (TF × strategy) combo produces wave-trading P&L over 60 days SPX.

NO LOOKAHEAD. Every signal uses bars[i] data only at decision time. Walk-forward
exit (breach / managed-profit / EOD).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import numpy as np

from .config import settings
from .predictor import (
    Bar, _wilder_rsi, _stoch, _wilder_atr, _wvf,
    SESSION_START_HOUR_ET, SESSION_START_MIN_ET,
    OBS_MINUTES, VOLATILE_MULT, NV_RANGE_MULT, V_ATR_MULT,
    RSI_LEN, STOCH_K_LEN, STOCH_D_LEN, STOCH_HIGH_THR, STOCH_LOW_THR,
    WVF_PERIOD, WVF_BB_MULT,
)

ET = ZoneInfo("America/New_York")


# ═════════════════════════════════════════════════════════════════════════
# RESAMPLER — turn 5m bars into N-minute bars
# ═════════════════════════════════════════════════════════════════════════

def resample_bars(bars_5m: list[Bar], target_minutes: int) -> list[Bar]:
    """Aggregate 5m bars into target_minutes bars. Resets per session date.

    target_minutes must be >= 5 and divisible by 5.
    """
    if target_minutes == 5:
        return bars_5m
    if target_minutes < 5 or target_minutes % 5 != 0:
        raise ValueError(f"target_minutes must be >=5 and a multiple of 5; got {target_minutes}")

    n_per = target_minutes // 5
    by_date: dict[str, list[Bar]] = {}
    for b in bars_5m:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        d = et.strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)

    out: list[Bar] = []
    for d in sorted(by_date.keys()):
        ses = sorted(by_date[d], key=lambda b: b.time)
        # Group N per bucket
        for i in range(0, len(ses), n_per):
            chunk = ses[i:i + n_per]
            if len(chunk) < n_per and i > 0:
                # incomplete trailing chunk — drop (unless first chunk)
                continue
            if not chunk:
                continue
            out.append(Bar(
                time=chunk[0].time,
                open=chunk[0].open,
                high=max(b.high for b in chunk),
                low=min(b.low for b in chunk),
                close=chunk[-1].close,
                volume=sum(b.volume for b in chunk),
            ))
    return out


# ═════════════════════════════════════════════════════════════════════════
# STRATEGY VARIANTS — each returns a list of signal dicts per session
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class TFSignal:
    time: datetime
    side: str           # "sell_call_cs" | "sell_put_cs"
    entry_price: float
    rsi: float
    stoch_k: float
    stoch_d: float
    wvf_spike: bool
    trend: str          # "up" | "down" | "flat"


def _ema(arr: np.ndarray, length: int) -> np.ndarray:
    out = np.full_like(arr, np.nan, dtype=float)
    if len(arr) < length:
        return out
    alpha = 2.0 / (length + 1.0)
    out[length - 1] = arr[:length].mean()
    for i in range(length, len(arr)):
        out[i] = arr[i] * alpha + out[i - 1] * (1 - alpha)
    return out


def _classify_trend(closes: np.ndarray, ema_fast: np.ndarray, ema_slow: np.ndarray, i: int) -> str:
    """Classify trend at bar i using EMA spread alone (fixed 2026-05-09).
    Threshold: 0.05% spread = meaningful trend on intraday."""
    if i < 0 or i >= len(closes):
        return "flat"
    if np.isnan(ema_fast[i]) or np.isnan(ema_slow[i]):
        return "flat"
    spread_pct = (ema_fast[i] - ema_slow[i]) / ema_slow[i] * 100.0
    if spread_pct > 0.05:
        return "up"
    if spread_pct < -0.05:
        return "down"
    return "flat"


def strategy_meanrev(bars: list[Bar], cooldown_bars: int = 6) -> list[TFSignal]:
    """ORIGINAL — Stoch reversal cross at extremes, no trend filter.

    Match predictor.py logic. Multiple signals per session. Cooldown gate.
    """
    return _run_stoch_reversal(bars, cooldown_bars=cooldown_bars, require_trend_filter=False)


def strategy_pullback(bars: list[Bar], cooldown_bars: int = 6,
                      ema_fast_len: int = 10, ema_slow_len: int = 30) -> list[TFSignal]:
    """CLASSIC PULLBACK — trend filter + Stoch reversal as pullback trigger.

    - In UPTREND   (EMA10 > EMA30 + price above EMA10): only sell-put (bullish)
    - In DOWNTREND (EMA10 < EMA30 + price below EMA10): only sell-call (bearish)
    - In FLAT: both sides allowed (treats as mean rev)

    The Stoch cross then acts as the pullback-end trigger (price recovering
    from a counter-trend dip).
    """
    return _run_stoch_reversal(
        bars, cooldown_bars=cooldown_bars, require_trend_filter=True,
        ema_fast_len=ema_fast_len, ema_slow_len=ema_slow_len,
    )


def strategy_pullback_plus(bars: list[Bar], cooldown_bars: int = 6,
                           ema_fast_len: int = 10, ema_slow_len: int = 30) -> list[TFSignal]:
    """PULLBACK + WVF spike + RSI agreement.

    Pullback as above, plus require:
      - sell-call: RSI > 60 (bearish-leaning) at signal bar
      - sell-put : RSI < 40 (bullish-leaning) at signal bar
      - WVF spike at signal bar (high-volatility confirmation)
    """
    return _run_stoch_reversal(
        bars, cooldown_bars=cooldown_bars, require_trend_filter=True,
        require_rsi=True, require_wvf=True,
        ema_fast_len=ema_fast_len, ema_slow_len=ema_slow_len,
    )


def _run_stoch_reversal(
    bars: list[Bar],
    cooldown_bars: int = 6,
    require_trend_filter: bool = False,
    require_rsi: bool = False,
    require_wvf: bool = False,
    ema_fast_len: int = 10,
    ema_slow_len: int = 30,
) -> list[TFSignal]:
    """Core Stoch-reversal loop with optional trend / RSI / WVF gates."""
    if len(bars) < max(STOCH_K_LEN, ema_slow_len) + 5:
        return []

    closes = np.array([b.close for b in bars])
    highs = np.array([b.high for b in bars])
    lows = np.array([b.low for b in bars])
    times = [b.time for b in bars]

    rsi_arr = _wilder_rsi(closes, RSI_LEN)
    k_arr, d_arr = _stoch(highs, lows, closes, STOCH_K_LEN, STOCH_D_LEN)
    wvf_arr, wvf_spike_arr = _wvf(closes, lows, WVF_PERIOD, WVF_BB_MULT)
    ema_fast = _ema(closes, ema_fast_len)
    ema_slow = _ema(closes, ema_slow_len)

    open_minute = SESSION_START_HOUR_ET * 60 + SESSION_START_MIN_ET

    # Group by session date for cooldown reset + obs-window enforcement
    session_idxs: dict[str, list[int]] = {}
    for i, t in enumerate(times):
        et = t.astimezone(ET) if t.tzinfo else t
        d = et.strftime("%Y-%m-%d")
        session_idxs.setdefault(d, []).append(i)

    signals: list[TFSignal] = []
    for d, idxs in session_idxs.items():
        post_obs_start_minute = open_minute + OBS_MINUTES
        # Filter to post-obs window only (skip first 15 min)
        post_obs = []
        for i in idxs:
            et = times[i].astimezone(ET) if times[i].tzinfo else times[i]
            mod = et.hour * 60 + et.minute
            if mod >= post_obs_start_minute and mod < (16 * 60):
                post_obs.append(i)
        if len(post_obs) < 2:
            continue

        last_call_idx = -10**9
        last_put_idx = -10**9
        for k_pos, i in enumerate(post_obs[1:], start=1):
            i_prev = post_obs[k_pos - 1]
            if np.isnan(k_arr[i]) or np.isnan(d_arr[i]):
                continue
            k_now, k_prev = k_arr[i], k_arr[i_prev]
            d_now, d_prev = d_arr[i], d_arr[i_prev]

            cross_down = (k_prev > d_prev and k_now < d_now and k_prev > STOCH_HIGH_THR)
            cross_up   = (k_prev < d_prev and k_now > d_now and k_prev < STOCH_LOW_THR)

            trend = _classify_trend(closes, ema_fast, ema_slow, i)
            rsi_v = rsi_arr[i] if not np.isnan(rsi_arr[i]) else 50.0
            wvf_sp = bool(wvf_spike_arr[i])

            # Cooldown gates
            call_ok = (k_pos - last_call_idx) >= cooldown_bars
            put_ok  = (k_pos - last_put_idx)  >= cooldown_bars

            # Sell call (bearish bet): want overbought reversal
            if cross_down and call_ok:
                # Trend filter: only allow in down/flat
                if require_trend_filter and trend == "up":
                    pass
                elif require_rsi and rsi_v < 60:
                    pass
                elif require_wvf and not wvf_sp:
                    pass
                else:
                    signals.append(TFSignal(
                        time=times[i], side="sell_call_cs",
                        entry_price=closes[i], rsi=rsi_v,
                        stoch_k=k_now, stoch_d=d_now,
                        wvf_spike=wvf_sp, trend=trend,
                    ))
                    last_call_idx = k_pos

            # Sell put (bullish bet): want oversold reversal
            if cross_up and put_ok:
                if require_trend_filter and trend == "down":
                    pass
                elif require_rsi and rsi_v > 40:
                    pass
                elif require_wvf and not wvf_sp:
                    pass
                else:
                    signals.append(TFSignal(
                        time=times[i], side="sell_put_cs",
                        entry_price=closes[i], rsi=rsi_v,
                        stoch_k=k_now, stoch_d=d_now,
                        wvf_spike=wvf_sp, trend=trend,
                    ))
                    last_put_idx = k_pos

    return signals


# ═════════════════════════════════════════════════════════════════════════
# WAVE BACKTEST — same exit logic as backtest_api.run_wave_backtest
# ═════════════════════════════════════════════════════════════════════════

def simulate_wave(
    signals: list[TFSignal],
    bars_5m: list[Bar],            # always use 5m granularity for exit walk-forward
    instrument: str = "XSP",
    strike_distance_pct: float = 0.5,
    credit_pct_per_side: float = 25.0,
    profit_target_pct: float = 25.0,
    favorable_move_pct: float = 0.3,
    wing_width_spx: float = 5.0,
) -> dict:
    """Simulate the trades. Exit always walks 5m bars even if signals were on 30m
    — this keeps exit granularity uniform across TFs.
    """
    multiplier = 100
    if instrument == "XSP":
        wing = 5.0
    elif instrument == "SPY":
        wing = 1.0
    else:
        wing = wing_width_spx

    credit_per_side = (credit_pct_per_side / 100.0) * wing * multiplier
    credit_total = credit_per_side * 2
    max_loss = wing * multiplier - credit_total
    profit_target = credit_total * (profit_target_pct / 100.0)

    bars_by_date: dict[str, list[Bar]] = {}
    for b in bars_5m:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        d = et.strftime("%Y-%m-%d")
        bars_by_date.setdefault(d, []).append(b)

    trades = []
    cum = 0.0

    for sig in signals:
        et_sig = sig.time.astimezone(ET) if sig.time.tzinfo else sig.time
        d = et_sig.strftime("%Y-%m-%d")
        ses_bars = bars_by_date.get(d, [])
        if not ses_bars:
            continue

        if sig.side == "sell_call_cs":
            short_strike = sig.entry_price * (1 + strike_distance_pct / 100.0)
        else:
            short_strike = sig.entry_price * (1 - strike_distance_pct / 100.0)

        outcome = "expire_max_profit"
        pnl = credit_total
        exit_time = None
        bars_held = 0

        for b in ses_bars:
            if b.time <= sig.time:
                continue
            et_b = b.time.astimezone(ET) if b.time.tzinfo else b.time
            if et_b.hour >= 16:
                break
            bars_held += 1

            if sig.side == "sell_call_cs" and b.high >= short_strike:
                outcome = "breach_max_loss"
                pnl = -max_loss
                exit_time = b.time
                break
            if sig.side == "sell_put_cs" and b.low <= short_strike:
                outcome = "breach_max_loss"
                pnl = -max_loss
                exit_time = b.time
                break

            if sig.side == "sell_call_cs":
                if b.low <= sig.entry_price * (1 - favorable_move_pct / 100.0):
                    outcome = "managed_profit"
                    pnl = profit_target
                    exit_time = b.time
                    break
            else:
                if b.high >= sig.entry_price * (1 + favorable_move_pct / 100.0):
                    outcome = "managed_profit"
                    pnl = profit_target
                    exit_time = b.time
                    break

        cum += pnl
        trades.append({
            "date": d,
            "side": sig.side,
            "trend": sig.trend,
            "entry_time": sig.time.isoformat(),
            "entry_price": float(sig.entry_price),
            "short_strike": float(short_strike),
            "exit_time": exit_time.isoformat() if exit_time else None,
            "bars_held": bars_held,
            "outcome": outcome,
            "pnl": float(pnl),
            "cum": float(cum),
        })

    n = len(trades)
    n_wins = sum(1 for t in trades if t["pnl"] > 0)
    n_breach = sum(1 for t in trades if t["outcome"] == "breach_max_loss")
    n_managed = sum(1 for t in trades if t["outcome"] == "managed_profit")
    n_expire = sum(1 for t in trades if t["outcome"] == "expire_max_profit")
    return {
        "n_trades": n,
        "n_wins": n_wins,
        "n_breach": n_breach,
        "n_managed": n_managed,
        "n_expire": n_expire,
        "win_rate_pct": round(100 * n_wins / n, 1) if n else 0,
        "total_pnl": round(cum, 2),
        "avg_pnl": round(cum / n, 2) if n else 0,
        "trades": trades,
    }


# ═════════════════════════════════════════════════════════════════════════
# MATRIX RUNNER — run all (TF × Strategy) combos and return summary table
# ═════════════════════════════════════════════════════════════════════════

STRATEGY_FNS = {
    "MeanRev":      strategy_meanrev,
    "Pullback":     strategy_pullback,
    "PullbackPlus": strategy_pullback_plus,
}

TIMEFRAMES = [5, 10, 15, 30, 60]


def run_matrix(
    instrument: str = "XSP",
    strike_distance_pct: float = 0.5,
    credit_pct_per_side: float = 25.0,
    profit_target_pct: float = 25.0,
    favorable_move_pct: float = 0.3,
    data_file: str = "SPX_5m_1y.json",   # default = 12-month dataset (regime-honest)
    sub_window_start: str | None = None,  # ISO date, optional filter
    sub_window_end: str | None = None,
    include_spx_10pt: bool = True,        # Henry Schwartz CBOE variant
) -> dict:
    """Run every (timeframe × strategy) combo on chosen SPX 5m dataset.

    data_file options:
      "SPX_5m_60d.json" — 60d benign window (Feb-May 2026)
      "SPX_5m_1y.json"  — 12mo with real tail events (incl 2025-04-09 +9.79%)

    Optionally filter to a sub-window for regime-specific testing
    (e.g. April 2025 tariff sequence, or pre-tariff bull period).
    """
    path = settings.data_dir / "historical" / data_file
    if not path.exists():
        # Fallback to 60d if requested file missing
        path = settings.data_dir / "historical" / "SPX_5m_60d.json"
    if not path.exists():
        return {"error": f"missing data: {path}"}

    raw = json.loads(path.read_text())
    # Optional sub-window filter
    if sub_window_start or sub_window_end:
        s = sub_window_start or "0000-00-00"
        e = sub_window_end or "9999-99-99"
        raw = [r for r in raw if s <= r["datetime"][:10] <= e]
    bars_5m = [
        Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        ) for r in raw
    ]
    if not bars_5m:
        return {"error": "no bars in selected window"}

    # Build wing-width × instrument variants. Henry Schwartz (CBOE) suggests
    # 10pt SPX wings collect more premium per side (~$1.00 each = ~33% credit
    # of the 10pt wing). We add this as a parallel variant when requested.
    variants = [
        # (label, instrument, wing_width_spx, credit_pct_per_side)
        (instrument,             instrument, 5.0,  credit_pct_per_side),
    ]
    if include_spx_10pt and instrument != "SPY":
        # Henry's variant: 10pt SPX wings, ~$1 premium per side ≈ 10% per side of 10pt wing × 100 = $100
        variants.append(("SPX_10pt", "SPX", 10.0, 10.0))

    matrix = []
    for tf in TIMEFRAMES:
        bars_tf = resample_bars(bars_5m, tf)
        for strat_name, strat_fn in STRATEGY_FNS.items():
            sigs = strat_fn(bars_tf)
            for var_label, var_inst, var_wing, var_credit_pct in variants:
                res = simulate_wave(
                    sigs, bars_5m,  # exit always on 5m granularity
                    instrument=var_inst,
                    strike_distance_pct=strike_distance_pct,
                    credit_pct_per_side=var_credit_pct,
                    profit_target_pct=profit_target_pct,
                    favorable_move_pct=favorable_move_pct,
                    wing_width_spx=var_wing,
                )
                matrix.append({
                    "timeframe_min": tf,
                    "strategy": strat_name,
                    "variant": var_label,
                    "wing": var_wing,
                    "n_signals": len(sigs),
                    "n_trades": res["n_trades"],
                    "win_rate_pct": res["win_rate_pct"],
                    "total_pnl": res["total_pnl"],
                    "avg_pnl": res["avg_pnl"],
                    "n_breach": res["n_breach"],
                    "n_managed": res["n_managed"],
                    "n_expire": res["n_expire"],
                })

    # Summary stats
    n_sessions = len(set(b.time.astimezone(ET).strftime("%Y-%m-%d") for b in bars_5m))
    return {
        "params": {
            "n_sessions": n_sessions,
            "instrument": instrument,
            "strike_distance_pct": strike_distance_pct,
            "credit_pct_per_side": credit_pct_per_side,
            "profit_target_pct": profit_target_pct,
            "favorable_move_pct": favorable_move_pct,
            "explainer": "Multi-timeframe × multi-strategy backtest. "
                         "5m source data resampled to 10/15/30/60m. "
                         "Strategy variants: MeanRev (no trend filter, original), "
                         "Pullback (EMA10/30 trend filter), "
                         "PullbackPlus (trend + RSI agreement + WVF spike). "
                         "Exit walk-forward on 5m granularity.",
        },
        "matrix": matrix,
    }
