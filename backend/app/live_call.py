"""Config L — the LIVE-READ PROBE (pre-registered 2026-10-09, docs/TRIAL_GATES.md).

A read (lean / level / invalidation / confidence) — from Caspar, from Claude in-session, or
from the scheduled scan — becomes ONE defined-risk SPY 0DTE credit spread on the PAPER account,
executed and managed by the SAME machinery as every prior trade: marketable-limit entry at the
executable credit, real-fill capture, TP / breach exits, the 15:25 ET time stop, the durable
ledger. Every read is journaled (data/live_calls.jsonl) and scored twice: direction vs the
close, dollars vs broker fills.

Rules enforced here (the pre-registration): paper endpoint only; trading enabled and not
halted; live Alpaca bars no older than 7 minutes; ONE contract, ONE open position, at most
CALL_MAX_TRADES_PER_DAY entries, no entry after CALL_LAST_ENTRY_ET, day halt at CALL_DAY_HALT_USD;
$2-wide spread (settings.SPY_WING_DOLLARS must equal the pre-registered width); executable
credit (short.bid − long.ask, live NBBO) ≥ CALL_FLOOR_PCT_OF_WIDTH of the width; the short
strike sits AT the read's invalidation level. Neutral reads trade nothing.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .config import settings
from . import telegram as tg

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")
SG = ZoneInfo("Asia/Singapore")
CALLS_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "live_calls.jsonl")
PREREG_WIDTH_SPY = 2.0          # docs/TRIAL_GATES.md 2026-10-09 — change there first
MAX_BAR_AGE_SEC = 420
_LOCK = asyncio.Lock()          # one submit at a time: the open-position check must not race


def append_call(rec: dict) -> None:
    try:
        os.makedirs(os.path.dirname(CALLS_PATH), exist_ok=True)
        with open(CALLS_PATH, "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except Exception as e:  # noqa: BLE001
        log.warning("live_calls append failed: %s", e)


def load_calls() -> list[dict]:
    try:
        with open(CALLS_PATH) as f:
            return [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return []
    except Exception as e:  # noqa: BLE001
        log.warning("live_calls load failed: %s", e)
        return []


def _hm(s: str) -> int:
    h, m = s.strip().split(":")
    return int(h) * 60 + int(m)


def _f(x, default=None):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def _sentences(text: str) -> list[str]:
    """One sentence per line for Telegram readability (Caspar, 2026-10-10)."""
    import re as _re
    parts = [p.strip() for p in _re.split(r"(?<=[.;!?])\s+", str(text).strip()) if p.strip()]
    return parts or [str(text).strip()]


def format_read(rec: dict) -> str:
    """Telegram message for one read. ALL levels in SPX terms (Caspar: one scale, SPX);
    the instrument's own strikes appear only on the trade line, with SPX in brackets."""
    lean = str(rec.get("lean", "neutral")).upper()
    conf = _f(rec.get("conf"))
    spx = _f(rec.get("spot_spx"))
    blocks = [f"📣 WaveZero READ · {rec.get('date')} {rec.get('time_et', '?')} ET / {rec.get('time_sgt', '?')} SGT · {rec.get('source', '?')}",
              f"SPX {spx:.0f} · lean {lean}" + (f" · conf {conf:.2f}" if conf is not None else "") if spx is not None
              else f"lean {lean}" + (f" · conf {conf:.2f}" if conf is not None else "")]
    inv = _f(rec.get("invalidation")); lvl = _f(rec.get("level"))
    inv_spx = inv * 10 if (inv is not None and inv < 2000) else inv
    lvl_spx = lvl * 10 if (lvl is not None and lvl < 2000) else lvl
    if lean in ("UP", "DOWN") and (lvl_spx is not None or inv_spx is not None):
        blocks.append((f"level SPX {lvl_spx:.0f} · " if lvl_spx is not None else "")
                      + (f"wrong if SPX {'<' if lean == 'UP' else '>'} {inv_spx:.0f}" if inv_spx is not None else ""))
    if rec.get("note"):
        blocks.append("\n".join(_sentences(str(rec["note"])[:600])))
    d = rec.get("decision")
    if d in ("submitted", "dry_run") and rec.get("short") is not None:
        kind = "put" if rec.get("side") == "sell_put_cs" else "call"
        inst = rec.get("instrument", "SPY")
        os_, ol_ = rec.get("order_short", rec["short"]), rec.get("order_long", rec["long"])
        blocks.append((f"→ PAPER #{rec.get('trade_no')}: " if d == "submitted" else "→ DRY RUN: would ")
                      + f"sell {inst} {os_:.0f}/{ol_:.0f} {kind} spread ×1 (SPX {os_ * 10:.0f}/{ol_ * 10:.0f})\n"
                      f"exec credit ${_f(rec.get('exec_credit_ct'), 0):.0f}/ct (floor ${_f(rec.get('floor_ct'), 0):.0f}) · max loss ${_f(rec.get('max_loss_ct'), 0):.0f}/ct\n"
                      f"TP {settings.DIRECTIONAL_TP_TARGET:.0f}% · stop at −100% credit (≈ SPX {os_ * 10:.0f} touched) · {rec.get('time_stop_et', '15:25')} ET close")
    elif d == "no_trade":
        blocks.append("→ no trade (neutral read)")
    elif d == "advisory_only":
        blocks.append("→ advisory only (auto-submit off)")
    elif d in ("rejected", "broker_rejected", "error"):
        reason = str(rec.get("reason") or "")
        if rec.get("order_short") is not None and reason.startswith("executable credit"):
            reason = reason.rsplit(" at ", 1)[0] + f" at SPX {rec['order_short'] * 10:.0f}/{rec['order_long'] * 10:.0f}"
        blocks.append(f"→ NOT traded: {reason}")
    return "\n\n".join(blocks)


