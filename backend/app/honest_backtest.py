"""Honest directional-spread backtest — Black-Scholes repricing engine.

Replaces the underlying-move power-law proxy in directional_spread_backtest.py,
which inflated win rate by booking a 10%-credit "win" on a 0.008% favorable tick
(median 1 bar) and therefore reported ~0 breaches because trades closed before any
breach could register.

What's different here (all aimed at realism):
  1. Strikes placed by TRUE Black-Scholes delta (strike_for_*_delta), not a %OTM
     lookup. "20Δ" actually means 20 delta at that session's vol.
  2. Per-session implied vol derived from realized 5m vol of PRE-ENTRY bars
     (lookahead-safe) × a premium multiplier. No fantasy fixed credit.
  3. Entry credit DERIVED from BS spread value — internally consistent with the
     strikes and vol.
  4. Spread repriced every bar with shrinking time-to-expiry → real theta/gamma.
     A win now requires real decay or a real favorable move.
  5. Intra-bar WICK breach detection (not just bar-close), capped at max loss.
  6. Real round-trip transaction cost per spread.

Signals, confluence, and VWAP gate are reused verbatim from the existing backtest
so results are apples-to-apples on entry selection.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from . import bs_pricing as bs
from .config import settings
from .predictor import Bar, run_backtest as predictor_run, _ema
from .directional_spread_backtest import _compute_confluence, _session_vwap

ET = ZoneInfo("America/New_York")


def _load_bars(data_window: str = "3y") -> list[Bar]:
    explicit = {"60d": "SPX_5m_60d.json", "1y": "SPX_5m_1y.json", "3y": "SPX_5m_3y.json"}
    candidates = [explicit[data_window]] if data_window in explicit else \
        ["SPX_5m_3y.json", "SPX_5m_1y.json", "SPX_5m_60d.json"]
    for c in candidates:
        p = settings.data_dir / "historical" / c
        if p.exists():
            raw = json.loads(p.read_text())
            return [Bar(time=datetime.fromisoformat(r["datetime"]), open=r["open"],
                        high=r["high"], low=r["low"], close=r["close"],
                        volume=r.get("volume", 0) or 0) for r in raw], c
    return [], None


def _periods_remaining(bar_time) -> float:
    et = bar_time.astimezone(ET) if bar_time.tzinfo else bar_time
    return max((16 * 60) - (et.hour * 60 + et.minute), 0) / 5.0


# Prepared-context cache — predictor over 85k bars is the bottleneck; compute once
# per data_window and reuse across the config sweep.
_CTX_CACHE: dict = {}


def _prepare(data_window: str):
    if data_window in _CTX_CACHE:
        return _CTX_CACHE[data_window]
    loaded = _load_bars(data_window)
    bars, data_file = loaded if loaded[0] else ([], None)
    if not bars:
        _CTX_CACHE[data_window] = None
        return None
    by_date: dict[str, list[Bar]] = defaultdict(list)
    for b in bars:
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        by_date[et.strftime("%Y-%m-%d")].append(b)
    sorted_dates = sorted(by_date.keys())
    daily_atr_map: dict[str, float] = {}
    trs: list[float] = []
    prev_close = None
    for d in sorted_dates:
        ses = by_date[d]
        hi = max(x.high for x in ses); lo = min(x.low for x in ses); cl = ses[-1].close
        tr = (hi - lo) if prev_close is None else max(hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        if len(trs) >= 14:
            daily_atr_map[d] = float(np.mean(trs[-14:]))
        prev_close = cl
    sessions = predictor_run(bars, lambda d: daily_atr_map.get(d))
    closes_all = np.array([b.close for b in bars])
    ema10_arr = _ema(closes_all, 10)
    idx_by_time = {b.time: i for i, b in enumerate(bars)}
    ctx = (bars, by_date, sessions, ema10_arr, idx_by_time, data_file)
    _CTX_CACHE[data_window] = ctx
    return ctx


def _event_dates(years) -> set:
    """Deterministic high-impact macro days: FOMC (published schedule) + NFP
    (1st Friday) + OPEX (3rd Friday). CPI is omitted (its date drifts and needs a
    real calendar) — so this UNDER-counts event days, making the overlay a
    conservative lower bound on how many trades live's blackout would remove."""
    import datetime as _dt
    out = set()
    FOMC = [
        "2022-01-26", "2022-03-16", "2022-05-04", "2022-06-15", "2022-07-27", "2022-09-21", "2022-11-02", "2022-12-14",
        "2023-02-01", "2023-03-22", "2023-05-03", "2023-06-14", "2023-07-26", "2023-09-20", "2023-11-01", "2023-12-13",
        "2024-01-31", "2024-03-20", "2024-05-01", "2024-06-12", "2024-07-31", "2024-09-18", "2024-11-07", "2024-12-18",
        "2025-01-29", "2025-03-19", "2025-05-07", "2025-06-18", "2025-07-30", "2025-09-17", "2025-11-05", "2025-12-17",
        "2026-01-28", "2026-03-18", "2026-05-06", "2026-06-17",
    ]
    out.update(d for d in FOMC if int(d[:4]) in years)
    for y in years:
        for m in range(1, 13):
            fri = [d for d in range(1, 29) if _dt.date(y, m, d).weekday() == 4]
            if fri:
                out.add(_dt.date(y, m, fri[0]).isoformat())          # NFP (1st Fri)
                if len(fri) >= 3:
                    out.add(_dt.date(y, m, fri[2]).isoformat())      # OPEX (3rd Fri)
    return out


