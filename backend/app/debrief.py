"""Session debrief — auto post-mortem of a trading session.

Classifies each closed directional-spread trade (win / breach / stop /
time-stop-underwater), detects directional skew (e.g. "4/4 sold calls into a
rally"), characterises the volatility regime, and renders an honest verdict that
separates "the strategy working as designed" from "something to investigate".

Used by /api/debrief (dashboard panel) and the Telegram EOD summary. Both share
ONE engine so the phone and the app never disagree.

Anchors to the validated backtest so drawdown is judged in context, not in a
vacuum: 30Δ/TP90/no-ladder/BS = +$5,479 over 153 trades / 5 yrs, max DD −$1,581.
"""
from __future__ import annotations

from .quant_utils import BetaBinomialWinRate, expected_shortfall

# Validated-backtest anchors (honest BS re-validation, see .env / config).
BACKTEST_MAX_DD = 1581.0
BACKTEST_TRADES = 153
BACKTEST_TOTAL = 5479.0
# Share of the validated edge that comes from PUT-selling (147 puts +$5,357 vs
# 6 calls +$121 over 3yr). The live book must be judged per-side against this.
BACKTEST_PUT_SHARE = 96
# Cost-sensitivity of the validated edge (round-trip $/spread, SPX) — the edge
# sits inside the cost error bar, so report the curve, not one number.
COST_CURVE = {25: 5479, 35: 3949, 45: 0, 60: 124}
# Realized 5m-return stdev below this = a calm "grind" tape (worst case for
# selling premium against a trend); above = genuinely moving.
LOW_VOL_STD = 0.0008


def _money(v) -> str:
    v = v or 0.0
    return (f"+${v:.0f}" if v >= 0 else f"−${abs(v):.0f}")


def _classify(t) -> dict:
    is_call = t.side == "sell_call_cs"
    side = "CALL" if is_call else "PUT"
    sig = t.underlying_at_signal
    clo = t.underlying_at_close
    moved = (clo - sig) if (sig is not None and clo is not None) else 0.0
    # "against" = the tape moved toward/through the short leg (call up, put down)
    against = (is_call and moved > 0) or ((not is_call) and moved < 0)
    pnl = t.pnl or 0.0
    outcome = t.outcome or ""
    breach = False
    if clo is not None and t.short_strike is not None:
        breach = (is_call and clo >= t.short_strike) or ((not is_call) and clo <= t.short_strike)

    if pnl > 0:
        cat, icon = "win", "✅"
        note = f"+${pnl:.0f} ({t.exit_reason or outcome})"
    elif breach:
        cat, icon = "breach", "🛑"
        word = "call" if is_call else "put"
        note = (f"price closed THROUGH your short {word} {t.short_strike:.0f} "
                f"(underlying {sig:.0f}→{clo:.0f})")
    elif "stop" in outcome or "ladder" in outcome:
        cat, icon = "stop", "🪜"
        note = f"stopped out — {t.exit_reason or outcome}"
    else:
        cat, icon = "time_underwater", "⏰"
        note = f"time-stopped underwater — {t.exit_reason or outcome}"
    return {
        "trade_no": t.trade_no, "side": side, "cat": cat, "icon": icon,
        "against": against, "pnl": round(pnl, 2), "note": note,
        "short_strike": t.short_strike,
        "underlying_signal": sig, "underlying_close": clo,
        "realized_std": t.bs_realized_std, "regime": t.gex_regime,
    }


def _book_split(ds) -> dict:
    """Per-side (put vs call) CUMULATIVE live economics — the single most important
    cut. The validated edge is ~96% puts; if the live book skews to calls, the
    headline +$5,479 does not support it."""
    split = {}
    for key, label in (("sell_put_cs", "put"), ("sell_call_cs", "call")):
        ts = [t for t in ds if t.side == key]
        pnls = [t.pnl or 0.0 for t in ts]
        w = sum(1 for p in pnls if p > 0)
        split[label] = {
            "n": len(ts), "wins": w,
            "win_rate": round(w / len(ts) * 100, 1) if ts else None,
            "total_pnl": round(sum(pnls), 2),
            "mean_pnl": round(sum(pnls) / len(ts), 2) if ts else None,
        }
    nc, npu = split["call"]["n"], split["put"]["n"]
    note = None
    if nc + npu > 0:
        call_share = round(nc / (nc + npu) * 100)
        if call_share >= 50 and nc >= 2:
            note = (f"⚠️ {call_share}% of live trades are CALLS, but the validated edge is "
                    f"~{BACKTEST_PUT_SHARE}% PUTS — you are trading the unproven side.")
        elif npu > nc:
            note = f"put-led book ({npu}P/{nc}C) — aligned with the validated put edge."
    split["note"] = note
    return split