def _push(orch, text: str) -> None:
    """Telegram, off the event loop, routed like every other ZeroDTE ping."""
    try:
        cid, tid = tg._route_zero_dte()
        orch._tg(tg.send, text, chat_id=cid, message_thread_id=tid)
    except Exception as e:  # noqa: BLE001
        log.warning("live_call telegram failed: %s", e)


def _today_broker_pnl(orch, date: str) -> float:
    tot = 0.0
    for t in orch.paper_trades:
        if str(t.fired_at)[:10] == date and t.closed and t.broker_realized_pnl is not None:
            tot += float(t.broker_realized_pnl)
    return tot


async def _spy_live_trade(orch) -> float | None:
    """Latest SPY trade from Alpaca's data API (IEX), for the XSP/SPY basis at call time.
    Best-effort; None on any failure."""
    try:
        import httpx
        url = f"{settings.ALPACA_DATA_URL}/v2/stocks/SPY/trades/latest"
        async with httpx.AsyncClient(timeout=httpx.Timeout(6.0, connect=3.0)) as c:
            r = await c.get(url, headers=orch.alpaca_trader._headers(), params={"feed": "iex"})
            r.raise_for_status()
            p = (r.json().get("trade") or {}).get("p")
            return float(p) if p else None
    except Exception as e:  # noqa: BLE001
        log.debug("live SPY trade unavailable: %s", e)
        return None


