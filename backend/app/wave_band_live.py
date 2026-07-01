"""WaveZero — the VALIDATED band-anchored config, as ONE shared decision function.

This is a faithful port of `scripts/wave_band_backtest.py::main()` (the validated spec:
band-anchored strikes + Schwartz vol-released gate + Bollinger 14/2.5 + cushion filter,
OOS +$42/d t=5.85, both regimes, Bonferroni + jackknife survived). The SAME function is
used by the live engine AND a parity test against the backtest — so live == validated
spec by construction, not by a re-implementation that can drift.

Pure + deterministic. Given the live 5m close buffer + today's realized 5m vol + the
running medians, `decide_band_trade()` returns the trade (side, SPX strikes, est. credit)
or None when the day is gated out (vol not released / can't clear the credit floor with
enough cushion). The live engine maps SPX→SPY, applies its own sizing, and submits.
"""
from __future__ import annotations

import math
import statistics as st
from dataclasses import dataclass

from . import bs_pricing as bs

MULT = 100
STEP = 5.0   # SPX strike walk step (matches the backtest)


@dataclass(frozen=True)
class BandParams:
    bb_len: int = 14
    bb_mult: float = 2.5
    wing: float = 10.0            # SPX points (~$1,000 defined risk at 1ct)
    credit_floor: float = 30.0    # $ per spread; walk toward spot until met
    choppy_push_atr: float = 1.5  # push strike further OTM on wide-band days
    min_otm_pct: float = 0.20     # walk CAP: never closer than this % to spot
    cushion_pct: float = 0.50     # SKIP if the final strike is < this % OTM (the cushion filter)
    premium_mult: float = 1.20    # IV/RV (DIRECTIONAL_PREMIUM_MULT)
    put_skew: float = 1.0
    call_skew: float = 1.0


@dataclass(frozen=True)
class BandDecision:
    side: str            # "sell_put_cs" | "sell_call_cs"
    short_strike: float  # SPX
    long_strike: float   # SPX
    est_credit: float    # $ per spread (BS estimate at entry)
    vol_released: bool
    choppy: bool
    pct_otm: float       # cushion actually achieved
    r5: float


def bollinger(closes: list[float], n: int, mult: float) -> tuple[float, float, float, float]:
    """SMA / upper / lower / width on the last `n` closes (continuous 5m series)."""
    w = closes[-n:] if len(closes) >= n else closes[:]
    sma = sum(w) / len(w)
    var = sum((c - sma) ** 2 for c in w) / len(w)   # population std (matches np.std default)
    sd = math.sqrt(var)
    upper, lower = sma + mult * sd, sma - mult * sd
    return sma, upper, lower, (upper - lower)


def decide_band_trade(
    closes: list[float],
    r5: float,
    periods_remaining: float,
    r5_median: float | None,
    width_median: float | None,
    p: BandParams = BandParams(),
) -> BandDecision | None:
    """Return the validated band-anchored trade, or None if the day is gated out.

    `closes`           continuous 5m closes through the entry bar (last is S0=entry price)
    `r5`               today's session realized 5m std (open→entry), lookahead-safe
    `periods_remaining` 5m periods to 16:00
    `r5_median`        running median of prior days' r5 (None until ≥~20 days → no gate yet)
    `width_median`     running median of prior days' band width (None → not choppy)
    """
    if not closes or r5 <= 0 or periods_remaining <= 1:
        return None
    S0 = closes[-1]
    sma, upper, lower, width = bollinger(closes, p.bb_len, p.bb_mult)

    # ── Schwartz vol-released gate ── (only once enough history to know the median)
    vol_released = (r5_median is None) or (r5 > r5_median)
    if r5_median is not None and not vol_released:
        return None

    choppy = (width_median is not None and width > width_median)
    atr = max(1.0, width / 4.0)
    downtrend = S0 < sma
    side = "sell_put_cs" if downtrend else "sell_call_cs"
    sk = p.put_skew if downtrend else p.call_skew
    tv0 = bs.total_vol_to_expiry(r5, periods_remaining, p.premium_mult) * sk

    # band-anchored short, pushed further OTM on choppy days; then walk toward spot to credit floor
    if downtrend:
        short = float(round(lower - (p.choppy_push_atr * atr if choppy else 0.0)))
        sign, cap = +1.0, S0 * (1 - p.min_otm_pct / 100.0)
    else:
        short = float(round(upper + (p.choppy_push_atr * atr if choppy else 0.0)))
        sign, cap = -1.0, S0 * (1 + p.min_otm_pct / 100.0)

    def credit_at(s: float) -> tuple[float, float]:
        lng = s - p.wing if downtrend else s + p.wing
        return bs.spread_value(side, S0, s, lng, tv0) * MULT, lng

    cr, lng = credit_at(short)
    tries = 0
    while cr < p.credit_floor and tries < 60:
        ns = short + sign * STEP
        if (downtrend and ns > cap) or ((not downtrend) and ns < cap):
            break   # would get too close to spot — stop walking
        short = ns
        cr, lng = credit_at(short)
        tries += 1
    if cr < p.credit_floor:
        return None   # can't get paid enough without too much risk

    # ── cushion filter ── skip thin-cushion days (net-negative subset; failure analysis)
    pct_otm = 100.0 * abs(S0 - short) / S0
    if pct_otm < p.cushion_pct:
        return None

    return BandDecision(side=side, short_strike=short, long_strike=lng, est_credit=cr,
                        vol_released=bool(vol_released), choppy=choppy, pct_otm=pct_otm, r5=r5)