def _broker_check(ds) -> dict:
    """Model P&L vs REAL Alpaca-fill P&L — the validation's source of truth once
    fills are captured. Empty until live orders fill (READ_BROKER_FILLS)."""
    have = [t for t in ds if getattr(t, "broker_realized_pnl", None) is not None]
    if not have:
        return {"n": 0, "note": "no real fills captured yet — P&L is model-only"}
    model = round(sum((t.pnl or 0.0) for t in have), 2)
    real = round(sum(t.broker_realized_pnl for t in have), 2)
    gap = round(real - model, 2)
    return {"n": len(have), "model_pnl": model, "broker_pnl": real, "slippage": gap,
            "note": (f"real fills run {_money(gap)} vs model across {len(have)} trade(s)"
                     if abs(gap) >= 1 else "real ≈ model")}


def _confidence_from_pnls(pnls, unit: str = "trades") -> dict:
    """Posterior win-rate (Beta-Binomial, with a 95% LOWER bound) and realized tail
    (Expected Shortfall) from a list of per-`unit` P&L numbers.

    Two audit lessons, made explicit: (1) a small-sample win streak is NOT an edge —
    the lower bound shows how little a hot run actually proves, so the validation gate
    reads the floor, not the headline rate; (2) average P&L hides the negative-skew
    tail — ES (mean of the worst 5%, in $) is the honest read for a premium-selling
    book. Pure measurement: never gates or sizes a trade."""
    pnls = [p for p in pnls if p is not None]
    n = len(pnls)
    if n == 0:
        return {"n": 0, "note": f"no closed {unit} yet"}
    wr = BetaBinomialWinRate()
    for p in pnls:
        wr.update(p > 0)
    naive = sum(1 for p in pnls if p > 0) / n
    block = {
        "n": n,
        "unit": unit,
        "naive_win_rate_pct": round(naive * 100, 1),
        "win_rate_posterior_pct": round(wr.mean * 100, 1),
        "win_rate_lower95_pct": round(wr.lower_bound() * 100, 1),
        "expected_shortfall": None,
        "value_at_risk": None,
    }
    if n >= 20:
        es, var = expected_shortfall(pnls, confidence=0.95)
        block["expected_shortfall"] = round(es, 2)
        block["value_at_risk"] = round(var, 2)
        block["note"] = (
            f"posterior win-rate {block['win_rate_posterior_pct']:.0f}% "
            f"(95% floor {block['win_rate_lower95_pct']:.0f}%); "
            f"worst-5% {unit} average {_money(es)}"
        )
    else:
        block["note"] = (
            f"{n} closed {unit} — posterior win-rate floor "
            f"{block['win_rate_lower95_pct']:.0f}% vs naive "
            f"{block['naive_win_rate_pct']:.0f}% (that gap IS the small-sample "
            f"uncertainty); need ≥20 for an Expected-Shortfall tail read"
        )
    return block


def _confidence_block(ds) -> dict:
    """WAVE book confidence — over the same closed-trade set as cum_pnl / book_split.
    Pure measurement; never gates or sizes a trade."""
    return _confidence_from_pnls([t.pnl for t in ds if t.pnl is not None], unit="trades")


