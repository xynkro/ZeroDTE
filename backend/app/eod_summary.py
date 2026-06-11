"""End-of-day summary builder.

Fires once per session shortly after 16:00 ET / 04:00 SGT to:
  - Resolve every Wave signal that fired today (walk-forward through
    today's bars to determine breach / managed / expire-OTM)
  - Score the day's Iron Condor against the actual session H/L/Close
  - Post a per-topic summary to Telegram (Wave topic, IC topic)

Uses the predictor's bar buffer for the walk-forward (same data the live
signal logic sees). No IBKR option-chain replay — outcomes are computed
on UNDERLYING price action, which is what the EU-style cash-settled
SPX/XSP options reference for settlement anyway.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from .models import SignalEvent, IronCondorBuilder
from .predictor import Bar


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


# Outcome thresholds
ATM_PCT_BAND = 0.0015      # |close - strike| / strike < 0.15% = "graze" zone
WAVE_FAVORABLE_MOVE_PCT = 0.3  # matches wave_backtest default (managed-close trigger)
DEFAULT_INSTRUMENT = "XSP"


# ─────────────────────────────────────────────────────────────────────
# Wave signal outcome (walk-forward)
# ─────────────────────────────────────────────────────────────────────

def resolve_wave_signal(
    sig: SignalEvent,
    today_bars: list[Bar],
    strike_distance_pct: float = 1.0,        # default = canonical 1% OTM
    favorable_move_pct: float = WAVE_FAVORABLE_MOVE_PCT,
    credit_pct_per_side: float = 12.0,       # used only for $ P&L estimate
    profit_target_pct: float = 25.0,
) -> dict:
    """Resolve a single wave signal. Returns dict with:
        outcome ∈ {breach_max_loss, managed_profit, expire_otm}
        pnl_dollars (signed, est)
        exit_time_iso
    """
    side = sig.side
    entry_price = sig.underlying_price
    sig_dt = datetime.fromisoformat(sig.triggered_at)

    # Strike at signal-fire time (no lookahead)
    if side == "sell_call_cs":
        short_strike = entry_price * (1 + strike_distance_pct / 100.0)
    else:
        short_strike = entry_price * (1 - strike_distance_pct / 100.0)

    # XSP wing in points (= 5pt × $100 = $500 max risk before credit)
    wing_pts = 5.0
    multiplier = 100
    credit_per_side = (credit_pct_per_side / 100.0) * wing_pts * multiplier
    total_credit = credit_per_side * 2
    max_loss = wing_pts * multiplier - total_credit
    profit_target = total_credit * (profit_target_pct / 100.0)

    outcome = "expire_otm"
    pnl = total_credit
    exit_time: Optional[datetime] = None

    for b in today_bars:
        if b.time <= sig_dt:
            continue
        et = b.time.astimezone(ET) if b.time.tzinfo else b.time
        if et.hour >= 16:
            break

        # Breach
        if side == "sell_call_cs" and b.high >= short_strike:
            outcome = "breach_max_loss"
            pnl = -max_loss
            exit_time = b.time
            break
        if side == "sell_put_cs" and b.low <= short_strike:
            outcome = "breach_max_loss"
            pnl = -max_loss
            exit_time = b.time
            break

        # Managed profit
        if side == "sell_call_cs":
            if b.low <= entry_price * (1 - favorable_move_pct / 100.0):
                outcome = "managed_profit"
                pnl = profit_target
                exit_time = b.time
                break
        else:
            if b.high >= entry_price * (1 + favorable_move_pct / 100.0):
                outcome = "managed_profit"
                pnl = profit_target
                exit_time = b.time
                break

    return {
        "outcome": outcome,
        "pnl_dollars": float(pnl),
        "short_strike": float(short_strike),
        "entry_price": float(entry_price),
        "side": side,
        "fired_at": sig.triggered_at,
        "exit_time": exit_time.isoformat() if exit_time else None,
    }


def is_win(outcome: str) -> bool:
    return outcome in ("expire_otm", "managed_profit")


# ─────────────────────────────────────────────────────────────────────
# Iron Condor outcome — score against actual session H/L/Close
# ─────────────────────────────────────────────────────────────────────

def resolve_iron_condor(
    ic: IronCondorBuilder,
    session_high: float,
    session_low: float,
    session_close: float,
    post_build_high: float | None = None,
    post_build_low: float | None = None,
) -> dict:
    """Score the day's IC. Categories:
      GOOD     — wings never tagged AND close inside the box → max profit
      ATM      — wing was tagged intraday but close came back inside
                 (EU-cash settled = still max profit, but uncomfortable)
      GRAZE    — close within ATM_PCT_BAND of a short strike
                 (settlement might still pay full credit, marginal)
      BAAAAD   — close beyond a short strike → max loss on that wing

    SCORING FIX (2026-05-09): use POST-BUILD H/L for tag detection, not full
    session H/L. The IC didn't exist before its built_at timestamp — H/L
    excursions before build are noise that have nothing to do with the actual
    trade lifetime. The CLOSE comparison still uses session_close (settlement).

    Caller passes both for legacy compatibility:
      session_high/low  — used for the "tagged_call/put" intraday flags only
                          if post_build_high/low are not provided
      post_build_high/low — preferred: actual H/L during the IC's life
    """
    short_call = ic.call_leg.short_strike if ic.call_leg else None
    short_put = ic.put_leg.short_strike if ic.put_leg else None
    if short_call is None or short_put is None:
        return {"outcome": "no_ic", "pnl_dollars": 0.0,
                "headroom_call": None, "cushion_put": None,
                "tagged_call": False, "tagged_put": False, "msg": "no IC built today"}

    # Scale-normalize: IC instrument can be XSP (1/10 SPX scale) but session
    # bars are always SPX-scaled (predictor tracks SPX directly). Detect the
    # scale gap by comparing short_call to session_close — if close > strike × 5,
    # assume XSP strikes vs SPX bars and divide session prices by 10.
    if session_close > short_call * 5:
        scale_div = 10.0
        session_high = session_high / scale_div
        session_low = session_low / scale_div
        session_close = session_close / scale_div
        if post_build_high is not None:
            post_build_high = post_build_high / scale_div
        if post_build_low is not None:
            post_build_low = post_build_low / scale_div

    # Tag detection — prefer post-build H/L if available (the IC didn't exist
    # before its built_at, so pre-build excursions are noise).
    tag_high = post_build_high if post_build_high is not None else session_high
    tag_low  = post_build_low  if post_build_low  is not None else session_low
    high_tagged = tag_high >= short_call
    low_tagged = tag_low <= short_put

    # Settlement-based outcome (where the close lands)
    if session_close > short_call:
        outcome = "BAAAAD_call_itm"
        emoji = "✗"
    elif session_close < short_put:
        outcome = "BAAAAD_put_itm"
        emoji = "✗"
    elif (abs(session_close - short_call) / short_call) < ATM_PCT_BAND \
         or (abs(session_close - short_put) / short_put) < ATM_PCT_BAND:
        outcome = "GRAZE"
        emoji = "🤏"
    elif high_tagged or low_tagged:
        # Wing tagged intraday but settled inside — still max profit
        outcome = "ATM_tagged"
        emoji = "🤏"
    else:
        outcome = "GOOD"
        emoji = "✓"

    # P&L estimate
    pnl: Optional[float] = None
    if ic.total_credit_dollars and ic.total_max_loss_dollars:
        if outcome.startswith("BAAAAD"):
            pnl = -float(ic.total_max_loss_dollars)
        else:
            pnl = float(ic.total_credit_dollars)

    # EXECUTED reality beats settlement fantasy: if this condor was closed LIVE
    # by the breakeven stop, score THAT (≈ scratch), not where price settled
    # hours after we were already out. (2026-06-10: scorer said −$2,005 'breached
    # at settlement' for a position that realized −$23 at the stop.)
    if getattr(ic, "broker_status", None) == "closed_stop":
        outcome = "STOPPED_BE"
        emoji = "🛑"
        pnl = 0.0

    headroom_call = short_call - session_high
    cushion_put = session_low - short_put

    return {
        "outcome": outcome,
        "emoji": emoji,
        "short_call": float(short_call),
        "short_put": float(short_put),
        "session_high": float(session_high),
        "session_low": float(session_low),
        "session_close": float(session_close),
        "headroom_call": float(headroom_call),
        "cushion_put": float(cushion_put),
        "tagged_call": bool(high_tagged),
        "tagged_put": bool(low_tagged),
        "pnl_dollars": pnl,
    }


# ─────────────────────────────────────────────────────────────────────
# Telegram message formatters
# ─────────────────────────────────────────────────────────────────────

def format_wave_summary(
    date_str: str,
    resolutions: list[dict],
    trend: str | None = None,
    rsi: float | None = None,
    stoch_k: float | None = None,
    regime: str | None = None,
) -> str:
    if not resolutions:
        lines = [f"📊 EOD WAVE — {date_str}", "No wave signals fired today."]
        # Explain WHY — the user deserves context, not silence
        reasons = []
        if trend == "down":
            reasons.append("market in downtrend — put signals blocked by trend filter, "
                           "stoch never reached overbought zone for calls")
        elif trend == "up":
            reasons.append("market in uptrend — call signals blocked by trend filter, "
                           "stoch never reached oversold zone for puts")
        else:
            reasons.append("stoch didn't cross from extreme zones (no reversal trigger)")
        if regime == "volatile":
            reasons.append("volatile regime — wave signals only fire in non-volatile sessions")
        if reasons:
            lines.append("reason: " + "; ".join(reasons))
        ind = []
        if rsi is not None:
            ind.append(f"RSI {rsi:.0f}")
        if stoch_k is not None:
            ind.append(f"StochK {stoch_k:.0f}")
        if trend:
            ind.append(f"trend={trend}")
        if ind:
            lines.append("final state: " + " · ".join(ind))
        return "\n".join(lines)

    n = len(resolutions)
    n_win = sum(1 for r in resolutions if is_win(r["outcome"]))
    n_loss = n - n_win
    wr = (n_win / n * 100) if n else 0
    total_pnl = sum(r["pnl_dollars"] for r in resolutions)

    lines = [
        f"📊 EOD WAVE — {date_str}",
        f"{n} signals · {n_win}W / {n_loss}L · WR {wr:.0f}%",
        f"Net P&L (paper, est): {'+' if total_pnl >= 0 else ''}${total_pnl:,.0f}",
        "",
    ]
    # Show last 8 trades briefly
    lines.append("Trades (chronological):")
    for r in resolutions[-8:]:
        t = r["fired_at"][11:16]  # HH:MM
        side_lbl = "CALL" if r["side"] == "sell_call_cs" else "PUT "
        ko = r["outcome"]
        if ko == "expire_otm":
            tag = "✓ OTM expire"
        elif ko == "managed_profit":
            tag = "✓ managed"
        else:
            tag = "✗ BREACH"
        sgn = "+" if r["pnl_dollars"] >= 0 else "−"
        lines.append(f"  {t} {side_lbl} ${r['short_strike']:.0f} → {tag} {sgn}${abs(r['pnl_dollars']):.0f}")
    if n > 8:
        lines.insert(4, f"(showing last 8 of {n})")
    return "\n".join(lines)


def format_ic_summary(date_str: str, ic_result: dict) -> str:
    """Single-build IC summary (one-IC days)."""
    if ic_result["outcome"] == "no_ic":
        return f"🦅 EOD IC — {date_str}\nNo IC built today (regime was volatile or build skipped)."

    out = ic_result["outcome"]
    emoji = ic_result["emoji"]
    tag_call = " (TAGGED)" if ic_result["tagged_call"] else ""
    tag_put  = " (TAGGED)" if ic_result["tagged_put"] else ""
    pnl = ic_result["pnl_dollars"]
    pnl_str = (f"+${pnl:.0f}" if pnl is not None and pnl >= 0
               else f"−${abs(pnl):.0f}" if pnl is not None
               else "—")

    # Pretty outcome label
    label = {
        "GOOD": "GOOD ✓ — both wings held, max profit kept",
        "ATM_tagged": "🤏 ATM — wing tagged intraday but close came back inside (still max profit)",
        "GRAZE": "🤏 GRAZE — settled within 0.15% of a short strike",
        "BAAAAD_call_itm": "✗ BAAAAD — call wing breached at settlement",
        "BAAAAD_put_itm": "✗ BAAAAD — put wing breached at settlement",
        "STOPPED_BE": "🛑 STOPPED at ~breakeven — closed LIVE before settlement (real fills logged)",
    }.get(out, f"{emoji} {out}")

    lines = [
        f"🦅 EOD IC — {date_str}",
        f"Session H ${ic_result['session_high']:.2f} · L ${ic_result['session_low']:.2f} · Close ${ic_result['session_close']:.2f}",
        f"Short CALL ${ic_result['short_call']:.0f} · headroom {ic_result['headroom_call']:+.2f}{tag_call}",
        f"Short PUT  ${ic_result['short_put']:.0f} · cushion  {ic_result['cushion_put']:+.2f}{tag_put}",
        "",
        f"Outcome: {label}",
        f"Est P&L (paper): {pnl_str}",
    ]
    return "\n".join(lines)


def format_ic_summary_multi(date_str: str, ic_builds_with_results: list[dict]) -> str:
    """EOD IC summary when multiple builds happened today (auto + /icnow rebuilds).
    Scores each build separately so the trader can see which one would have won.
    """
    if not ic_builds_with_results:
        return f"🦅 EOD IC — {date_str}\nNo IC built today."

    n = len(ic_builds_with_results)
    n_good = sum(1 for r in ic_builds_with_results if r["result"]["outcome"].startswith("GOOD"))
    n_baaaad = sum(1 for r in ic_builds_with_results if r["result"]["outcome"].startswith("BAAAAD"))
    n_other = n - n_good - n_baaaad

    lines = [f"🦅 EOD IC — {date_str}"]
    if n > 1:
        lines.append(f"{n} builds today: {n_good} GOOD · {n_baaaad} BAAAAD · {n_other} other")
        # Session stats are same for all builds (single underlying, single session)
        first = ic_builds_with_results[0]["result"]
        if first.get("outcome") != "no_ic":
            lines.append(
                f"Session H ${first['session_high']:.2f} · "
                f"L ${first['session_low']:.2f} · "
                f"Close ${first['session_close']:.2f}"
            )
        lines.append("")

        for entry in ic_builds_with_results:
            ic = entry["ic"]
            r = entry["result"]
            built_at = ic.built_at[11:16] if ic.built_at and len(ic.built_at) >= 16 else "?"
            trigger_tag = "🤖" if ic.trigger == "auto" else "🖱"
            if r["outcome"] == "no_ic":
                lines.append(f"{trigger_tag} {built_at} {ic.trigger} — no chain data, skipped")
                continue
            out = r["outcome"]
            short_label = {
                "GOOD": "GOOD ✓",
                "ATM_tagged": "🤏 ATM",
                "GRAZE": "🤏 GRAZE",
                "BAAAAD_call_itm": "✗ BAAAAD (call ITM)",
                "BAAAAD_put_itm": "✗ BAAAAD (put ITM)",
            }.get(out, out)
            pnl = r.get("pnl_dollars")
            pnl_str = (f"+${pnl:.0f}" if pnl is not None and pnl >= 0
                       else f"−${abs(pnl):.0f}" if pnl is not None
                       else "—")
            lines.append(
                f"{trigger_tag} {built_at} {ic.trigger}: "
                f"C${r['short_call']:.0f} P${r['short_put']:.0f} → {short_label} {pnl_str}"
            )

        # Aggregate P&L
        total_pnl = sum(r["result"].get("pnl_dollars") or 0 for r in ic_builds_with_results)
        lines.append("")
        lines.append(f"Net (if you held ALL): {'+' if total_pnl >= 0 else ''}${total_pnl:,.0f}")
        lines.append(
            "(paper-trading reality: you probably only held one — pick the matching row above)"
        )
        return "\n".join(lines)

    # Only one build → use single-build format
    return format_ic_summary(date_str, ic_builds_with_results[0]["result"])


# ─────────────────────────────────────────────────────────────────────
# Top-level builder — orchestrator calls this
# ─────────────────────────────────────────────────────────────────────

def build_eod_summaries(
    today_iso: str,                  # "YYYY-MM-DD"
    today_signals: list[SignalEvent],
    today_bars: list[Bar],
    iron_condor: IronCondorBuilder,                  # the IC user actually deployed
    iron_condor_history: list[IronCondorBuilder] | None = None,  # all today's rebuilds (info only)
    # Indicator context for "why no signals" explanation
    trend: str | None = None,
    rsi: float | None = None,
    stoch_k: float | None = None,
    regime: str | None = None,
) -> tuple[str, str]:
    """Returns (wave_msg, ic_msg).

    EOD scores ONLY the IC currently in `iron_condor` field — that's the LATEST
    build, which is what the user would actually have deployed in TWS (each
    /icnow rebuild "rolls" the previous virtual position; in real trading you'd
    only have one IC live at a time). The history is kept for /actionplan
    transparency but not for scoring.
    """
    # Filter signals to today's date
    today_sigs = [s for s in today_signals if s.triggered_at.startswith(today_iso)]
    resolutions = [resolve_wave_signal(s, today_bars) for s in today_sigs]

    # Compute session H/L/Close from today's bars
    if today_bars:
        sess_high = max(b.high for b in today_bars)
        sess_low = min(b.low for b in today_bars)
        sess_close = today_bars[-1].close
    else:
        sess_high = sess_low = sess_close = 0.0

    # Score the LATEST IC (= what was actually deployed)
    ic_for_today = iron_condor if iron_condor and iron_condor.available else None
    # Sanity: only count if it was built today
    if ic_for_today and ic_for_today.built_at and not ic_for_today.built_at.startswith(today_iso):
        ic_for_today = None

    # Count rebuilds for the day's transparency line
    today_history_count = 0
    if iron_condor_history:
        today_history_count = sum(
            1 for b in iron_condor_history
            if b.built_at and b.built_at.startswith(today_iso) and b.available
        )

    wave_msg = format_wave_summary(today_iso, resolutions,
                                    trend=trend, rsi=rsi, stoch_k=stoch_k, regime=regime)
    if ic_for_today is None:
        ic_msg = (f"🦅 EOD IC — {today_iso}\n"
                  "No IC built today (regime was volatile or build skipped).")
    else:
        # Compute POST-BUILD H/L (only bars at/after the IC's built_at)
        post_build_high: float | None = None
        post_build_low: float | None = None
        try:
            built_at = ic_for_today.built_at
            if built_at and today_bars:
                from datetime import datetime as _dt
                ic_built_dt = _dt.fromisoformat(built_at)
                post_bars = [b for b in today_bars if b.time >= ic_built_dt]
                if post_bars:
                    post_build_high = max(b.high for b in post_bars)
                    post_build_low = min(b.low for b in post_bars)
        except Exception as _e:
            log.warning("post-build H/L extraction failed: %s", _e)

        ic_result = resolve_iron_condor(
            ic_for_today, sess_high, sess_low, sess_close,
            post_build_high=post_build_high,
            post_build_low=post_build_low,
        )
        ic_msg = format_ic_summary_with_history_note(
            today_iso, ic_for_today, ic_result, today_history_count,
        )
    return wave_msg, ic_msg


def format_ic_summary_with_history_note(
    date_str: str,
    ic: IronCondorBuilder,
    ic_result: dict,
    history_count: int,
) -> str:
    """Single-IC EOD summary with a footnote on the day's rebuild count."""
    base = format_ic_summary(date_str, ic_result)

    # Annotate which IC was deployed (auto vs /icnow override) + rebuild count
    built_at = ic.built_at[11:16] if ic.built_at and len(ic.built_at) >= 16 else "?"
    trigger_lbl = {
        "auto":  f"🤖 auto-deployed at {built_at} ET",
        "icnow": f"🖱 manual /icnow override at {built_at} ET",
    }.get(ic.trigger, f"@{built_at}")

    extra = [trigger_lbl]
    if history_count > 1:
        extra.append(f"({history_count} total rebuilds today; this one scored = the one you'd have held)")
    return base + "\n\n" + "\n".join(extra)
