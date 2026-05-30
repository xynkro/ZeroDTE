"""Stateful streaming wrapper around the offline predictor.

Maintains a rolling buffer of last N bars and re-runs the signal logic on
each new bar. Returns the latest session result + any signals fired.

Reuses constants from `predictor` so live and backtest are identical.
"""
from __future__ import annotations

from collections import deque
from datetime import datetime
from typing import Optional

import numpy as np

from .predictor import (
    Bar, Signal, SessionResult, run_backtest,
    OBS_MINUTES, SESSION_START_HOUR_ET, SESSION_START_MIN_ET,
    VOLATILE_MULT, NV_RANGE_MULT, V_ATR_MULT,
    RSI_LEN, STOCH_K_LEN, STOCH_D_LEN, WVF_PERIOD,
    EMA_FAST_LEN, EMA_SLOW_LEN, TREND_FILTER_ENABLED,
    _wilder_rsi, _stoch, _wilder_atr, _wvf, _ema, _classify_trend,
)


# Buffer enough history for indicators to warm up + at least 1 full session.
# 5m × 78 bars/session × 5 sessions = 390. Round up to 600 for safety.
DEFAULT_BUFFER_BARS = 600


class LivePredictor:
    """Online wrapper. Feed bars in via on_bar(); query state via current_state()."""

    def __init__(self, atr_d1_lookup, max_bars: int = DEFAULT_BUFFER_BARS):
        self._buffer: deque[Bar] = deque(maxlen=max_bars)
        self._atr_d1_lookup = atr_d1_lookup
        self._latest_session: Optional[SessionResult] = None
        self._known_signal_keys: set[str] = set()
        self._new_signals: list[Signal] = []

    def on_bar(self, bar: Bar) -> list[Signal]:
        """Append a new bar, recompute, return any NEW signals fired."""
        # De-duplicate identical timestamps
        if self._buffer and self._buffer[-1].time == bar.time:
            self._buffer[-1] = bar  # update intra-bar
        else:
            self._buffer.append(bar)

        if len(self._buffer) < 50:  # need warmup
            return []

        sessions = run_backtest(list(self._buffer), self._atr_d1_lookup)
        if not sessions:
            return []
        latest = sessions[-1]
        self._latest_session = latest

        new = []
        for sig in latest.signals:
            key = f"{sig.time.isoformat()}|{sig.side}"
            if key not in self._known_signal_keys:
                self._known_signal_keys.add(key)
                new.append(sig)

        return new

    def current_state(self) -> dict:
        """Return current regime + projection + indicator values."""
        if not self._buffer:
            return {"warming_up": True, "buffer_size": 0}

        s = self._latest_session
        last = self._buffer[-1]

        # Compute live indicator values from current buffer
        bars = list(self._buffer)
        closes = np.array([b.close for b in bars])
        highs  = np.array([b.high for b in bars])
        lows   = np.array([b.low for b in bars])

        rsi_v: float | None = None
        stoch_k_v: float | None = None
        stoch_d_v: float | None = None
        atr5_v: float | None = None
        wvf_v: float | None = None
        wvf_spike_v: bool = False
        trend_v: str = "flat"
        ema_fast_v: float | None = None
        ema_slow_v: float | None = None

        try:
            rsi_arr = _wilder_rsi(closes, RSI_LEN)
            if not np.isnan(rsi_arr[-1]):
                rsi_v = float(rsi_arr[-1])
            k_arr, d_arr = _stoch(highs, lows, closes, STOCH_K_LEN, STOCH_D_LEN)
            if not np.isnan(k_arr[-1]):
                stoch_k_v = float(k_arr[-1])
            if not np.isnan(d_arr[-1]):
                stoch_d_v = float(d_arr[-1])
            atr_arr = _wilder_atr(highs, lows, closes, RSI_LEN)
            if not np.isnan(atr_arr[-1]):
                atr5_v = float(atr_arr[-1])
            wvf_arr, wvf_spike_arr = _wvf(closes, lows, WVF_PERIOD, 2.0)
            if not np.isnan(wvf_arr[-1]):
                wvf_v = float(wvf_arr[-1])
            wvf_spike_v = bool(wvf_spike_arr[-1])
            # Pullback trend (the new filter that's gating live signals)
            ef = _ema(closes, EMA_FAST_LEN)
            es = _ema(closes, EMA_SLOW_LEN)
            if not np.isnan(ef[-1]):
                ema_fast_v = float(ef[-1])
            if not np.isnan(es[-1]):
                ema_slow_v = float(es[-1])
            trend_v = _classify_trend(closes, ef, es, len(closes) - 1)
        except Exception:
            pass

        return {
            "warming_up": s is None,
            "buffer_size": len(self._buffer),
            "last_bar_time": last.time.isoformat(),
            "last_close": last.close,
            "session_date": s.session_date if s else None,
            "regime": s.regime if s else "pre_obs",
            "proj_high": s.proj_high if s else None,
            "proj_low": s.proj_low if s else None,
            "obs_high": s.obs_high if s else None,
            "obs_low": s.obs_low if s else None,
            "obs_open": s.obs_open if s else None,
            "obs_close": s.obs_close if s else None,
            "session_high": s.session_high if s else None,
            "session_low": s.session_low if s else None,
            "n_signals_today": len(s.signals) if s else 0,
            # Live indicator values
            "rsi": rsi_v,
            "stoch_k": stoch_k_v,
            "stoch_d": stoch_d_v,
            "atr_5m": atr5_v,
            "wvf": wvf_v,
            "wvf_spike": wvf_spike_v,
            # Pullback trend filter — live signal gate
            "trend": trend_v,
            "ema_fast": ema_fast_v,
            "ema_slow": ema_slow_v,
            "trend_filter_enabled": TREND_FILTER_ENABLED,
        }