def load_ic_night_nets(log_path: str, exclude_date: str | None = None) -> list[float]:
    """Per-night real net P&L for EXECUTED condor nights, from the rolling
    debrief_log.jsonl the improvement loop reads — the cross-night series the
    single-session MEIC debrief is otherwise blind to. De-dup by date (last wins,
    matching improve_loop); `exclude_date` drops tonight so the caller can append
    its fresh net without double-counting on an EOD re-run."""
    import json
    nets: dict[str, float] = {}
    try:
        with open(log_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                d = r.get("date", "")
                if exclude_date is not None and d == exclude_date:
                    continue
                if (r.get("ic_executed") or 0) > 0 and r.get("ic_real_net") is not None:
                    nets[d] = float(r["ic_real_net"])
    except OSError:
        return []
    return [nets[d] for d in sorted(nets)]


def build_debrief(trades, date: str | None = None) -> dict:
    """trades: iterable of PaperTrade. Returns a structured debrief for one
    session (the latest closed-trade date by default)."""
    ds = [t for t in trades
          if getattr(t, "strategy", None) == "directional_spread"
          and t.closed and t.pnl is not None]
    days = sorted({(t.fired_at or "")[:10] for t in ds if t.fired_at})
    cum_all = round(sum(t.pnl or 0 for t in ds), 2)
    dd_pct = round(abs(min(0.0, cum_all)) / BACKTEST_MAX_DD * 100, 0)

    if not days:
        return {
            "date": None, "session_pnl": 0, "wins": 0, "losses": 0, "trades": [],
            "flags": {}, "verdict": "No closed trades yet — nothing to debrief.",
            "discipline": f"Validated on {BACKTEST_TRADES} trades. You have 0.",
            "cum_pnl": cum_all, "dd_vs_backtest_pct": dd_pct,
            "book_split": _book_split(ds),
            "broker_realized": _broker_check(ds),
            "confidence": _confidence_block(ds),
        }

    date = date or days[-1]
    day = [t for t in ds if (t.fired_at or "")[:10] == date]
    if not day:
        # Historical trades exist, but NONE closed on the requested date. Do not
        # fall through to the win/loss logic — with zero trades `losses` is empty
        # and the verdict would read "Clean session — every trade closed green",
        # which is nonsense for a day that never traded. Report it honestly.
        return {
            "date": date, "session_pnl": 0, "wins": 0, "losses": 0, "trades": [],
            "flags": {},
            "verdict": "No directional trades closed today — nothing to debrief.",
            "discipline": (f"Edge validated on {BACKTEST_TRADES} trades (+${BACKTEST_TOTAL:.0f}, "
                           f"positive 5/5 yrs). You have {len(ds)} closed — far too few to judge it."),
            "cum_pnl": cum_all, "dd_vs_backtest_pct": dd_pct,
            "available_dates": days,
            "book_split": _book_split(ds),
            "broker_realized": _broker_check(ds),
            "confidence": _confidence_block(ds),
            "no_trades_today": True,
        }
    analyses = [_classify(t) for t in day]
    wins = [a for a in analyses if a["cat"] == "win"]
    losses = [a for a in analyses if a["cat"] != "win"]
    session_pnl = round(sum(a["pnl"] for a in analyses), 2)

    sides = {a["side"] for a in analyses}
    skew = None
    if len(analyses) >= 2 and len(sides) == 1:
        s = next(iter(sides))
        skew = (f"all {len(analyses)} trades {s} — "
                f"fading {'upside' if s == 'CALL' else 'downside'}")
    trend_fades = sum(1 for a in losses if a["against"])
    stds = [a["realized_std"] for a in analyses if a["realized_std"]]
    avg_std = (sum(stds) / len(stds)) if stds else None
    vol_ctx = None
    if avg_std is not None:
        vol_ctx = "low (calm grind)" if avg_std < LOW_VOL_STD else "elevated"

    # ── Verdict: separate "by design" from "investigate" ────────────────────
    if not losses:
        verdict = "Clean session — every trade closed green."
    else:
        bits = []
        # Most losses are the strategy fading the tape? (independent of skew so a
        # single trend-fade trade still gets explained.)
        if trend_fades and trend_fades >= (len(losses) + 1) // 2:
            loss_calls = sum(1 for a in losses if a["side"] == "CALL")
            dir_word = "rally" if loss_calls >= len(losses) - loss_calls else "selloff"
            bits.append(f"the losing trade(s) are the strategy fading a {dir_word}"
                        + (f" on a {vol_ctx} tape" if vol_ctx else "")
                        + " — mean-reversion behaving as designed, not a malfunction")
        if cum_all < -BACKTEST_MAX_DD:
            bits.append(f"⚠️ cumulative drawdown (${cum_all:.0f}) has EXCEEDED the "
                        f"backtested max (−${BACKTEST_MAX_DD:.0f}) — worth a real review")
        else:
            bits.append(f"drawdown is ${cum_all:.0f} = {dd_pct:.0f}% of the backtested "
                        f"max (−${BACKTEST_MAX_DD:.0f}) — inside the validated envelope")
        verdict = "; ".join(bits) + "."

    discipline = (f"Edge validated on {BACKTEST_TRADES} trades (+${BACKTEST_TOTAL:.0f}, "
                  f"positive 5/5 yrs). You have {len(ds)} closed — far too few to judge it.")

    return {
        "date": date,
        "session_pnl": session_pnl,
        "wins": len(wins), "losses": len(losses),
        "trades": analyses,
        "flags": {
            "directional_skew": skew,
            "trend_fade_losses": trend_fades,
            "vol_context": vol_ctx,
            "avg_realized_std": round(avg_std, 6) if avg_std is not None else None,
        },
        "cum_pnl": cum_all,
        "dd_vs_backtest_pct": dd_pct,
        "verdict": verdict,
        "discipline": discipline,
        "available_dates": days,
        # Cumulative put-book vs call-book economics (quant-audit: the headline cut).
        "book_split": _book_split(ds),
        "broker_realized": _broker_check(ds),
        # Posterior win-rate floor + realized tail (Expected Shortfall) — reads the
        # lower bound and the tail, not the headline rate. Pure measurement.
        "confidence": _confidence_block(ds),
    }


# ── MEIC / iron-condor book ──────────────────────────────────────────────────
# Anchors from the honest MEIC backtest (scripts/meic_backtest.py, ladder
# 11/12/13/14 ET): per-entry stop-rate 58%, 73% green NIGHTS, +$125/entry
# expectancy at $50 SPX-scale round-trip cost. Edge thins as real cost rises.
IC_BT_STOP_RATE = 58.0
IC_BT_GREEN_NIGHTS = 73.0
IC_BT_COST_RT_SPX = 50.0
IC_SLIP_TOLERANCE_PCT = 15.0   # entry slippage above this = the audit's edge-thinning line


def _limit_shadow_summary(rungs) -> dict | None:
    """Per-night CBOE-mid marketable-limit SHADOW report — pure measurement.

    For each condor where the limit shadow was computed AND the real fill landed,
    classify would_fill vs would_not_fill and average the per-share improvement
    (real − limit). Positive improvement = market got us BETTER than the limit
    (so the limit ladder would still have captured at least the shadow price).
    Negative = the limit wouldn't have triggered (or would have improved further
    while sitting). We are NOT yet calling the limit ladder — this is the metric
    that decides when we flip IC_LIMIT_LIVE_ENABLED on."""
    measured = []
    for b in rungs or ():
        lim = getattr(b, "limit_shadow_credit_per_share_spy", None)
        real = getattr(b, "limit_shadow_market_credit_per_share_spy", None)
        if lim is None or real is None:
            continue
        qty = getattr(b, "contracts", None) or 1
        measured.append({
            "build_id": getattr(b, "build_id", ""),
            "limit_per_share": float(lim),
            "real_per_share": float(real),
            "improve_per_share": round(float(real) - float(lim), 4),
            "improve_total": round((float(real) - float(lim)) * qty * 100.0, 2),
            "decision": getattr(b, "limit_shadow_decision", None) or "unknown",
        })
    if not measured:
        return None
    would_fill = sum(1 for m in measured if m["decision"] == "would_fill")
    would_not = sum(1 for m in measured if m["decision"] == "would_not_fill")
    mean_per_share = sum(m["improve_per_share"] for m in measured) / len(measured)
    total_improve = sum(m["improve_total"] for m in measured)
    return {
        "n": len(measured),
        "would_fill": would_fill,
        "would_not_fill": would_not,
        "would_fill_rate_pct": round(would_fill / len(measured) * 100, 1),
        "mean_improve_per_share": round(mean_per_share, 4),
        "total_improve_spy": round(total_improve, 2),
        "rows": measured,
    }


def _ic_status(b) -> str:
    s = getattr(b, "broker_status", None)
    if s == "submitted":
        return "executed"
    if s == "closed_stop":
        return "stopped"
    if s in ("error", "rejected"):
        return "error"
    return "alert_only" if getattr(b, "available", False) else "skipped"


def build_ic_debrief(ic_history, date: str, real_book: dict | None = None,
                     night_nets: list[float] | None = None) -> dict:
    """MEIC condor-book post-mortem for ONE session — the piece the wave debrief
    was blind to. real_book (optional) = {entries, entry_credit, exit_cost, net}
    in SPY-scale real $ from Alpaca fills; model-only when absent.

    Measures execution reality vs the backtest's ASSUMPTIONS (slippage vs the
    $50 cost model, stop-rate vs 58%) — these feed the weekly improvement loop.
    It never judges strategy off one night; small N is reported, not alarmed."""
    rungs = [b for b in (ic_history or [])
             if (getattr(b, "build_id", "") or "").startswith(f"ic_{date}")]
    if not rungs:
        return {"date": None, "verdict": "No condors built this session.", "n": 0}

    executed = stopped = 0
    model_credit_spx = 0.0
    rows = []
    for b in rungs:
        st = _ic_status(b)
        if st in ("executed", "stopped"):
            executed += 1
            model_credit_spx += (b.total_credit_dollars or 0.0)
        if st == "stopped":
            stopped += 1
        cl, pl = b.call_leg, b.put_leg
        bid = b.build_id or ""
        rows.append({
            "slot": (bid[-4:][:2] + ":" + bid[-2:]) if len(bid) >= 4 else "—",
            "status": st,
            "call_short": getattr(cl, "short_strike", None) if cl else None,
            "put_short": getattr(pl, "short_strike", None) if pl else None,
            "model_credit": round(b.total_credit_dollars or 0.0, 2),
        })
    stop_rate = round(stopped / executed * 100, 1) if executed else None

    out = {"date": date, "n": len(rungs), "rungs": rows,
           "executed": executed, "stopped": stopped, "stop_rate": stop_rate,
           "model_credit_spx": round(model_credit_spx, 2)}
    flags: list[str] = []

    # CBOE-mid marketable-limit SHADOW — measurement only, never tunes anything.
    shadow = _limit_shadow_summary(rungs)
    out["limit_shadow"] = shadow
    if shadow:
        wf, wn = shadow["would_fill"], shadow["would_not_fill"]
        mean_i = shadow["mean_improve_per_share"]
        total_i = shadow["total_improve_spy"]
        if wf and wn == 0:
            flags.append(f"✅ limit-shadow: {wf}/{wf} would_fill at CBOE-mid limit · "
                         f"mean improve {mean_i:+.3f} $/share (real over limit) · "
                         f"total {total_i:+.2f} $ — limit ladder would capture this")
        elif wf and wn:
            flags.append(f"limit-shadow: {wf} would_fill, {wn} would_not_fill (rate "
                         f"{shadow['would_fill_rate_pct']:.0f}%) · ladder will need its "
                         f"reprice rungs to catch the non-fills")
        elif wn and not wf:
            flags.append(f"⚠️ limit-shadow: 0/{wn} would_fill — CBOE mid is below real fill "
                         f"by mean {abs(mean_i):.3f} $/share. Live ladder must reprice or "
                         f"market-fallback to clear; do NOT flip IC_LIMIT_LIVE_ENABLED yet")

    # Execution quality — the number the audit said decides everything.
    if real_book and real_book.get("entries"):
        real_entry = real_book.get("entry_credit") or 0.0       # SPY-scale real $
        real_net = real_book.get("net") or 0.0
        model_entry_spy = model_credit_spx / 10.0               # SPX→SPY ×1ct
        slip = model_entry_spy - real_entry
        slip_pct = round(slip / model_entry_spy * 100, 1) if model_entry_spy else None
        out["real"] = {"entry_credit": round(real_entry, 2), "net_pnl": round(real_net, 2),
                       "entries": real_book.get("entries"),
                       "model_entry_spy": round(model_entry_spy, 2), "slippage_pct": slip_pct}
        if slip_pct is not None and slip_pct > IC_SLIP_TOLERANCE_PCT:
            flags.append(f"⚠️ entry slippage {slip_pct:.0f}% (model ${model_entry_spy:.0f}→real "
                         f"${real_entry:.0f}) — above the ~{IC_SLIP_TOLERANCE_PCT:.0f}% line where IC "
                         f"edge thins; CBOE-mid limit orders are the fix, not a param change")
        elif slip_pct is not None and slip_pct < -5:
            flags.append(f"✅ fills RICHER than model by {abs(slip_pct):.0f}% (model ${model_entry_spy:.0f}"
                         f"→real ${real_entry:.0f}) — the BS credit model is conservative; the backtest "
                         f"understates this book's edge")
        elif slip_pct is not None:
            flags.append(f"entry slippage {slip_pct:.0f}% — within tolerance")
    else:
        out["real"] = {"note": "no real fills captured — model-only"}

    if stop_rate is not None:
        if executed >= 8 and stop_rate > IC_BT_STOP_RATE + 20:
            flags.append(f"⚠️ stop-rate {stop_rate:.0f}% vs backtest {IC_BT_STOP_RATE:.0f}% — running "
                         f"hot; likely a trend day or entry-timing issue, not the strategy")
        else:
            flags.append(f"stop-rate {stop_rate:.0f}% (backtest {IC_BT_STOP_RATE:.0f}%; N={executed} "
                         f"— too few to judge)")

    net = (out.get("real") or {}).get("net_pnl")
    if net is not None:
        green = net > 0
        out["verdict"] = (
            f"{'GREEN' if green else 'RED'} condor night — {executed} fired, {stopped} stopped, "
            f"real {_money(net)}. " + (
                f"In line with the {IC_BT_GREEN_NIGHTS:.0f}% green-night model."
                if green else
                f"A red night is expected ~{100-IC_BT_GREEN_NIGHTS:.0f}% of the time BY DESIGN — "
                f"the stops capped it at {_money(net)} instead of the ~{_money(-(model_credit_spx/10*4))} "
                f"max. Not a malfunction unless it repeats with low slippage."))
    else:
        out["verdict"] = f"{executed} condors fired, {stopped} stopped (model-only — no fills captured)."

    # Cross-night posterior green-night rate + tail (ES over per-night net P&L):
    # prior nights from the rolling log + tonight's net. Pure measurement.
    _series = list(night_nets or [])
    if net is not None:
        _series.append(net)
    out["confidence"] = _confidence_from_pnls(_series, unit="nights")

    out["flags"] = flags
    return out


def format_ic_debrief_telegram(d: dict) -> str:
    if not d.get("date"):
        return ""
    lines = [f"🦅 MEIC DEBRIEF · {d['date']}"]
    r = d.get("real") or {}
    if r.get("net_pnl") is not None:
        lines.append(f"book: {d['executed']} fired · {d['stopped']} stopped · "
                     f"real {_money(r['net_pnl'])} (entry {_money(r.get('entry_credit'))})")
    else:
        lines.append(f"book: {d['executed']} fired · {d['stopped']} stopped · model-only")
    for rg in d.get("rungs", []):
        cs, ps = rg.get("call_short"), rg.get("put_short")
        legs = f"C{cs:.0f}/P{ps:.0f}" if cs and ps else "(no build)"
        lines.append(f"  {rg['slot']} {rg['status']:>9} {legs} · model {_money(rg['model_credit'])}")
    sh = d.get("limit_shadow")
    if sh:
        lines.append(f"limit-shadow: {sh['would_fill']}/{sh['n']} would_fill · "
                     f"mean improve {sh['mean_improve_per_share']:+.3f} $/share · "
                     f"total {_money(sh['total_improve_spy'])}")
    for f in d.get("flags", []):
        lines.append(f)
    cf = d.get("confidence") or {}
    if cf.get("n"):
        es = cf.get("expected_shortfall")
        tail = f" · worst-5% {_money(es)}" if es is not None else ""
        lines.append(
            f"edge: green-night {cf['win_rate_posterior_pct']:.0f}% "
            f"(95% floor {cf['win_rate_lower95_pct']:.0f}%, N={cf['n']}){tail}"
        )
    lines.append(f"verdict: {d['verdict']}")
    return "\n".join(lines)


def log_assumptions(date: str, ic_d: dict, wave_d: dict, path: str) -> dict:
    """Append one row of MEASURED reality to the rolling log the weekly
    improvement loop reads. The sample accumulates HERE; the loop never retunes
    off a single night — it waits for N and re-confirms against the backtest."""
    import json
    r = ic_d.get("real") or {}
    sh = ic_d.get("limit_shadow") or {}
    row = {
        "date": date,
        "ic_executed": ic_d.get("executed", 0),
        "ic_stopped": ic_d.get("stopped", 0),
        "ic_stop_rate": ic_d.get("stop_rate"),
        "ic_real_net": r.get("net_pnl"),
        "ic_entry_credit": r.get("entry_credit"),
        "ic_slippage_pct": r.get("slippage_pct"),
        # CBOE-mid limit-shadow — feeds the live-flip decision. None until enabled.
        "ic_limit_shadow_n": sh.get("n"),
        "ic_limit_shadow_would_fill_rate_pct": sh.get("would_fill_rate_pct"),
        "ic_limit_shadow_mean_improve_per_share": sh.get("mean_improve_per_share"),
        "ic_limit_shadow_total_improve_spy": sh.get("total_improve_spy"),
        "wave_pnl": wave_d.get("session_pnl"),
        "wave_n": len(wave_d.get("trades", [])),
        "wave_wins": wave_d.get("wins"),
    }
    try:
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row


def format_debrief_telegram(d: dict) -> str:
    """Compact Telegram rendering of a debrief dict."""
    if not d.get("date"):
        return "🔍 DEBRIEF — no closed trades to review."
    lines = [f"🔍 DEBRIEF · {d['date']}"]
    sp = d["session_pnl"]
    sp_str = f"+${sp:.0f}" if sp >= 0 else f"−${abs(sp):.0f}"
    lines.append(f"session: {len(d['trades'])} trade(s) · {d['wins']}W/{d['losses']}L · {sp_str}")
    for a in d["trades"]:
        p = a["pnl"]
        p_str = f"+${p:.0f}" if p >= 0 else f"−${abs(p):.0f}"
        lines.append(f"{a['icon']} #{a['trade_no']} {a['side']} {p_str} — {a['note']}")
    fl = d.get("flags", {})
    if fl.get("directional_skew"):
        lines.append(f"⚠️ {fl['directional_skew']}")
    if fl.get("vol_context"):
        lines.append(f"vol: {fl['vol_context']}")
    bs = d.get("book_split") or {}
    pu, ca = bs.get("put") or {}, bs.get("call") or {}
    if (pu.get("n") or 0) + (ca.get("n") or 0) > 0:
        lines.append(f"book: PUT {pu.get('n',0)} ({_money(pu.get('total_pnl'))}) · "
                     f"CALL {ca.get('n',0)} ({_money(ca.get('total_pnl'))})")
        if bs.get("note"):
            lines.append(bs["note"])
    cf = d.get("confidence") or {}
    if cf.get("n"):
        es = cf.get("expected_shortfall")
        tail = f" · worst-5% {_money(es)}" if es is not None else ""
        lines.append(
            f"edge: post WR {cf['win_rate_posterior_pct']:.0f}% "
            f"(95% floor {cf['win_rate_lower95_pct']:.0f}%, N={cf['n']}){tail}"
        )
    lines.append(f"verdict: {d['verdict']}")
    lines.append(d["discipline"])
    return "\n".join(lines)
