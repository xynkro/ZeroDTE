"""Telegram push — adapted from FinancePWA for 0DTE signal alerts.

Same bot (@Tron_shaft_bot) and chat_id used across both projects, so phone
ping works whether dashboard is open or not, regardless of which project
fired it.

parse_mode MUST be one of "none", "MarkdownV2", "HTML" — there is no plain
"Markdown" (per FinancePWA auto-memory, past footgun).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Literal

import httpx


log = logging.getLogger(__name__)
ParseMode = Literal["none", "MarkdownV2", "HTML"]


# ── Mute state ──────────────────────────────────────────────────────────────
# Module-global mute. Set by /shutup command, cleared by /wake. All send()
# calls check this BEFORE hitting Telegram, so muting silences every pathway
# (orchestrator signals, macro news pushes, session open, etc.) at once.

_muted_until: datetime | None = None


def set_mute(until: datetime | None) -> None:
    """Mute outgoing pings until `until` (UTC). Pass None to unmute."""
    global _muted_until
    _muted_until = until


def is_muted() -> bool:
    if _muted_until is None:
        return False
    return datetime.now(timezone.utc) < _muted_until


def mute_remaining_str() -> str:
    if not is_muted():
        return "not muted"
    secs = int((_muted_until - datetime.now(timezone.utc)).total_seconds())
    if secs <= 0:
        return "not muted"
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h{(secs % 3600) // 60}m"
    return f"{secs // 86400}d"


def send(
    text: str,
    parse_mode: ParseMode = "none",
    silent: bool = False,
    chat_id: str | int | None = None,
    message_thread_id: int | None = None,
    override_mute: bool = False,
) -> dict | None:
    """Send a Telegram message. Returns API response or None on missing config.

    Never raises — Telegram failure must not break signal delivery to dashboard.
    Logs warnings instead. Uses sync httpx (already a dep) for simplicity;
    Telegram is fire-and-forget so a 1-3s blocking call on the orchestrator
    loop is acceptable for a once-every-few-minutes signal event.

    Routing:
      chat_id            override default (defaults to env TELEGRAM_CHAT_ID).
                         For supergroups use the negative -100xxxxxxxxx ID.
      message_thread_id  topic ID inside a forum supergroup (e.g. 2 for
                         Zero DTE Signals, 6 for Macro Financial News).
                         Omit / 0 for the General topic.
    """
    if is_muted() and not override_mute:
        log.info("Telegram muted — skipping send: %s", text[:60])
        return None

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if chat_id is None:
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "922547929")
    if not token:
        log.warning("TELEGRAM_BOT_TOKEN not set — skipping Telegram send")
        return None

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "disable_notification": silent,
    }
    if parse_mode != "none":
        payload["parse_mode"] = parse_mode
    if message_thread_id and message_thread_id > 0:
        payload["message_thread_id"] = int(message_thread_id)

    try:
        with httpx.Client(timeout=10.0) as client:
            r = client.post(url, json=payload)
            # Capture body BEFORE raise_for_status so we see Telegram's error detail
            try:
                body = r.json()
            except Exception:
                body = {"raw": r.text}

            # Phantom-topic fallback: when Telegram says "message thread not found"
            # (typically a stale topic_id from a deleted+recreated forum topic),
            # retry WITHOUT message_thread_id so the reply lands in General. Better
            # to surface than to silently fail.
            if (r.status_code == 400 and "message thread not found"
                    in (body.get("description") or "").lower()
                    and payload.get("message_thread_id") is not None):
                log.warning("phantom thread_id=%s — retrying without thread",
                            payload.get("message_thread_id"))
                fallback = {k: v for k, v in payload.items() if k != "message_thread_id"}
                fallback["text"] = (
                    "⚠️ couldn't reply in your topic (Telegram says it doesn't exist — likely a "
                    "client cache glitch from renaming/recreating the topic). "
                    "Force-quit Telegram and reopen, or delete + recreate the topic.\n\n"
                    + fallback["text"]
                )
                r = client.post(url, json=fallback)
                try:
                    body = r.json()
                except Exception:
                    body = {"raw": r.text}

            if r.status_code >= 400:
                log.error("Telegram %d: %s (payload chat_id=%s thread=%s)",
                          r.status_code, body, payload.get("chat_id"),
                          payload.get("message_thread_id"))
                return None
        if not body.get("ok"):
            log.error("Telegram API error: %s", body)
            return None
        return body
    except Exception as e:
        log.error("Telegram send failed: %s", e)
        return None


def _route_zero_dte() -> tuple[str | None, int | None]:
    """Return (chat_id, message_thread_id) for Zero DTE Signals topic, or
    (None, None) if not configured (caller falls back to DM)."""
    group = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    topic = os.environ.get("TELEGRAM_TOPIC_ZERO_DTE", "").strip()
    if not group:
        return None, None
    try:
        return group, int(topic) if topic else None
    except ValueError:
        return group, None


def _route_macro() -> tuple[str | None, int | None]:
    """Return (chat_id, message_thread_id) for Macro Financial News topic."""
    group = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    topic = os.environ.get("TELEGRAM_TOPIC_MACRO", "").strip()
    if not group:
        return None, None
    try:
        return group, int(topic) if topic else None
    except ValueError:
        return group, None


def _route_iron_condor() -> tuple[str | None, int | None]:
    """Return (chat_id, thread_id) for end-of-day Iron Condor alerts.

    Currently routes to the same topic as Wave (thread 2) due to a Telegram
    bug that prevents bot posting in newly-created forum topics. Distinguished
    visually via 🦅 emoji + once-daily cadence.
    """
    group = os.environ.get("TELEGRAM_GROUP_CHAT_ID", "").strip()
    topic = os.environ.get("TELEGRAM_TOPIC_IRON_CONDOR", "").strip()
    if not group:
        return None, None
    try:
        return group, int(topic) if topic else None
    except ValueError:
        return group, None


# ── 0DTE-specific alert formatters ──────────────────────────────────────────

def ping_signal(
    side: str,                    # "sell_call_cs" | "sell_put_cs"
    underlying_price: float,
    short_strike: float,
    long_strike: float | None,
    estimated_credit: float | None,
    confluence_score: int,
    confluence_max: int,
    trend: str,                   # "up" | "down" | "flat"
    instrument: str = "XSP",
    pwa_url: str | None = None,
    # New management fields
    trade_no: int = 0,
    contracts: int = 1,
    sizing_note: str | None = None,
    tp_target: float | None = None,
    stop_target: float | None = None,
    strategy: str = "wave",
    tp_pct: float | None = None,        # directional: TP as % of credit (e.g. 90)
    short_delta: int | None = None,     # directional: short-leg delta
) -> dict | None:
    """Fire on each new signal entry. Routes to Wave Zero DTE Signals topic.
    Includes sizing recommendation + management plan so the trader knows the
    entire trade plan from the alert alone.
    """
    if side == "sell_call_cs":
        emoji = "🔴⬆️"
        verb = "SELL CALL"
    else:
        emoji = "🟢⬇️"
        verb = "SELL PUT"

    n_tag = f" #{trade_no}" if trade_no else ""
    delta_tag = f" · {short_delta}Δ" if short_delta else ""
    lines = [
        f"{emoji} ENTRY{n_tag} · {verb} · {instrument}{delta_tag} · conf {confluence_score}/{confluence_max}",
        f"underlying ${underlying_price:.2f}",
        f"short ${short_strike:.0f}" + (f" / long ${long_strike:.0f}" if long_strike else ""),
        f"trend: {trend}"
        + (f" · est credit ${estimated_credit:.0f}" if estimated_credit else ""),
    ]
    # Sizing + management plan
    if sizing_note:
        lines.append(f"size: {sizing_note}")
    if strategy == "directional_spread":
        # Theta-harvest plan: hold for decay, no ladder, stop if spread ~2×, T-30m close
        tp_txt = f"TP {tp_pct:.0f}% credit" if tp_pct is not None else "TP (theta harvest)"
        lines.append(f"plan: {tp_txt} · no ladder · stop if spread 2× · T-30m close")
    elif tp_target is not None or stop_target is not None:
        plan = []
        if tp_target is not None:
            plan.append(f"TP ${tp_target:.2f}")
        if stop_target is not None:
            plan.append(f"STOP ${stop_target:.0f} (short strike)")
        lines.append("plan: " + " · ".join(plan))
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_signal_exit(
    trade_no: int,
    side: str,
    outcome: str,            # "managed_profit" | "stopped_breach" | "time_close" | "eod_expire"
    underlying_at_close: float,
    underlying_at_signal: float,
    short_strike: float,
    contracts: int,
    pnl: float,
    exit_reason: str,
    pwa_url: str | None = None,
    peak_pct_kept: float | None = None,   # directional: peak % of credit captured
    tp_pct: float | None = None,          # directional: configured TP target %
) -> dict | None:
    """Fire when a trade hits TP / STOP / TIME / EOD.
    Routes to Wave Zero DTE Signals so the alert pairs with its entry.
    """
    side_tag = "CALL" if side == "sell_call_cs" else "PUT"
    pct_move = (underlying_at_close - underlying_at_signal) / underlying_at_signal * 100
    pnl_str = f"+${pnl:.0f}" if pnl >= 0 else f"−${abs(pnl):.0f}"
    tp_label = f"{tp_pct:.0f}% credit captured" if tp_pct is not None else "credit captured"

    if outcome == "managed_profit":
        emoji = "✅"; tag = "TAKE PROFIT"
    elif outcome == "stopped_breach":
        emoji = "🛑"; tag = "STOP — short strike breached"
    elif outcome == "time_close":
        emoji = "⏰"; tag = "TIME STOP — closing pre-EOD"
    elif outcome == "eod_expire":
        emoji = "✅"; tag = "EOD — expired OTM"
    # Directional spread strategy (May 2026 pivot → honest BS re-validation)
    elif outcome == "tp_target_hit":
        emoji = "🎯"; tag = f"TP HIT — {tp_label}"
    elif outcome == "stop_ladder_hit":
        emoji = "🪜"; tag = "STOP LADDER — profit ratcheted"
    elif outcome == "breach_max_loss":
        emoji = "🛑"; tag = "STOP — loss (spread 2× / strike breach)"
    elif outcome == "max_profit_otm":
        emoji = "✅"; tag = "EOD — expired OTM (max profit)"
    else:
        emoji = "•";  tag = outcome.upper()

    lines = [
        f"{emoji} EXIT #{trade_no} · {tag}",
        f"{side_tag} short ${short_strike:.0f} · {contracts}× contracts",
        f"underlying ${underlying_at_signal:.2f} → ${underlying_at_close:.2f} ({pct_move:+.2f}%)",
    ]
    if peak_pct_kept is not None:
        lines.append(f"peak credit captured: {peak_pct_kept:+.0f}%")
    lines += [
        f"reason: {exit_reason}",
        f"est P&L (paper): {pnl_str}",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_macro_blackout(
    event: str,
    minutes_until: int,
    pwa_url: str | None = None,
) -> dict | None:
    """Fire when entering ±15 min of a high-impact macro event.
    Routes to "Macro Financial News" topic.
    """
    when = "in " + (f"{minutes_until} min" if minutes_until > 0 else "now")
    lines = [
        f"⏸️ DON'T TRADE · {event}",
        f"event {when} — stand aside on new 0DTE entries until released",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_macro()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_macro_news(
    headline: str,
    summary: str | None = None,
    url: str | None = None,
) -> dict | None:
    """Fire when a hot macro news item drops between sessions / pre-market.
    Routes to "Macro Financial News" topic.
    """
    lines = [f"📰 MACRO · {headline}"]
    if summary:
        lines.append(summary[:300])
    if url:
        lines.append(url)
    chat_id, thread_id = _route_macro()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_iron_condor(
    expiry: str,                       # "20260508"
    underlying_price: float,
    instrument: str,                   # "XSP" | "SPX" | "SPY"
    short_call: float,
    long_call: float,
    short_put: float,
    long_put: float,
    total_credit: float | None,        # in $ (per spread, multiplier already applied)
    max_loss: float | None,
    bpr_estimate: float | None,
    pwa_url: str | None = None,
    # Skew info (asymmetric OTM based on obs window drift)
    skew_direction: str | None = None,  # "bearish" | "bullish" | "neutral"
    obs_drift_pct: float | None = None,
    call_pct_otm: float | None = None,
    put_pct_otm: float | None = None,
) -> dict | None:
    """Fire once when end-of-day IC is built (~12:30 ET / 00:30 SGT).

    Currently routed to the Wave topic (thread 2) until the Telegram bug
    affecting newly-created forum topics is resolved. The 🦅 emoji + once-
    a-day cadence make it visually distinct from wave 🔴/🟢 signals.
    """
    # Pretty expiry: 20260508 → 2026-05-08
    if len(expiry) == 8 and expiry.isdigit():
        exp_pretty = f"{expiry[:4]}-{expiry[4:6]}-{expiry[6:]}"
    else:
        exp_pretty = expiry

    # Skew badge for header
    skew_badge = ""
    if skew_direction and skew_direction != "neutral":
        arrow = "↘" if skew_direction == "bearish" else "↗"
        skew_badge = f" · {arrow} {skew_direction} skew"

    lines = [
        f"🦅 IRON CONDOR · {instrument} · {exp_pretty}{skew_badge}",
        f"deploy ~13:00 ET · expire 16:00 ET",
        f"underlying ${underlying_price:.2f}",
        f"CALL leg: short ${short_call:.0f} / long ${long_call:.0f}",
        f"PUT  leg: short ${short_put:.0f} / long ${long_put:.0f}",
    ]
    # Skew detail line
    if skew_direction and skew_direction != "neutral" and call_pct_otm and put_pct_otm:
        drift_str = f"{obs_drift_pct:+.2f}%" if obs_drift_pct is not None else "?"
        lines.append(
            f"skew: obs drift {drift_str} · call {call_pct_otm:.2f}% OTM / put {put_pct_otm:.2f}% OTM"
        )
    money = []
    if total_credit is not None:
        money.append(f"credit ${total_credit:.0f}")
    if max_loss is not None:
        money.append(f"max loss ${max_loss:.0f}")
    if bpr_estimate is not None:
        money.append(f"BPR ${bpr_estimate:.0f}")
    if money:
        lines.append(" · ".join(money))
    else:
        lines.append("credit: chain unavailable (geometric strikes)")
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_iron_condor()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_ic_stop(
    underlying_price: float,
    instrument: str,
    short_call: float,
    short_put: float,
    current_spread_cost: float,     # what it costs to buy back NOW
    original_credit: float,          # what we collected at entry
    stop_threshold: float,           # = original_credit (breakeven)
    pwa_url: str | None = None,
) -> dict | None:
    """🛑 IC STOP alert — fires when IC mark-to-market reaches breakeven (1× credit).
    Theta Profits' "Breakeven IC" rule: close if spread cost >= original credit.
    Tail-risk amputation: turns max-loss days into ~breakeven days.
    """
    # Side that's threatening (closer to its short strike)
    call_dist = short_call - underlying_price
    put_dist  = underlying_price - short_put
    threatened = "CALL" if call_dist < put_dist else "PUT"
    threatened_strike = short_call if threatened == "CALL" else short_put

    lines = [
        f"🛑 IC STOP · {instrument} — close at breakeven",
        f"underlying ${underlying_price:.2f} · {threatened} side threatened (short ${threatened_strike:.0f})",
        f"buyback cost ${current_spread_cost:.0f} ≥ credit collected ${original_credit:.0f}",
        f"action: CLOSE THE {threatened} WING NOW (cap at ~breakeven)",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_iron_condor()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_daily_loss_limit(
    today_pnl: float,
    daily_loss_limit_dollars: float,
    pwa_url: str | None = None,
) -> dict | None:
    """🛑 DAILY LOSS LIMIT REACHED alert. Fires once when crossed; new entries refused."""
    pct = (today_pnl / daily_loss_limit_dollars * 100) if daily_loss_limit_dollars else 0
    lines = [
        f"🛑 DAILY LOSS LIMIT REACHED",
        f"today P&L: −${abs(today_pnl):.0f} (limit: −${abs(daily_loss_limit_dollars):.0f})",
        f"NO NEW ENTRIES until tomorrow's session",
        f"existing positions continue managed",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_iv_gate_skip(
    vix_value: float,
    threshold: float,
    pwa_url: str | None = None,
) -> dict | None:
    """ℹ️ IC SKIP — VIX too high to deploy IC safely."""
    lines = [
        f"ℹ️ IC SKIPPED — VIX too elevated",
        f"VIX1D ${vix_value:.1f} > threshold ${threshold:.1f}",
        f"breach risk too high for premium-selling. No IC today.",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_iron_condor()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_session_open(
    underlying_price: float,
    regime: str,
    proj_high: float | None,
    proj_low: float | None,
    pwa_url: str | None = None,
) -> dict | None:
    """Fire once at 09:45 ET when regime classifies + projections appear.
    Routes to "Zero DTE Signals" topic (it's about today's session)."""
    lines = [
        f"🟢 SESSION OPEN · regime: {regime}",
        f"SPX ${underlying_price:.2f}",
    ]
    if proj_high and proj_low:
        lines.append(f"projected H ${proj_high:.0f} / L ${proj_low:.0f}")
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_morning_alive(
    underlying_price: float,
    feed_type: str,
    pwa_url: str | None = None,
) -> dict | None:
    """Fire once at first bar of each session (09:30 ET), BEFORE regime classifies.
    Guarantees the user always sees a morning ping, even if:
      - regime never classifies (data gap)
      - no signals fire all day (smooth trend)
      - IC build silently fails
    This is the "system alive" heartbeat.
    """
    lines = [
        f"🔔 GOOD MORNING · ZeroDTE bot alive",
        f"SPX ${underlying_price:.2f} · feed: {feed_type}",
        f"watching for signals + IC build...",
    ]
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_midday_status(
    underlying_price: float,
    trend: str,
    rsi: float | None,
    stoch_k: float | None,
    regime: str,
    signals_today: int,
    reason_no_signals: str | None = None,
    pwa_url: str | None = None,
) -> dict | None:
    """Fire once at ~13:00 ET when zero signals have fired today.
    Breaks the silence — tells the user the system IS alive and WHY
    it's quiet (trend filter, stoch not in zone, etc.).
    Added 2026-05-19 after 'no telegram, wtf' feedback."""
    if signals_today > 0:
        return None  # signals fired — this ping would be noise

    lines = [
        f"📡 MIDDAY STATUS · 0 signals so far",
        f"SPX ${underlying_price:.2f} · regime: {regime} · trend: {trend}",
    ]
    ind_parts = []
    if rsi is not None:
        ind_parts.append(f"RSI {rsi:.0f}")
    if stoch_k is not None:
        ind_parts.append(f"StochK {stoch_k:.0f}")
    if ind_parts:
        lines.append("indicators: " + " · ".join(ind_parts))
    if reason_no_signals:
        lines.append(f"why quiet: {reason_no_signals}")
    lines.append("still watching — will alert if conditions align")
    if pwa_url:
        lines.append(f"📱 {pwa_url}")
    chat_id, thread_id = _route_zero_dte()
    return send("\n".join(lines), parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_eod_wave(message: str) -> dict | None:
    """End-of-day Wave summary → Wave Zero DTE Signals topic."""
    chat_id, thread_id = _route_zero_dte()
    return send(message, parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_eod_iron_condor(message: str) -> dict | None:
    """End-of-day IC summary → Iron Condor Zero DTE Signals topic."""
    chat_id, thread_id = _route_iron_condor()
    return send(message, parse_mode="none",
                chat_id=chat_id, message_thread_id=thread_id)


def ping_test() -> dict | None:
    """Smoke-test: pings BOTH topics to verify routing without faking a signal."""
    chat_id, zd_thread = _route_zero_dte()
    _, mc_thread = _route_macro()
    if not chat_id:
        # No group configured — fall back to DM
        return send("✅ ZeroDTE Telegram bridge live (DM mode — group not configured).",
                    parse_mode="none")
    a = send("✅ Zero DTE Signals topic — bridge live.",
             parse_mode="none", chat_id=chat_id, message_thread_id=zd_thread)
    b = send("✅ Macro Financial News topic — bridge live.",
             parse_mode="none", chat_id=chat_id, message_thread_id=mc_thread)
    if a and b:
        return {"ok": True, "zero_dte_msg": a.get("result", {}).get("message_id"),
                "macro_msg": b.get("result", {}).get("message_id")}
    return None