async def submit_call(orch, *, lean: str, conf, side: str | None = None,
                      short=None, level=None, invalidation=None, note: str = "",
                      source: str = "manual", dry_run: bool = False, **_ignored) -> dict:
    now = datetime.now(ET)
    date = now.strftime("%Y-%m-%d")
    ts_et = f"{(16 * 60 - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE) // 60}:{(16 * 60 - settings.WAVE_TIME_STOP_MIN_BEFORE_CLOSE) % 60:02d}"
    rec: dict = {
        "ts": datetime.now(timezone.utc).isoformat(), "date": date,
        "time_et": now.strftime("%H:%M"), "time_sgt": now.astimezone(SG).strftime("%H:%M"),
        "source": source, "lean": str(lean or "neutral").strip().lower(), "conf": _f(conf, 0.0),
        "level": _f(level), "invalidation": _f(invalidation), "note": str(note or "")[:400],
        "time_stop_et": ts_et, "dry_run": bool(dry_run),
    }

    def _finish(decision: str, reason: str | None = None, push: bool = True) -> dict:
        rec["decision"] = decision
        if reason:
            rec["reason"] = reason
        append_call(rec)
        if push and not dry_run:
            try:
                _push(orch, format_read(rec))
            except Exception as e:  # noqa: BLE001
                log.warning("format/push failed: %s", e)
        log.info("live_call %s: %s", decision, json.dumps(rec, default=str)[:300])
        return rec

    if rec["lean"] not in ("up", "down", "neutral"):
        return _finish("rejected", f"bad lean {lean!r} (up|down|neutral)")

    # ── spot from the engine's own bars (SPX scale = SPY × 10), must be LIVE ──
    buf = list(orch.predictor._buffer)
    if not buf:
        return _finish("rejected", "no bars in the engine yet")
    last = buf[-1]
    lt = last.time.astimezone(ET) if last.time.tzinfo else last.time.replace(tzinfo=ET)
    S0 = float(last.close)
    spot_spy = S0 / 10.0
    rec["spot_spx"] = round(S0, 1)
    rec["spot_spy"] = round(spot_spy, 2)
    rec["bar_age_sec"] = round((now - lt).total_seconds())

    if rec["lean"] == "neutral":
        return _finish("no_trade")
    _min_conf = settings.CALL_MIN_CONF
    if settings.CALL_AM_MIN_CONF > 0 and (now.hour * 60 + now.minute) < 12 * 60:
        _min_conf = max(_min_conf, settings.CALL_AM_MIN_CONF)   # mornings must earn it (PM beat AM in every tested variant)
    if rec["conf"] < _min_conf:
        return _finish("rejected", f"confidence {rec['conf']:.2f} < {_min_conf:.2f}{' (AM bar)' if _min_conf != settings.CALL_MIN_CONF else ''}")
    if now.weekday() > 4:
        return _finish("rejected", "weekend")
    mins = now.hour * 60 + now.minute
    if mins < _hm("09:35") or mins > _hm(settings.CALL_LAST_ENTRY_ET):
        return _finish("rejected", f"outside entry window 09:35–{settings.CALL_LAST_ENTRY_ET} ET")
    if rec["bar_age_sec"] > MAX_BAR_AGE_SEC:
        return _finish("rejected", f"last bar is {rec['bar_age_sec']}s old (feed stale)")
    if getattr(orch.state, "feed_type", None) != "alpaca":
        return _finish("rejected", f"feed is {getattr(orch.state, 'feed_type', None)}, not alpaca")
    if "paper-api" not in (settings.ALPACA_BASE_URL or ""):
        return _finish("rejected", "ALPACA_BASE_URL is not the paper endpoint — refusing")
    if settings.PAPER_BROKER != "alpaca" or getattr(orch, "alpaca_trader", None) is None:
        return _finish("rejected", "paper broker not armed (PAPER_BROKER != alpaca)")
    if not settings.TRADING_ENABLED or getattr(settings, "TRADING_HALTED", False):
        return _finish("rejected", "trading disabled or halted")
    if abs(float(settings.SPY_WING_DOLLARS) - PREREG_WIDTH_SPY) > 1e-9:
        return _finish("rejected", f"SPY_WING_DOLLARS={settings.SPY_WING_DOLLARS} ≠ pre-registered {PREREG_WIDTH_SPY}")
    today_pnl = _today_broker_pnl(orch, date)
    if today_pnl <= -abs(settings.CALL_DAY_HALT_USD):
        return _finish("rejected", f"day halt: today's real P&L ${today_pnl:+.0f} ≤ −${abs(settings.CALL_DAY_HALT_USD):.0f}")
    n_today = sum(1 for c in load_calls() if c.get("date") == date and c.get("decision") == "submitted")
    if n_today >= settings.CALL_MAX_TRADES_PER_DAY:
        return _finish("rejected", f"{n_today} trades already today (max {settings.CALL_MAX_TRADES_PER_DAY})")

    side = side or ("sell_put_cs" if rec["lean"] == "up" else "sell_call_cs")
    if side not in ("sell_put_cs", "sell_call_cs"):
        return _finish("rejected", f"bad side {side}")
    if (side == "sell_put_cs") != (rec["lean"] == "up"):
        return _finish("rejected", f"side {side} contradicts lean {rec['lean']}")
    rec["side"] = side
    put = side == "sell_put_cs"
    width = float(settings.SPY_WING_DOLLARS)

    # SPX-scale levels from a caller (or the model) are normalised to SPY
    inv = rec["invalidation"]
    if inv is not None and inv > 2000:
        inv = inv / 10.0; rec["invalidation"] = inv; rec["scale_note"] = "invalidation given at SPX scale"
    if rec["level"] is not None and rec["level"] > 2000:
        rec["level"] = rec["level"] / 10.0
    if short is None:
        if inv is not None:
            short = math.floor(inv) if put else math.ceil(inv)
        else:
            short = math.floor(spot_spy * (1 - 0.003)) if put else math.ceil(spot_spy * (1 + 0.003))
    short = float(round(_f(short, 0)))
    if short > 2000:
        short = float(round(short / 10.0))
    if put and short >= spot_spy - 0.5:
        return _finish("rejected", f"put short {short:.0f} not OTM vs SPY {spot_spy:.2f}")
    if (not put) and short <= spot_spy + 0.5:
        return _finish("rejected", f"call short {short:.0f} not OTM vs SPY {spot_spy:.2f}")
    dist_pct = 100.0 * abs(spot_spy - short) / spot_spy
    rec["dist_pct"] = round(dist_pct, 3)
    if settings.CALL_MIN_DIST_PCT > 0 and dist_pct < settings.CALL_MIN_DIST_PCT - 1e-9:
        return _finish("rejected", f"invalidation only {dist_pct:.2f}% from spot (< {settings.CALL_MIN_DIST_PCT:.2f}% minimum) — too tight to pay the touch risk")
    long_ = short - width if put else short + width
    rec.update(short=short, long=long_, width=width)

    async with _LOCK:
        open_pos = [t for t in orch.paper_trades
                    if (not t.closed) or t.broker_status == "close_error"]
        if len(open_pos) >= settings.CALL_MAX_OPEN:
            stale = [t for t in open_pos if str(t.fired_at)[:10] < date]
            if stale:
                log.warning("live_call: %d stale open trade(s) from prior sessions block entries: %s",
                            len(stale), [t.id for t in stale])
            return _finish("rejected", f"{len(open_pos)} position(s) open or unmanaged (max {settings.CALL_MAX_OPEN})")

        # ── price on executable NBBO: short.bid − long.ask ──
        inst = (settings.PROBE_UNDERLYING or "SPY").upper()
        if inst not in ("SPY", "XSP"):
            return _finish("rejected", f"PROBE_UNDERLYING={inst!r} unsupported (SPY|XSP)")
        rec["instrument"] = inst
        if inst == "XSP" and now.weekday() == 4 and 15 <= now.day <= 21:
            return _finish("rejected", "XSP 3rd-Friday series is AM-settled — probe stands aside today")
        try:
            from .nbbo_chain import fetch_chain_nbbo
            if inst == "XSP":
                # XSP = SPX/10 (≈ SPY × 1.003 because of SPY's dividend drag). Fetch its own chain
                # around a first guess, take the option-implied XSP spot from put-call parity, and
                # map the SPY-scale read levels onto XSP strikes with the LIVE ratio.
                nb = await fetch_chain_nbbo(orch.alpaca_trader, spot_spy * 1.003, now.strftime("%y%m%d"),
                                            underlying="XSP")
            else:
                nb = await fetch_chain_nbbo(orch.alpaca_trader, spot_spy, now.strftime("%y%m%d"))
        except Exception as e:  # noqa: BLE001
            return _finish("rejected", f"NBBO fetch failed ({inst}): {e}")
        calls = {r["strike"]: r for r in (nb.get("calls") or [])}
        puts = {r["strike"]: r for r in (nb.get("puts") or [])}
        # option-implied spot of the traded instrument (put-call parity, strike nearest spot)
        implied = None
        guess = spot_spy * (1.003 if inst == "XSP" else 1.0)
        for k in sorted(set(calls) & set(puts), key=lambda k: abs(k - guess))[:1]:
            implied = k + calls[k]["mid"] - puts[k]["mid"]
        if implied is None:
            return _finish("rejected", f"{inst} chain has no two-sided ATM quotes")
        rec["nbbo_implied_spot"] = round(implied, 2)
        if inst == "XSP":
            live = await _spy_live_trade(orch)              # basis from a LIVE print, not a 5-min bar
            if live is None and rec["bar_age_sec"] > 330:
                return _finish("rejected", f"no live SPY print and the last bar is {rec['bar_age_sec']}s old")
            basis_spot = live or spot_spy
            rec["spy_live"] = live
            ratio = implied / basis_spot
            if not (1.0005 < ratio < 1.0070):           # observed basis ≈ 1.003 (SPY dividend drag)
                return _finish("rejected", f"XSP/SPY ratio {ratio:.4f} outside 1.0005–1.0070 (feed out of sync)")
            rec["xsp_spy_ratio"] = round(ratio, 5)
            short_i = float(round(short * ratio))          # NEAREST XSP strike (no directional rounding bias)
            long_i = short_i - width if put else short_i + width
            dist_x = 100.0 * abs(implied - short_i) / implied
            if (put and short_i >= implied - 0.5) or ((not put) and short_i <= implied + 0.5):
                return _finish("rejected", f"XSP short {short_i:.0f} not OTM vs implied {implied:.2f}")
            if settings.CALL_MIN_DIST_PCT > 0 and dist_x < settings.CALL_MIN_DIST_PCT - 1e-9:
                return _finish("rejected", f"XSP strike {short_i:.0f} only {dist_x:.2f}% from spot (< {settings.CALL_MIN_DIST_PCT:.2f}% minimum)")
            rec["dist_pct"] = round(dist_x, 3)
        else:
            if abs(implied / spot_spy - 1) > 0.0015:
                return _finish("rejected", f"bar spot {spot_spy:.2f} vs option-implied {implied:.2f}: feed out of sync")
            ratio = 1.0
            short_i, long_i = float(short), float(long_)
        rec.update(order_short=short_i, order_long=long_i)
        rows = puts if put else calls
        s_, l_ = rows.get(float(short_i)), rows.get(float(long_i))
        if not s_ or not l_:
            return _finish("rejected", f"unquotable: {inst} {short_i:.0f}/{long_i:.0f} not both quoted")
        exec_credit = round(s_["bid"] - l_["ask"], 2)          # $/share
        floor = width * settings.CALL_FLOOR_PCT_OF_WIDTH / 100.0
        rec.update(short_bid=s_["bid"], short_ask=s_["ask"], long_bid=l_["bid"], long_ask=l_["ask"],
                   exec_credit_ct=round(exec_credit * 100, 2), floor_ct=round(floor * 100, 2),
                   max_loss_ct=round((width - exec_credit) * 100, 2))
        if exec_credit < floor:
            return _finish("rejected", f"executable credit ${exec_credit * 100:.0f}/ct < floor ${floor * 100:.0f}/ct at {inst} {short_i:.0f}/{long_i:.0f}")
        if dry_run:
            return _finish("dry_run", push=False)

        # ── build the trade through the validated path ──
        from .models import SignalEvent, StrikeSuggestion
        from .directional_spread_manager import open_directional_trade
        from . import bs_pricing as bs
        today_closes = [b.close for b in buf
                        if (lambda t: t.strftime("%Y-%m-%d") == date and (9, 30) <= (t.hour, t.minute) < (16, 0))
                        (b.time.astimezone(ET) if b.time.tzinfo else b.time.replace(tzinfo=ET))]
        r5 = bs.realized_5m_std(today_closes) if len(today_closes) >= 5 else None
        if not r5 or r5 <= 0:
            return _finish("rejected", "insufficient intraday history for the exit model (need 5 RTH bars)")
        ev = SignalEvent(side=side, triggered_at=last.time.isoformat(), underlying_price=S0,
                         confluence={"live_read": True}, confluence_score=4)
        spx_credit = exec_credit * 100 * 10.0            # SPY contract $ → SPX-scale $ (×10)
        eng_short = round(short_i / ratio * 10.0, 2)      # the REAL legs expressed in engine scale (SPY×10)
        eng_long = round(long_i / ratio * 10.0, 2)
        sp = StrikeSuggestion(instrument="SPX", side=side, mode="directional_spread",
                              short_strike=eng_short, long_strike=eng_long,
                              wing_width=abs(eng_long - eng_short), multiplier=100,
                              estimated_credit_dollars=spx_credit,
                              max_loss_dollars=width * 10.0 * 100 - spx_credit,
                              notional_per_contract=S0 * 100)
        trade_no = orch._next_trade_no(now)
        pt, _sizing = open_directional_trade(ev, sp, trade_no=trade_no, realized_std=r5)
        pt.contracts = 1                                  # pre-registered: ONE contract, always
        if inst != "SPY":
            pt.order_underlying = inst                    # broker truth; pt.instrument stays "SPX" (engine scale)
            pt.order_short_strike = float(short_i)
            pt.order_long_strike = float(long_i)
        pt.entry_mid_quote = round(exec_credit * 100, 2)
        pt.breakeven_dist_pct = round(100.0 * abs(S0 - eng_short) / S0, 3)
        pt.proj_high_at_signal = getattr(getattr(orch.state, "regime", None), "proj_high", None)
        pt.proj_low_at_signal = getattr(getattr(orch.state, "regime", None), "proj_low", None)
        orch.paper_trades.append(pt)
        pt.broker_status = "pending"
        task = asyncio.create_task(orch._submit_alpaca_entry(pt, ev, f"live-read {source} conf {rec['conf']:.2f}", True))
        orch._broker_entry_tasks[pt.id] = task
        orch._persist_state()
        rec.update(trade_id=pt.id, trade_no=trade_no, contracts=1)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=25)
        except asyncio.TimeoutError:
            log.warning("live_call: broker entry still pending after 25s (trade #%d)", trade_no)
        except Exception as e:  # noqa: BLE001
            log.warning("live_call: broker entry task error: %s", e)
        rec["broker_status"] = pt.broker_status
        rec["alpaca_order_id"] = pt.alpaca_order_id
        if pt not in orch.paper_trades:
            return _finish("broker_rejected", f"broker did not execute (status {pt.broker_status})")
        if pt.broker_status in ("shadow", "error", "skipped_afterhours"):
            return _finish("broker_rejected", f"broker status {pt.broker_status}")
        return _finish("submitted")
