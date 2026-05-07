"""Python port of the ZeroDTE Range Predictor Pine indicator.

Reproduces the SAME logic so the backtest is faithful to what TV would show.
Critical: order of operations on each 5-min bar must match the Pine script.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time
from typing import Optional

import numpy as np


# ===== LOCKED PARAMETERS (mirror Pine inputs; no per-day tuning) =====
OBS_MINUTES = 15
SESSION_START_HOUR_ET = 9
SESSION_START_MIN_ET = 30
SESSION_END_MIN_AFTER_OPEN = 390  # 6.5h * 60

VOLATILE_MULT = 1.5         # obs_range / ATR(14) D1
NV_RANGE_MULT = 1.5         # non-volatile: project = obs_range × this each side
V_ATR_MULT = 2.0            # volatile: project = ATR D1 × this each side from open
RSI_LEN = 14
RSI_LONG_THR = 70
RSI_SHORT_THR = 30
STOCH_K_LEN = 14
STOCH_D_LEN = 3
STOCH_HIGH_THR = 80
STOCH_LOW_THR = 20
WVF_PERIOD = 22
WVF_BB_MULT = 2.0
NEAR_PROJ_ATR = 0.5         # signal must be within 0.5×ATR(5m) of projected boundary


@dataclass
class Bar:
    time: datetime  # tz-aware (typically -04:00 / -05:00 ET)
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class Signal:
    time: datetime
    side: str          # "sell_call_cs" or "sell_put_cs"
    entry_price: float # underlying price at signal
    proj_high: float
    proj_low: float
    regime: str
    rsi: float
    stoch_k: float
    stoch_d: float
    wvf: float
    wvf_spike: bool
    strong: bool       # WVF spike confirmation


@dataclass
class SessionResult:
    session_date: str
    regime: str
    proj_high: Optional[float]
    proj_low: Optional[float]
    obs_high: Optional[float]
    obs_low: Optional[float]
    session_high: float
    session_low: float
    session_close: float
    proj_high_held: bool   # True if session high < proj_high (predicted upper held)
    proj_low_held: bool    # True if session low > proj_low (predicted lower held)
    both_held: bool        # iron condor would have profited
    signals: list[Signal] = field(default_factory=list)


def _wilder_rsi(closes: np.ndarray, length: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < length + 1:
        return out
    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_g = gains[:length].mean()
    avg_l = losses[:length].mean()
    out[length] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    for i in range(length, len(deltas)):
        avg_g = (avg_g * (length - 1) + gains[i]) / length
        avg_l = (avg_l * (length - 1) + losses[i]) / length
        out[i + 1] = 100.0 if avg_l == 0 else 100.0 - 100.0 / (1.0 + avg_g / avg_l)
    return out


def _stoch(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
           k_len: int, d_len: int) -> tuple[np.ndarray, np.ndarray]:
    n = len(closes)
    k = np.full(n, np.nan)
    for i in range(k_len - 1, n):
        hh = highs[i - k_len + 1: i + 1].max()
        ll = lows[i - k_len + 1: i + 1].min()
        rng = hh - ll
        k[i] = 50.0 if rng == 0 else 100.0 * (closes[i] - ll) / rng
    d = np.full(n, np.nan)
    for i in range(d_len - 1, n):
        if not np.isnan(k[i - d_len + 1: i + 1]).any():
            d[i] = k[i - d_len + 1: i + 1].mean()
    return k, d


def _wilder_atr(highs: np.ndarray, lows: np.ndarray, closes: np.ndarray,
                length: int) -> np.ndarray:
    n = len(closes)
    out = np.full(n, np.nan)
    if n < length + 1:
        return out
    tr = np.zeros(n)
    tr[0] = highs[0] - lows[0]
    for i in range(1, n):
        tr[i] = max(highs[i] - lows[i],
                    abs(highs[i] - closes[i - 1]),
                    abs(lows[i] - closes[i - 1]))
    out[length] = tr[1:length + 1].mean()
    for i in range(length + 1, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def _wvf(closes: np.ndarray, lows: np.ndarray, period: int,
         bb_mult: float) -> tuple[np.ndarray, np.ndarray]:
    n = len(closes)
    wvf = np.full(n, np.nan)
    spike = np.zeros(n, dtype=bool)
    for i in range(period - 1, n):
        hclose = closes[i - period + 1: i + 1].max()
        if hclose > 0:
            wvf[i] = (hclose - lows[i]) / hclose * 100.0
    # BB on WVF
    for i in range(period * 2 - 2, n):
        window = wvf[i - period + 1: i + 1]
        if not np.isnan(window).any():
            mid = window.mean()
            sd = window.std(ddof=0)
            spike[i] = wvf[i] > (mid + bb_mult * sd)
    return wvf, spike


def _et_minute_of_day(t: datetime) -> int:
    """Minutes since midnight in ET. Bars are tz-aware, typically America/New_York offset."""
    return t.hour * 60 + t.minute


def _et_session_id(t: datetime) -> str:
    return f"{t.year:04d}-{t.month:02d}-{t.day:02d}"


def run_backtest(bars: list[Bar], atr_d1_func) -> list[SessionResult]:
    """Run the indicator's logic over historical bars, grouped by session.
    `atr_d1_func(date_iso) -> float` returns prior D1 ATR(14) for regime classification.
    """
    closes = np.array([b.close for b in bars])
    highs = np.array([b.high for b in bars])
    lows = np.array([b.low for b in bars])
    times = [b.time for b in bars]

    rsi_arr = _wilder_rsi(closes, RSI_LEN)
    stoch_k_arr, stoch_d_arr = _stoch(highs, lows, closes, STOCH_K_LEN, STOCH_D_LEN)
    atr5_arr = _wilder_atr(highs, lows, closes, RSI_LEN)
    wvf_arr, wvf_spike_arr = _wvf(closes, lows, WVF_PERIOD, WVF_BB_MULT)

    # Group bars by session date (ET local date)
    session_bars: dict[str, list[int]] = {}
    for i, t in enumerate(times):
        sid = _et_session_id(t)
        session_bars.setdefault(sid, []).append(i)

    open_minute = SESSION_START_HOUR_ET * 60 + SESSION_START_MIN_ET

    results: list[SessionResult] = []
    for sid, idxs in session_bars.items():
        session_open_idx = None
        for i in idxs:
            mod = _et_minute_of_day(times[i])
            if mod >= open_minute:
                session_open_idx = i
                break
        if session_open_idx is None:
            continue

        # Observation window: first OBS_MINUTES // 5 bars (so 3 bars for 15min)
        n_obs_bars = OBS_MINUTES // 5
        obs_idxs = []
        for i in idxs:
            mod = _et_minute_of_day(times[i])
            mins_since_open = mod - open_minute
            if 0 <= mins_since_open < OBS_MINUTES:
                obs_idxs.append(i)
            if len(obs_idxs) >= n_obs_bars:
                break

        if len(obs_idxs) < n_obs_bars:
            # Not enough data for this session
            continue

        obs_high = max(highs[i] for i in obs_idxs)
        obs_low = min(lows[i] for i in obs_idxs)
        obs_close = closes[obs_idxs[-1]]
        obs_range = obs_high - obs_low

        atr_d1 = atr_d1_func(sid)
        if atr_d1 is None or atr_d1 <= 0 or obs_range <= 0:
            continue

        regime_volatile = (obs_range / atr_d1) > VOLATILE_MULT
        if regime_volatile:
            proj_high = obs_close + V_ATR_MULT * atr_d1
            proj_low = obs_close - V_ATR_MULT * atr_d1
            regime_str = "VOLATILE"
        else:
            proj_high = obs_high + NV_RANGE_MULT * obs_range
            proj_low = obs_low - NV_RANGE_MULT * obs_range
            regime_str = "NON-VOLATILE"

        # Session-wide stats (after observation window)
        post_obs_idxs = [i for i in idxs if _et_minute_of_day(times[i]) - open_minute >= OBS_MINUTES]
        if not post_obs_idxs:
            continue
        sess_high = max(highs[i] for i in post_obs_idxs)
        sess_low = min(lows[i] for i in post_obs_idxs)
        sess_close = closes[post_obs_idxs[-1]]

        proj_high_held = sess_high < proj_high
        proj_low_held = sess_low > proj_low
        both_held = proj_high_held and proj_low_held

        # Generate intraday signals (NON-VOLATILE only; volatile = stand aside)
        signals: list[Signal] = []
        fired_call = False
        fired_put = False
        if not regime_volatile:
            for k in range(1, len(post_obs_idxs)):
                i = post_obs_idxs[k]
                i_prev = post_obs_idxs[k - 1]
                if np.isnan(rsi_arr[i]) or np.isnan(stoch_k_arr[i]) or np.isnan(stoch_d_arr[i]):
                    continue
                if np.isnan(atr5_arr[i]) or atr5_arr[i] <= 0:
                    continue

                # Stoch crossovers
                k_now = stoch_k_arr[i]
                k_prev = stoch_k_arr[i_prev]
                d_now = stoch_d_arr[i]
                d_prev = stoch_d_arr[i_prev]
                cross_down_from_high = (
                    k_prev > d_prev and k_now < d_now and k_prev > STOCH_HIGH_THR
                )
                cross_up_from_low = (
                    k_prev < d_prev and k_now > d_now and k_prev < STOCH_LOW_THR
                )

                near_upper = 0 <= (proj_high - closes[i]) <= NEAR_PROJ_ATR * atr5_arr[i]
                near_lower = 0 <= (closes[i] - proj_low) <= NEAR_PROJ_ATR * atr5_arr[i]

                sell_call = (rsi_arr[i] > RSI_LONG_THR
                             and cross_down_from_high
                             and near_upper)
                sell_put = (rsi_arr[i] < RSI_SHORT_THR
                            and cross_up_from_low
                            and near_lower)

                if sell_call and not fired_call:
                    sig = Signal(
                        time=times[i], side="sell_call_cs",
                        entry_price=closes[i],
                        proj_high=proj_high, proj_low=proj_low, regime=regime_str,
                        rsi=rsi_arr[i], stoch_k=k_now, stoch_d=d_now,
                        wvf=wvf_arr[i] if not np.isnan(wvf_arr[i]) else 0.0,
                        wvf_spike=bool(wvf_spike_arr[i]),
                        strong=bool(wvf_spike_arr[i]),
                    )
                    signals.append(sig)
                    fired_call = True
                if sell_put and not fired_put:
                    sig = Signal(
                        time=times[i], side="sell_put_cs",
                        entry_price=closes[i],
                        proj_high=proj_high, proj_low=proj_low, regime=regime_str,
                        rsi=rsi_arr[i], stoch_k=k_now, stoch_d=d_now,
                        wvf=wvf_arr[i] if not np.isnan(wvf_arr[i]) else 0.0,
                        wvf_spike=bool(wvf_spike_arr[i]),
                        strong=bool(wvf_spike_arr[i]),
                    )
                    signals.append(sig)
                    fired_put = True

        results.append(SessionResult(
            session_date=sid,
            regime=regime_str,
            proj_high=proj_high, proj_low=proj_low,
            obs_high=obs_high, obs_low=obs_low,
            session_high=sess_high, session_low=sess_low,
            session_close=sess_close,
            proj_high_held=proj_high_held,
            proj_low_held=proj_low_held,
            both_held=both_held,
            signals=signals,
        ))

    return results