def run_honest_backtest(
    target_delta: int = 20,
    wing_dollars: float = 10.0,
    premium_mult: float = 1.20,
    confluence_min: int = 3,
    require_vwap: bool = True,
    use_dynamic_stops: bool = True,
    ladder: tuple[tuple[float, float], ...] = ((90., 75.), (75., 50.), (50., 25.)),
    final_tp_target: float = 50.0,
    time_stop_min: int = 30,
    cost_per_spread_rt: float = 25.0,
    max_entry_tv: float | None = None,
    data_window: str = "3y",
    return_trades: bool = True,
    # ── Quant-audit pricing-honesty knobs (default = original flat behaviour) ──
    skew_enabled: bool = False,        # price puts richer / calls cheaper (real skew)
    skew_put_mult: float = 1.15,       # put IV ≈ 1.15× the flat realized-vol base
    skew_call_mult: float = 0.90,      # call IV ≈ 0.90× the base
    vol_ratchet: bool = False,         # lift repricing vol if intraday RV exceeds entry RV
    block_event_days: bool = False,    # skip FOMC/NFP/OPEX days (mirrors live blackout)
) -> dict:
    ctx = _prepare(data_window)
    if ctx is None:
        return {"error": "missing data"}
    bars, by_date, sessions, ema10_arr, idx_by_time, data_file = ctx
    event_set = _event_dates(range(2021, 2027)) if block_event_days else set()

    trades = []
    n_signals = n_filt_conf = n_filt_vwap = n_filt_vol = 0
    mult = 100

    for s in sessions:
        if not s.signals:
            continue
        sb = by_date.get(s.session_date, [])
        if not sb:
            continue
        sb_sorted = sorted(sb, key=lambda x: x.time)
        if block_event_days and str(s.session_date) in event_set:
            continue

        for sig in s.signals:
            n_signals += 1
            idx = idx_by_time.get(sig.time)
            if idx is None:
                continue
            ema10 = float(ema10_arr[idx]) if not np.isnan(ema10_arr[idx]) else 0.0
            ssi = idx_by_time.get(sb_sorted[0].time, idx)
            vwap = _session_vwap(bars[ssi:idx + 1], idx - ssi)
            conf = _compute_confluence(sig.time, sig.side, sig.rsi, sig.wvf_spike,
                                       ema10, sig.entry_price, vwap)
            if conf.score < confluence_min:
                n_filt_conf += 1
                continue
            if require_vwap and not conf.vwap_aligned:
                n_filt_vwap += 1
                continue

            S0 = sig.entry_price
            side = sig.side

            # Per-session vol from pre-entry closes (lookahead-safe)
            pre_closes = [b.close for b in sb_sorted if b.time <= sig.time]
            r5 = bs.realized_5m_std(pre_closes)
            if r5 <= 0:
                continue
            pr0 = _periods_remaining(sig.time)
            if pr0 <= 0:
                continue
            tv0 = bs.total_vol_to_expiry(r5, pr0, premium_mult)
            if tv0 <= 0:
                continue
            if max_entry_tv is not None and tv0 > max_entry_tv:
                n_filt_vol += 1
                continue

            # Skew: tilt the vol per side (puts richer, calls cheaper). Applied to
            # BOTH strike placement and credit so the 30Δ strike + premium stay
            # internally consistent with the side's implied vol.
            sk = (skew_put_mult if side == "sell_put_cs" else skew_call_mult) if skew_enabled else 1.0
            tv0s = tv0 * sk

            # Strikes by true delta (on the side-skewed vol)
            if side == "sell_call_cs":
                short_K = bs.strike_for_call_delta(S0, tv0s, target_delta / 100.0)
                long_K = short_K + wing_dollars
            else:
                short_K = bs.strike_for_put_delta(S0, tv0s, target_delta / 100.0)
                long_K = short_K - wing_dollars

            credit_ps = bs.spread_value(side, S0, short_K, long_K, tv0s)
            if credit_ps <= 0.02:
                continue
            credit_usd = credit_ps * mult
            max_loss_pct = (credit_ps - wing_dollars) / credit_ps * 100.0  # e.g. -167%

            peak = 0.0
            stop_pct = -100.0
            outcome = "expire"
            exit_pct = None
            bars_held = 0
            post_closes = []

            for b in sb_sorted:
                if b.time <= sig.time:
                    continue
                et = b.time.astimezone(ET) if b.time.tzinfo else b.time
                bmin = et.hour * 60 + et.minute
                pr = _periods_remaining(b.time)

                if bmin >= 16 * 60:
                    v = bs.spread_value(side, b.close, short_K, long_K, 0.0)  # intrinsic
                    exit_pct = max(max_loss_pct, min(100.0, (credit_ps - v) / credit_ps * 100.0))
                    outcome = "expire"
                    break

                bars_held += 1
                post_closes.append(b.close)
                # Vol-floor ratchet: lift repricing vol if intraday RV exceeds the
                # entry RV (conservative-only — can never reduce a loss estimate).
                r5_eff = r5
                if vol_ratchet and len(post_closes) >= 2:
                    r5_eff = max(r5, bs.realized_5m_std([S0] + post_closes))
                tv = bs.total_vol_to_expiry(r5_eff, pr, premium_mult) * sk
                worst = b.high if side == "sell_call_cs" else b.low
                best = b.low if side == "sell_call_cs" else b.high

                v_worst = bs.spread_value(side, worst, short_K, long_K, tv)
                v_best = bs.spread_value(side, best, short_K, long_K, tv)
                pk_worst = max(max_loss_pct, min(100.0, (credit_ps - v_worst) / credit_ps * 100.0))
                pk_best = max(max_loss_pct, min(100.0, (credit_ps - v_best) / credit_ps * 100.0))

                if pk_best > peak:
                    peak = pk_best

                if use_dynamic_stops:
                    for trig, lock in ladder:
                        if peak >= trig:
                            stop_pct = max(stop_pct, lock)
                            break

                # STOP (worst-first ordering = pessimistic)
                if pk_worst <= stop_pct:
                    if stop_pct >= 0:
                        exit_pct = stop_pct            # locking a profit — limit fills
                    else:
                        exit_pct = pk_worst            # losing stop — fill at bar worst (gap-honest)
                    outcome = "stop_ladder" if stop_pct >= 0 else "stop_loss"
                    break

                # WICK breach through short strike → at/near max loss
                wick_breach = (side == "sell_call_cs" and worst >= short_K) or \
                              (side == "sell_put_cs" and worst <= short_K)
                if wick_breach and pk_worst <= -100.0:
                    exit_pct = max_loss_pct
                    outcome = "breach"
                    break

                # TP (limit at the target level)
                if pk_best >= final_tp_target:
                    exit_pct = final_tp_target
                    outcome = "tp"
                    break

                # TIME stop
                if bmin >= (16 * 60 - time_stop_min):
                    v = bs.spread_value(side, b.close, short_K, long_K, tv)
                    exit_pct = max(max_loss_pct, min(100.0, (credit_ps - v) / credit_ps * 100.0))
                    outcome = "time"
                    break

            if exit_pct is None:
                exit_pct = 100.0
                outcome = "expire"

            pnl = credit_usd * (exit_pct / 100.0) - cost_per_spread_rt
            trades.append({
                "date": str(s.session_date), "side": side,
                "entry_price": float(S0), "short_strike": float(short_K),
                "credit": round(credit_usd, 1), "tv0": round(tv0, 5),
                "peak": round(peak, 1), "exit_pct": round(exit_pct, 1),
                "bars_held": bars_held, "outcome": outcome,
                "pnl": round(pnl, 2),
            })

    # Summary
    n = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    tot = sum(t["pnl"] for t in trades)
    oc = defaultdict(int)
    for t in trades:
        oc[t["outcome"]] += 1
    cum = np.cumsum([t["pnl"] for t in trades]) if trades else np.array([0.0])
    dd = float((cum - np.maximum.accumulate(cum)).min()) if n else 0.0
    by_year = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
    for t in trades:
        y = t["date"][:4]
        by_year[y]["n"] += 1
        by_year[y]["pnl"] += t["pnl"]
        if t["pnl"] > 0:
            by_year[y]["w"] += 1

    return {
        "params": {
            "target_delta": target_delta, "wing_dollars": wing_dollars,
            "premium_mult": premium_mult, "final_tp_target": final_tp_target,
            "use_dynamic_stops": use_dynamic_stops, "cost_per_spread_rt": cost_per_spread_rt,
            "max_entry_tv": max_entry_tv, "data_file": data_file,
        },
        "summary": {
            "n_trades": n, "win_rate_pct": round(100 * len(wins) / n, 1) if n else 0,
            "total_pnl": round(tot, 0), "avg_pnl": round(tot / n, 1) if n else 0,
            "avg_win": round(float(np.mean([t["pnl"] for t in wins])), 1) if wins else 0,
            "avg_loss": round(float(np.mean([t["pnl"] for t in losses])), 1) if losses else 0,
            "avg_credit": round(float(np.mean([t["credit"] for t in trades])), 0) if n else 0,
            "max_drawdown": round(dd, 0),
            "n_breach": oc["breach"], "n_stop_loss": oc["stop_loss"],
            "n_stop_ladder": oc["stop_ladder"], "n_tp": oc["tp"],
            "n_time": oc["time"], "n_expire": oc["expire"],
            "n_filt_vol": n_filt_vol,
        },
        "yearly": [{"year": y, "n": by_year[y]["n"], "pnl": round(by_year[y]["pnl"], 0),
                    "wr": round(100 * by_year[y]["w"] / by_year[y]["n"], 1) if by_year[y]["n"] else 0}
                   for y in sorted(by_year)],
        "trades": trades if return_trades else [],
    }
