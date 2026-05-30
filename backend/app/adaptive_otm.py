"""Adaptive %OTM selection for the IC builder, based on the day's VIX.

Calibrated against 12 months of SPX 5m + daily VIX (2024-11 to 2026-05) — see
scripts/vix_otm_analysis.py for the empirical breakdown that produced these
buckets. Net rule:

   VIX < 13        →  0.8% OTM (~15Δ)   low-vol, moderate width
   VIX 13-15       →  0.9% OTM (~12Δ)   normal — strategy's sweet spot
   VIX 15-18       →  1.0% OTM (~10Δ)   medium — still good EV
   VIX 18-22       →  1.5% OTM (~5Δ)    wide — marginal but still trade
   VIX >= 22       →  STAND ASIDE       net negative across all widths

Tunable via .env:
   IC_VIX_BUCKETS="13:0.5,15:0.5,18:1.0,22:1.5,99:0"
   (each pair: <upper_vix>:<pct_otm>; 0 means stand aside)
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional


log = logging.getLogger(__name__)


# Default rule from the 12-month backtest. Each tuple is (upper_vix_bound, pct_otm).
# A bucket matches if VIX < upper_bound. Use pct_otm=0 to mean STAND ASIDE.
DEFAULT_BUCKETS = [
    (13.0, 0.8),  # ultra-low → moderate (0.5% was too tight, low win rate)
    (15.0, 0.9),  # low-normal → sweet spot (~12Δ, survivable daily range)
    (18.0, 1.0),  # normal   → medium (bulk of trading days, best aggregate)
    (22.0, 1.5),  # elevated → wide (still trade but small EV)
    (99.0, 0.0),  # >= 22    → stand aside
]


@dataclass
class OtmDecision:
    pct_otm: float | None       # None = stand aside
    bucket_label: str
    rationale: str


def _parse_buckets_env() -> list[tuple[float, float]] | None:
    """Parse IC_VIX_BUCKETS env if set (e.g. '13:0.5,15:0.5,18:1.0,22:1.5,99:0').
    Returns list of (upper_bound, pct_otm) sorted ascending. None if env unset."""
    raw = os.environ.get("IC_VIX_BUCKETS", "").strip()
    if not raw:
        return None
    try:
        out = []
        for chunk in raw.split(","):
            ub, pct = chunk.split(":")
            out.append((float(ub), float(pct)))
        out.sort(key=lambda t: t[0])
        return out
    except Exception as e:
        log.warning("IC_VIX_BUCKETS parse failed (%r): %s — falling back to defaults", raw, e)
        return None


def pick_pct_otm(vix: float | None) -> OtmDecision:
    """Map current VIX to the empirically-best %OTM, or signal stand-aside."""
    buckets = _parse_buckets_env() or DEFAULT_BUCKETS
    if vix is None:
        # Failsafe: assume normal-vol regime. 1.0% OTM is the most-trades best-EV bucket.
        return OtmDecision(
            pct_otm=1.0,
            bucket_label="VIX unknown",
            rationale="VIX unavailable — defaulting to 1.0% OTM (normal-vol bucket)",
        )

    for upper, pct in buckets:
        if vix < upper:
            if pct <= 0:
                return OtmDecision(
                    pct_otm=None,
                    bucket_label=f"VIX {vix:.1f} (>= {upper - (upper - 22):.0f})",
                    rationale=(f"VIX {vix:.1f} too high — historically all %OTM choices "
                               f"net negative in this bucket. STAND ASIDE."),
                )
            return OtmDecision(
                pct_otm=pct,
                bucket_label=f"VIX {vix:.1f} (< {upper:.0f})",
                rationale=(f"VIX {vix:.1f} → {pct}% OTM. "
                           f"Backtest: this bucket × this %OTM was the best-EV combo."),
            )
    # Above all bucket bounds → stand aside
    return OtmDecision(
        pct_otm=None,
        bucket_label=f"VIX {vix:.1f} extreme",
        rationale=f"VIX {vix:.1f} above all configured buckets — STAND ASIDE.",
    )
