"""Wave gospel backtest — validates Phase 1, 2, 3 gates against historical data.

Runs the orchestrator's logic on 12mo SPX 5m history and compares:
  • UNGATED baseline: 0.5% OTM, intra-bar STOP, fixed-% TP, 15min TIME
  • PHASE 1:           + gates (confluence, VIX, max-trades, blackout)
  • PHASE 3:           1.5% OTM canonical, STOP-on-close, 30min TIME, gates

Phase 3 implements canonical 0DTE (TradingBlock + CBOE + 0-dte.com):
  • 1.5% OTM strikes (was 0.5%) — 10-15Δ short, NOT 25Δ
  • STOP only on bar CLOSE through strike (not intra-bar wick)
  • 30 min TIME stop (was 15 min) — wider gamma buffer
  • 14:00 ET no-new-entry cutoff (was 14:30)
  • Credit/loss heuristics scaled by strike distance (matches IC backtest presets)

Usage:
  cd backend && ../.venv/bin/python -m scripts.backtest_phase1_gospel
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT.parent
ENV_PATH = PROJECT_ROOT / ".env"

# Load .env BEFORE importing predictor so predictor's module-level env reads pick up
# WAVE_NO_NEW_ENTRY_AFTER_ET=14:00 (Phase 3) instead of the default 14:30.
if ENV_PATH.exists():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass

sys.path.insert(0, str(ROOT))

from app.predictor import (
    Bar, Signal, run_backtest,
    EMA_FAST_LEN, EMA_SLOW_LEN, _ema, _classify_trend,
)
from app.adaptive_otm import pick_pct_otm

ET = ZoneInfo("America/New_York")

# ── Phase 1 gate thresholds (mirror .env defaults) ──
WAVE_MIN_CONFLUENCE_SCORE = 3
WAVE_FULL_SIZE_CONFLUENCE = 4
MAX_TRADES_PER_DAY = 3
RSI_CALL_THR = 65
RSI_PUT_THR = 35
NEAR_EMA10_PCT = 0.30  # within 0.3% of EMA10 = "fresh"

# ── Phase 2 thresholds (mirror .env defaults) ──
WAVE_TP_ATR_MULT = 0.4         # TP = 0.4 × ATR(D1)
WAVE_SAMEBAR_EXIT_GUARD = True
WAVE_MIDSESSION_REGATE = True
WAVE_MIDSESSION_VOL_MULT = 2.0

# ── Trade simulation params (Phase 3 canonical defaults) ──
WING_WIDTH = 5.0              # XSP 5pt wing
WAVE_FAVORABLE_MOVE_PCT = 0.3  # 0.3% favorable underlying move = TP fires
MULTIPLIER = 100

# Per-phase strike distance & STOP behavior
PHASE_CONFIG = {
    0: {"pct_otm": 0.5, "stop_on_close": False, "time_stop_min": 15, "vwap_gate": False, "prime_window": False, "label": "UNGATED (legacy 0.5% OTM, wick STOP)"},
    1: {"pct_otm": 0.5, "stop_on_close": False, "time_stop_min": 15, "vwap_gate": False, "prime_window": False, "label": "PHASE 1 (gates, 0.5% OTM, wick STOP)"},
    3: {"pct_otm": 1.5, "stop_on_close": True,  "time_stop_min": 30, "vwap_gate": False, "prime_window": False, "label": "PHASE 3 (canonical 1.5% OTM, close STOP, 30min TIME)"},
    4: {"pct_otm": 1.5, "stop_on_close": True,  "time_stop_min": 30, "vwap_gate": True,  "prime_window": True,  "label": "PHASE 4 (Phase 3 + VWAP gate + prime-window factor)"},
}

# Credit/max-loss heuristics by strike distance (mirrors dashboard IC presets, scaled
# to single-side wave). Source: backtest_api.py BT_PRESETS.
# At %OTM, credit_pct_of_wing × wing × multiplier = credit per spread.
def credit_for_pct_otm(pct_otm: float) -> float:
    """Approximate credit ($) for a single-side credit spread on XSP 5pt wing
    at the given strike distance. Linear interpolation between known presets."""
    presets = [(0.5, 175), (1.0, 100), (1.5, 60), (2.0, 35)]
    for i, (p, c) in enumerate(presets):
        if pct_otm <= p:
            if i == 0 or pct_otm == p:
                return float(c)
            p_lo, c_lo = presets[i - 1]
            return c_lo + (c - c_lo) * (pct_otm - p_lo) / (p - p_lo)
    # Beyond 2.0% — extrapolate down
    return max(15.0, 35 - (pct_otm - 2.0) * 10)


@dataclass
class TradeResult:
    date: str
    side: str
    entry_time: str
    entry_price: float
    short_strike: float
    tp_target: float
    confluence_score: int
    confluence_dict: dict
    outcome: str       # "TP" / "STOP" / "TIME" / "EOD"
    pnl: float
    bars_held: int
    gated: bool        # True if Phase 1 would have skipped this signal
    gate_reason: str   # Why it was gated (or "passed")


def load_spx_bars() -> list[Bar]:
    """Load 12-month SPX 5m bars."""
    path = ROOT / "data" / "historical" / "SPX_5m_1y.json"
    if not path.exists():
        path = ROOT / "data" / "historical" / "SPX_5m_60d.json"
    print(f"Loading {path.name}...")
    raw = json.loads(path.read_text())
    bars = [
        Bar(
            time=datetime.fromisoformat(r["datetime"]),
            open=r["open"], high=r["high"], low=r["low"],
            close=r["close"], volume=r.get("volume", 0) or 0,
        )
        for r in raw
    ]
    print(f"  loaded {len(bars)} bars across {len(set(b.time.astimezone(ET).date() for b in bars))} sessions")
    return bars


def fetch_vix_daily(start: str, end: str) -> dict[str, float]:
    """Fetch daily VIX close keyed by 'YYYY-MM-DD'. Reuses yfinance."""
    try:
        import yfinance as yf
        print(f"Fetching VIX from {start} to {end}...")
        df = yf.download("^VIX", start=start, end=end, interval="1d",
                         progress=False, auto_adjust=False)
        if df is None or df.empty:
            print("  WARNING: empty VIX data, defaulting to None")
            return {}
        out = {}
        closes = df["Close"].values.flatten()
        for i, idx in enumerate(df.index):
            out[idx.strftime("%Y-%m-%d")] = float(closes[i])
        print(f"  loaded {len(out)} VIX daily closes")
        return out
    except Exception as e:
        print(f"  VIX fetch failed: {e}")
        return {}


def vix_for_session(date_str: str, vix_map: dict) -> float | None:
    """Prior-trading-day VIX (= 'expected vol entering this session')."""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    for back in range(1, 6):
        prior = (d - timedelta(days=back)).strftime("%Y-%m-%d")
        if prior in vix_map:
            return vix_map[prior]
    return None


def compute_atr_d1(bars: list[Bar], period: int = 14) -> dict[str, float]:
    """D1 ATR per session date. Used for regime classification."""
    by_date = {}
    for b in bars:
        d = b.time.astimezone(ET).strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)
    daily = []
    dates_sorted = sorted(by_date.keys())
    for d in dates_sorted:
        day = by_date[d]
        daily.append((d, max(b.high for b in day), min(b.low for b in day), day[-1].close))
    out = {}
    for i, (d, h, l, c) in enumerate(daily):
        if i == 0:
            tr = h - l
        else:
            prev_c = daily[i - 1][3]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        if i < period:
            continue
        last_n_tr = []
        for j in range(i - period + 1, i + 1):
            ph, pl = daily[j][1], daily[j][2]
            pc = daily[j - 1][3] if j > 0 else (ph - pl)
            tr_j = max(ph - pl, abs(ph - pc), abs(pl - pc))
            last_n_tr.append(tr_j)
        out[d] = sum(last_n_tr) / period
    return out


def compute_session_vwap(bars_so_far: list[Bar]) -> float | None:
    """Phase 4: session VWAP from all bars in current session up to signal time."""
    if not bars_so_far:
        return None
    session_date = bars_so_far[0].time.astimezone(ET).strftime("%Y-%m-%d")
    cum_pv = 0.0
    cum_v = 0.0
    for b in bars_so_far:
        if b.time.astimezone(ET).strftime("%Y-%m-%d") != session_date:
            continue
        typical = (b.high + b.low + b.close) / 3.0
        v = max(b.volume or 1, 1)
        cum_pv += typical * v
        cum_v += v
    return cum_pv / cum_v if cum_v > 0 else None


def compute_confluence(
    sig: Signal,
    bars_so_far: list[Bar],
    vix_value: float | None,
    macro_clear: bool = True,
    include_prime_window: bool = False,
) -> tuple[int, dict]:
    """Compute variable confluence factors at signal time.
    Phase 1: 5 factors. Phase 4: +1 (prime-window) = 6 factors."""
    confluence = {}

    # 1. RSI directional
    if sig.side == "sell_call_cs":
        confluence["rsi_overbought"] = bool(sig.rsi is not None and sig.rsi > RSI_CALL_THR)
    else:
        confluence["rsi_oversold"] = bool(sig.rsi is not None and sig.rsi < RSI_PUT_THR)

    # 2. WVF spike
    confluence["wvf_spike"] = bool(sig.wvf_spike)

    # 3. Macro clear (default True for backtest — Finnhub historical not loaded)
    confluence["macro_clear"] = macro_clear

    # 4. VIX bucket OK (not stand-aside)
    decision = pick_pct_otm(vix_value)
    confluence["vix_bucket_ok"] = decision.pct_otm is not None

    # 5. Near EMA10
    closes = np.array([b.close for b in bars_so_far])
    ema_fast = _ema(closes, EMA_FAST_LEN)
    if len(closes) > 0 and not np.isnan(ema_fast[-1]) and sig.entry_price > 0:
        ext_pct = abs(sig.entry_price - ema_fast[-1]) / sig.entry_price * 100.0
        confluence["near_ema10"] = ext_pct < NEAR_EMA10_PCT
    else:
        confluence["near_ema10"] = False

    # 6. Phase 4: prime-window (10:30-13:00 ET = canonical sweet spot)
    if include_prime_window:
        et = sig.time.astimezone(ET) if sig.time.tzinfo else sig.time
        bar_min = et.hour * 60 + et.minute
        confluence["in_prime_window"] = (10 * 60 + 30) <= bar_min < (13 * 60)

    return sum(1 for v in confluence.values() if v), confluence


def simulate_exit(
    sig: Signal,
    session_bars: list[Bar],
    short_strike: float,
    tp_target: float,
    credit: float,
    same_bar_guard: bool = False,
    stop_on_close: bool = False,
    time_stop_min: int = 15,
) -> tuple[str, float, int]:
    """Walk forward through session bars, return (outcome, pnl, bars_held).

    Priority: STOP > TP > TIME > EOD.

    Phase 2 same_bar_guard: TP/TIME can't fire on the entry bar (only STOP can).
    Phase 3 stop_on_close: STOP fires only when bar.close is through strike,
                            not on intra-bar wick.
    Phase 3 time_stop_min: configurable TIME stop window (was hardcoded 15).

    P&L per outcome scales with credit collected (Phase 3 — vol-aware):
      TP   = 0.75 × credit (close at 25% remaining premium)
      STOP = -(wing × multiplier - credit) (max loss)
      TIME = 0.40 × credit (rough breakeven late in day)
      EOD  = credit (full max profit if expired OTM)
    """
    max_loss = WING_WIDTH * MULTIPLIER - credit
    tp_pnl = credit * 0.75
    stop_pnl = -max_loss
    time_pnl = credit * 0.40
    eod_pnl = credit

    bars_held = 0
    for b in session_bars:
        if b.time < sig.time:
            continue
        is_entry_bar = b.time == sig.time

        # STOP check — uses bar.close (Phase 3) or bar.high/low (legacy)
        if stop_on_close:
            call_breach = sig.side == "sell_call_cs" and b.close >= short_strike
            put_breach  = sig.side == "sell_put_cs"  and b.close <= short_strike
        else:
            call_breach = sig.side == "sell_call_cs" and b.high >= short_strike
            put_breach  = sig.side == "sell_put_cs"  and b.low  <= short_strike

        if is_entry_bar and same_bar_guard:
            # Phase 2: STOP fires on entry bar (capital protection); TP/TIME do not
            if call_breach or put_breach:
                return "STOP", stop_pnl, bars_held
            bars_held += 1
            continue

        bars_held += 1
        et = b.time.astimezone(ET)
        bar_min = et.hour * 60 + et.minute

        # STOP
        if call_breach or put_breach:
            return "STOP", stop_pnl, bars_held

        # TP — favorable move
        if sig.side == "sell_call_cs" and b.low <= tp_target:
            return "TP", tp_pnl, bars_held
        if sig.side == "sell_put_cs" and b.high >= tp_target:
            return "TP", tp_pnl, bars_held

        # TIME — N min before close
        if bar_min >= (16 * 60 - time_stop_min) and bar_min < 16 * 60:
            return "TIME", time_pnl, bars_held

        # EOD
        if bar_min >= 16 * 60:
            return "EOD", eod_pnl, bars_held

    return "EOD", eod_pnl, bars_held


def check_mid_session_volatility(
    session_bars: list[Bar],
    sig_time: datetime,
    atr_d1: float | None,
) -> bool:
    """Returns True if rolling 30-min realized range > MULT × expected at any point
    BETWEEN obs end (09:45) and signal time. If so, the session is "locked"
    volatile and this signal would be blocked under Phase 2.
    """
    if not WAVE_MIDSESSION_REGATE or atr_d1 is None or atr_d1 <= 0:
        return False
    expected_30m = atr_d1 / 3.61  # sqrt(78/6) ≈ 3.61
    threshold = WAVE_MIDSESSION_VOL_MULT * expected_30m
    open_minute = 9 * 60 + 30
    obs_end_minute = open_minute + 15
    close_minute = 16 * 60

    # Iterate bars from obs end up to (and including) signal bar; track 30m rolling
    eligible = [b for b in session_bars
                if b.time <= sig_time
                and (b.time.astimezone(ET).hour * 60 + b.time.astimezone(ET).minute) >= obs_end_minute
                and (b.time.astimezone(ET).hour * 60 + b.time.astimezone(ET).minute) < close_minute]
    if len(eligible) < 6:
        return False
    for i in range(5, len(eligible)):
        window = eligible[i - 5: i + 1]  # 6 bars = 30 min
        range_30m = max(b.high for b in window) - min(b.low for b in window)
        if range_30m > threshold:
            return True
    return False


def apply_gates(
    sig: Signal,
    confluence_score: int,
    confluence: dict,
    vix_value: float | None,
    trades_today_count: int,
    phase: int = 1,
    session_bars: list | None = None,
    atr_d1: float | None = None,
) -> tuple[bool, str]:
    """Returns (passed, reason). Phase 1 = 4 gates. Phase 2 = +mid-session re-gate."""
    # GATE 1: Confluence threshold
    if confluence_score < WAVE_MIN_CONFLUENCE_SCORE:
        return False, f"confluence {confluence_score}/5 < {WAVE_MIN_CONFLUENCE_SCORE}/5"

    # GATE 2: Macro blackout — skipped in backtest (defaults clear)

    # GATE 3: Max trades per day
    if trades_today_count >= MAX_TRADES_PER_DAY:
        return False, f"daily cap {trades_today_count}/{MAX_TRADES_PER_DAY}"

    # GATE 4: VIX bucket
    decision = pick_pct_otm(vix_value)
    if decision.pct_otm is None:
        return False, f"VIX bucket {decision.bucket_label}"

    # GATE 4b (Phase 2): Mid-session vol re-gate
    if phase >= 2 and session_bars is not None:
        if check_mid_session_volatility(session_bars, sig.time, atr_d1):
            return False, "mid-session vol spike"

    return True, "passed"


def run_one_strategy(
    bars: list[Bar],
    atr_d1_map: dict,
    vix_map: dict,
    phase: int,            # 0=ungated, 1=Phase 1 gates, 3=Phase 3 (canonical)
    label: str,
) -> dict:
    """Run the predictor → simulate exits with the configured phase logic."""
    cfg = PHASE_CONFIG.get(phase, PHASE_CONFIG[0])
    pct_otm        = cfg["pct_otm"]
    stop_on_close  = cfg["stop_on_close"]
    time_stop_min  = cfg["time_stop_min"]
    use_vwap_gate  = cfg.get("vwap_gate", False)
    use_prime_win  = cfg.get("prime_window", False)
    credit         = credit_for_pct_otm(pct_otm)

    print(f"  [{label}] pct_otm={pct_otm}% credit=${credit:.0f} "
          f"stop_on_close={stop_on_close} time_stop_min={time_stop_min} "
          f"vwap_gate={use_vwap_gate} prime_window={use_prime_win}")

    # Group bars by session for exit walk-forward
    by_date: dict[str, list[Bar]] = {}
    for b in bars:
        d = b.time.astimezone(ET).strftime("%Y-%m-%d")
        by_date.setdefault(d, []).append(b)
    for d in by_date:
        by_date[d].sort(key=lambda b: b.time)

    sessions = run_backtest(bars, lambda d: atr_d1_map.get(d))

    trades: list[TradeResult] = []
    gate_reasons = Counter()
    trades_per_day = Counter()

    same_bar_guard = (phase >= 2) and WAVE_SAMEBAR_EXIT_GUARD

    for ses in sessions:
        date_str = ses.session_date
        session_bars = by_date.get(date_str, [])
        if not session_bars:
            continue
        vix_value = vix_for_session(date_str, vix_map)
        atr_d1 = atr_d1_map.get(date_str)

        for sig in sorted(ses.signals, key=lambda s: s.time):
            bars_so_far = [b for b in session_bars if b.time <= sig.time]
            conf_score, conf_dict = compute_confluence(
                sig, bars_so_far, vix_value, macro_clear=True,
                include_prime_window=use_prime_win,
            )

            # Phase 4: VWAP gate (hard) — applied BEFORE other gates
            if use_vwap_gate:
                vwap = compute_session_vwap(bars_so_far)
                if vwap is not None:
                    if sig.side == "sell_call_cs" and sig.entry_price <= vwap:
                        gate_reasons["VWAP misaligned (CALL @ ≤ VWAP)"] += 1
                        trades.append(TradeResult(
                            date=date_str, side=sig.side,
                            entry_time=sig.time.isoformat(),
                            entry_price=sig.entry_price,
                            short_strike=0, tp_target=0,
                            confluence_score=conf_score, confluence_dict=conf_dict,
                            outcome="GATED", pnl=0.0, bars_held=0,
                            gated=True, gate_reason="VWAP misaligned (CALL ≤ VWAP)",
                        ))
                        continue
                    if sig.side == "sell_put_cs" and sig.entry_price >= vwap:
                        gate_reasons["VWAP misaligned (PUT @ ≥ VWAP)"] += 1
                        trades.append(TradeResult(
                            date=date_str, side=sig.side,
                            entry_time=sig.time.isoformat(),
                            entry_price=sig.entry_price,
                            short_strike=0, tp_target=0,
                            confluence_score=conf_score, confluence_dict=conf_dict,
                            outcome="GATED", pnl=0.0, bars_held=0,
                            gated=True, gate_reason="VWAP misaligned (PUT ≥ VWAP)",
                        ))
                        continue

            if phase >= 1:
                passed, reason = apply_gates(
                    sig, conf_score, conf_dict, vix_value,
                    trades_per_day[date_str],
                    phase=phase,
                    session_bars=session_bars,
                    atr_d1=atr_d1,
                )
                if not passed:
                    gate_reasons[reason] += 1
                    trades.append(TradeResult(
                        date=date_str, side=sig.side,
                        entry_time=sig.time.isoformat(),
                        entry_price=sig.entry_price,
                        short_strike=0, tp_target=0,
                        confluence_score=conf_score,
                        confluence_dict=conf_dict,
                        outcome="GATED",
                        pnl=0.0, bars_held=0,
                        gated=True, gate_reason=reason,
                    ))
                    continue

            # Strike at phase-configured % OTM
            if sig.side == "sell_call_cs":
                short_strike = sig.entry_price * (1 + pct_otm / 100)
            else:
                short_strike = sig.entry_price * (1 - pct_otm / 100)

            # TP target — fixed-% favorable move (vol-scaled disabled by default)
            tp_dist = sig.entry_price * (WAVE_FAVORABLE_MOVE_PCT / 100)
            if sig.side == "sell_call_cs":
                tp_target = sig.entry_price - tp_dist
            else:
                tp_target = sig.entry_price + tp_dist

            outcome, pnl, bars_held = simulate_exit(
                sig, session_bars, short_strike, tp_target,
                credit=credit,
                same_bar_guard=same_bar_guard,
                stop_on_close=stop_on_close,
                time_stop_min=time_stop_min,
            )

            trades.append(TradeResult(
                date=date_str, side=sig.side,
                entry_time=sig.time.isoformat(),
                entry_price=sig.entry_price,
                short_strike=short_strike, tp_target=tp_target,
                confluence_score=conf_score,
                confluence_dict=conf_dict,
                outcome=outcome, pnl=pnl, bars_held=bars_held,
                gated=False, gate_reason="passed",
            ))
            trades_per_day[date_str] += 1

    # Aggregate stats (only NON-gated trades count toward P&L)
    actual_trades = [t for t in trades if not t.gated]
    n_total = len(actual_trades)
    n_wins = sum(1 for t in actual_trades if t.pnl > 0)
    outcomes = Counter(t.outcome for t in actual_trades)
    total_pnl = sum(t.pnl for t in actual_trades)
    n_sessions = len(set(t.date for t in actual_trades))

    return {
        "label": label,
        "n_total_signals": len(trades),  # raw + gated
        "n_actual_trades": n_total,
        "n_gated": len(trades) - n_total,
        "n_sessions": n_sessions,
        "win_rate_pct": round(100 * n_wins / n_total, 1) if n_total else 0,
        "total_pnl": round(total_pnl, 2),
        "avg_pnl_per_trade": round(total_pnl / n_total, 2) if n_total else 0,
        "avg_pnl_per_session": round(total_pnl / n_sessions, 2) if n_sessions else 0,
        "outcomes": dict(outcomes),
        "gate_reasons": dict(gate_reasons),
        "trades": trades,
    }


def print_summary_3way(ungated: dict, phase1: dict, phase2: dict):
    """3-way comparison: UNGATED / PHASE 1 / PHASE 2."""
    print()
    print("=" * 100)
    print("WAVE GOSPEL BACKTEST — 3-WAY COMPARISON")
    print("=" * 100)
    print()
    rows = [
        ("Total raw signals",      ungated["n_actual_trades"], phase1["n_total_signals"], phase2["n_total_signals"]),
        ("Trades fired",           ungated["n_actual_trades"], phase1["n_actual_trades"], phase2["n_actual_trades"]),
        ("Trades GATED (skipped)", 0, phase1["n_gated"], phase2["n_gated"]),
        ("Win rate %",             f"{ungated['win_rate_pct']}%", f"{phase1['win_rate_pct']}%", f"{phase2['win_rate_pct']}%"),
        ("Total P&L $",            f"${ungated['total_pnl']:,.0f}", f"${phase1['total_pnl']:,.0f}", f"${phase2['total_pnl']:,.0f}"),
        ("Avg P&L per trade $",    f"${ungated['avg_pnl_per_trade']:,.2f}", f"${phase1['avg_pnl_per_trade']:,.2f}", f"${phase2['avg_pnl_per_trade']:,.2f}"),
        ("Avg P&L per session $",  f"${ungated['avg_pnl_per_session']:,.2f}", f"${phase1['avg_pnl_per_session']:,.2f}", f"${phase2['avg_pnl_per_session']:,.2f}"),
    ]
    print(f"{'Metric':<28}  {'UNGATED (baseline)':<22}  {'PHASE 1 (gates)':<22}  {'PHASE 2 (+ exits)':<22}")
    print("-" * 100)
    for label, u, p1, p2 in rows:
        print(f"{label:<28}  {str(u):<22}  {str(p1):<22}  {str(p2):<22}")
    print()

    print("Outcome distribution:")
    for outcome in ["TP", "STOP", "TIME", "EOD"]:
        u = ungated["outcomes"].get(outcome, 0)
        p1 = phase1["outcomes"].get(outcome, 0)
        p2 = phase2["outcomes"].get(outcome, 0)
        u_pct = 100 * u / ungated["n_actual_trades"] if ungated["n_actual_trades"] else 0
        p1_pct = 100 * p1 / phase1["n_actual_trades"] if phase1["n_actual_trades"] else 0
        p2_pct = 100 * p2 / phase2["n_actual_trades"] if phase2["n_actual_trades"] else 0
        print(f"  {outcome:<6}  ungated={u:4d} ({u_pct:5.1f}%)   phase1={p1:4d} ({p1_pct:5.1f}%)   phase2={p2:4d} ({p2_pct:5.1f}%)")
    print()

    print("Phase 2 gate rejections:")
    if phase2["gate_reasons"]:
        total = sum(phase2["gate_reasons"].values())
        for reason, count in sorted(phase2["gate_reasons"].items(), key=lambda x: -x[1]):
            pct = 100 * count / total
            print(f"  {count:4d} ({pct:5.1f}%) — {reason}")
    print()

    print("=" * 100)
    print("HEADLINE")
    print("=" * 100)
    for label, d in [("UNGATED", ungated), ("PHASE 1", phase1), ("PHASE 2", phase2)]:
        verdict = "✓ PROFITABLE" if d["total_pnl"] > 0 else "✗ losing money"
        print(f"  {label:<10}  trades={d['n_actual_trades']:4d}  WR={d['win_rate_pct']:>5}%  P&L=${d['total_pnl']:>+8,.0f}  {verdict}")
    print()
    delta_p1 = phase1["total_pnl"] - ungated["total_pnl"]
    delta_p2 = phase2["total_pnl"] - phase1["total_pnl"]
    print(f"  Phase 1 lift over baseline: ${delta_p1:+,.0f}")
    print(f"  Phase 2 lift over Phase 1:  ${delta_p2:+,.0f}")
    print()


def main():
    bars = load_spx_bars()
    if not bars:
        print("No bars loaded — aborting.")
        return

    # Date range for VIX
    dates_sorted = sorted(set(b.time.astimezone(ET).strftime("%Y-%m-%d") for b in bars))
    vix_map = fetch_vix_daily(dates_sorted[0], dates_sorted[-1])

    print("Computing daily ATR(14)...")
    atr_d1_map = compute_atr_d1(bars)

    print("Running UNGATED baseline (0.5% OTM, intra-bar STOP, 15min TIME)...")
    ungated = run_one_strategy(bars, atr_d1_map, vix_map, phase=0, label="UNGATED")

    print("Running PHASE 1 (gates, 0.5% OTM, intra-bar STOP, 15min TIME)...")
    phase1 = run_one_strategy(bars, atr_d1_map, vix_map, phase=1, label="PHASE 1")

    print("Running PHASE 3 (canonical: 1.5% OTM, close-only STOP, 30min TIME)...")
    phase3 = run_one_strategy(bars, atr_d1_map, vix_map, phase=3, label="PHASE 3")

    print("Running PHASE 4 (Phase 3 + VWAP gate + prime-window factor)...")
    phase4 = run_one_strategy(bars, atr_d1_map, vix_map, phase=4, label="PHASE 4")

    print()
    print("=" * 100)
    print("EVOLUTION SUMMARY (12mo SPX 5m)")
    print("=" * 100)
    print(f"{'Variant':<60}  {'Trades':>7}  {'WR':>6}  {'P&L $':>12}  {'Avg/trade':>12}")
    print("-" * 100)
    for label_str, d in [("UNGATED baseline (legacy 0.5% OTM, wick STOP)", ungated),
                          ("PHASE 1 (gates only)", phase1),
                          ("PHASE 3 (canonical 1.5% OTM, close STOP)", phase3),
                          ("PHASE 4 (+ VWAP gate + prime-window factor)", phase4)]:
        print(f"{label_str:<60}  {d['n_actual_trades']:>7}  {d['win_rate_pct']:>5}%  "
              f"${d['total_pnl']:>+10,.0f}  ${d['avg_pnl_per_trade']:>+10,.2f}")
    print()
    delta_p1 = phase1["total_pnl"] - ungated["total_pnl"]
    delta_p3 = phase3["total_pnl"] - phase1["total_pnl"]
    delta_p4 = phase4["total_pnl"] - phase3["total_pnl"]
    print(f"  Phase 1 lift: ${delta_p1:+,.0f}    Phase 3 lift: ${delta_p3:+,.0f}    Phase 4 lift: ${delta_p4:+,.0f}")
    print()
    best_phase = max([("Phase 1", phase1), ("Phase 3", phase3), ("Phase 4", phase4)],
                     key=lambda x: x[1]["total_pnl"])
    best_avg   = max([("Phase 1", phase1), ("Phase 3", phase3), ("Phase 4", phase4)],
                     key=lambda x: x[1]["avg_pnl_per_trade"])
    print(f"  → Highest TOTAL P&L: {best_phase[0]} (${best_phase[1]['total_pnl']:+,.0f})")
    print(f"  → Highest AVG/trade: {best_avg[0]} (${best_avg[1]['avg_pnl_per_trade']:+,.2f})")
    print()

    # Scale projections
    for label_str, d in [("Phase 3", phase3), ("Phase 4", phase4)]:
        if d["total_pnl"] > 0 and d["n_sessions"] > 0:
            per_day = d["total_pnl"] / d["n_sessions"]
            scale_10 = per_day * 10
            print(f"  {label_str}: ${per_day:.0f}/session × 10 contracts = ${scale_10:.0f}/day @ scale")

    out_path = ROOT / "data" / "backtest_results" / "gospel_phase4_validation.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    def _strip(d):
        return {**{k: v for k, v in d.items() if k != "trades"}, "trade_count": len(d["trades"])}
    with open(out_path, "w") as f:
        json.dump({
            "ungated": _strip(ungated),
            "phase1":  _strip(phase1),
            "phase3":  _strip(phase3),
            "phase4":  _strip(phase4),
        }, f, indent=2, default=str)
    print(f"\nResults saved to {out_path.relative_to(ROOT.parent)}")


if __name__ == "__main__":
    main()