def seed_running_medians(bb_len: int = 14, bb_mult: float = 2.5) -> "RunningMedians":
    """Build a RunningMedians seeded from the historical SPX_5m_3y.json so the
    Schwartz/cushion gate is calibrated from day one — the live expanding median
    == the validated backtest's.

    Replays the SAME data through the SAME per-day accumulation as
    `scripts/test_wave_band_parity.run_live` (and the backtest main loop): for each
    ET day with >=20 bars, take the entry bar at/after ENTRY_MIN (~10:00 ET), form
    the session-so-far closes `pre = closes[day_open .. entry]`, require >=5 of them,
    compute r5 = realized_5m_std(pre) and the entry band width = bollinger(buf, n, mult)
    over the continuous series `buf = closes[:entry+1]`, and record (r5, width) on EVERY
    qualifying day where r5 > 0 — EXACTLY mirroring the test so the medians match to
    the cent. No lookahead: each value is from session-open→entry only.

    Imports the backtest's own loader/helpers so live and validation share one path.
    """
    # Imported here (not at module top) so the pure decision core has no script dep.
    from scripts.wave_band_backtest import _load, _minute, ENTRY_MIN, ET
    from collections import defaultdict

    bars = _load()
    closes = [b[3] for b in bars]
    by_day: dict[str, list[int]] = defaultdict(list)
    for i, b in enumerate(bars):
        et = b[0].astimezone(ET) if b[0].tzinfo else b[0]
        by_day[et.strftime("%Y-%m-%d")].append(i)

    rm = RunningMedians()
    for date in sorted(by_day):
        idxs = by_day[date]
        if len(idxs) < 20:
            continue
        eidx = next((i for i in idxs if _minute(bars[i][0]) >= ENTRY_MIN), None)
        if eidx is None:
            continue
        pre = closes[idxs[0]:eidx + 1]
        if len(pre) < 5:
            continue
        r5 = bs.realized_5m_std(list(pre))
        buf = closes[:eidx + 1]                       # continuous series through entry
        _, _, _, width = bollinger(buf, bb_len, bb_mult)
        if r5 > 0:
            rm.record(r5, width)
    return rm


class RunningMedians:
    """Tracks per-day r5 and band-width history → running medians for the gates.
    Persist `r5_hist`/`width_hist` across sessions; append ONE value per day AFTER the
    entry decision (no lookahead). Seed from history so the gates are live from day 1."""

    def __init__(self, r5_hist: list[float] | None = None,
                 width_hist: list[float] | None = None, window: int | None = None, min_n: int = 20):
        # window=None → EXPANDING median (matches the validated backtest). A finite window
        # would be more adaptive but is NOT what was validated — keep None unless re-validated.
        self.r5_hist = list(r5_hist or [])
        self.width_hist = list(width_hist or [])
        self.window, self.min_n = window, min_n

    def r5_median(self) -> float | None:
        return st.median(self.r5_hist) if len(self.r5_hist) >= self.min_n else None

    def width_median(self) -> float | None:
        return st.median(self.width_hist) if len(self.width_hist) >= self.min_n else None

    def record(self, r5: float, width: float) -> None:
        self.r5_hist.append(r5)
        self.width_hist.append(width)
        if self.window is not None:
            self.r5_hist = self.r5_hist[-self.window:]
            self.width_hist = self.width_hist[-self.window:]
