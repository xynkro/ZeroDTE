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

# Validated-backtest anchors (honest BS re-validation, see .env / config).
BACKTEST_MAX_DD = 1581.0
BACKTEST_TRADES = 153
BACKTEST_TOTAL = 5479.0
# Realized 5m-return stdev below this = a calm "grind" tape (worst case for
# selling premium against a trend); above = genuinely moving.
LOW_VOL_STD = 0.0008


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
        }

    date = date or days[-1]
    day = [t for t in ds if (t.fired_at or "")[:10] == date]
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
    }


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
    lines.append(f"verdict: {d['verdict']}")
    lines.append(d["discipline"])
    return "\n".join(lines)
