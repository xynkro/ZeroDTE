"""Telegram bot command poller.

Long-polls api.telegram.org/getUpdates and dispatches /commands sent in any
chat the bot can read. Authorization is enforced by Telegram user_id (only
Caspar's account = TELEGRAM_AUTHORIZED_USER_ID can control the bot).

Commands:
  /status     — backend health, regime, last bar, indicators, mute state
  /shutup [d] — mute alerts for d (default 1h, examples: 30m, 2h, 1d, forever)
  /wake       — unmute (alerts resume immediately)
  /news       — latest 5 hot macro headlines
  /calendar   — next 3 high-impact economic events
  /signals    — last 5 signals fired
  /help       — list commands

Wired from orchestrator.start() so it lives as long as the backend.
Idempotent: tracks update_id offset so commands aren't replayed on restart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Awaitable, Callable

import httpx

from . import telegram as tg
from .config import settings


if TYPE_CHECKING:
    from .orchestrator import Orchestrator


log = logging.getLogger(__name__)


# ── Authorization ───────────────────────────────────────────────────────────
# Only this Telegram user_id can control the bot. Defaults to the same value
# as TELEGRAM_CHAT_ID when sending DMs (Caspar's user_id = 922547929).
def _authorized_user_id() -> int:
    raw = os.environ.get("TELEGRAM_AUTHORIZED_USER_ID") \
          or os.environ.get("TELEGRAM_CHAT_ID", "922547929")
    try:
        return int(raw)
    except ValueError:
        return 922547929


# ── Command list (registered with Telegram for type-ahead) ──────────────────
COMMANDS_FOR_BOTFATHER = [
    {"command": "actionplan","description": "What's the latest trade action / status now?"},
    {"command": "icnow",     "description": "Build + ping today's Iron Condor right now (skip 12:30 ET wait)"},
    {"command": "status",    "description": "Backend health + regime + last signal"},
    {"command": "portfolio", "description": "Your account snapshot (in Portfolio Ping topic)"},
    {"command": "shutup",    "description": "Mute alerts (default 1h, e.g. /shutup 30m)"},
    {"command": "wake",      "description": "Unmute alerts immediately"},
    {"command": "news",      "description": "Latest 5 hot macro headlines"},
    {"command": "calendar",  "description": "Next 3 high-impact economic events"},
    {"command": "signals",   "description": "Last 5 signals fired"},
    {"command": "help",      "description": "List commands"},
]


# Commands that anyone in the user_account_map may invoke (not just admin)
PUBLIC_COMMANDS = {"portfolio", "positions", "snapshot"}


# ── Duration parser for /shutup ─────────────────────────────────────────────
_DURATION_RE = re.compile(r"^\s*(\d+)\s*([smhd]|min|hr|hour|day)?\s*$", re.IGNORECASE)


def _parse_duration_seconds(s: str | None) -> int | None:
    """Parse '30m', '2h', '1d', 'forever', '' → seconds (or None for forever)."""
    if not s or not s.strip():
        return 3600  # default: 1h
    s = s.strip().lower()
    if s in ("forever", "until_wake", "perma", "permanent"):
        return None
    m = _DURATION_RE.match(s)
    if not m:
        return 3600
    num = int(m.group(1))
    unit = (m.group(2) or "m").lower()
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400,
            "min": 60, "hr": 3600, "hour": 3600, "day": 86400}.get(unit, 60)
    return num * mult


# ── Bot poller ──────────────────────────────────────────────────────────────

class TelegramBotPoller:
    """Long-poll Telegram for /commands; dispatch to handlers; reply in-thread.

    The poller is decoupled from orchestrator state — it receives an Orchestrator
    reference at construction so handlers can read it. Replies always go to
    the same chat + topic (message_thread_id) the command came from.
    """

    def __init__(self, orchestrator: "Orchestrator"):
        self.orch = orchestrator
        self._task: asyncio.Task | None = None
        self._client: httpx.AsyncClient | None = None
        self._offset: int = 0
        self._stopped = asyncio.Event()
        self._authorized_user = _authorized_user_id()

    async def start(self) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            log.warning("TELEGRAM_BOT_TOKEN not set — bot poller disabled")
            return
        self._client = httpx.AsyncClient(timeout=60.0)

        # Register command list so they show in the type-ahead UI
        try:
            await self._client.post(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                json={"commands": COMMANDS_FOR_BOTFATHER},
            )
            log.info("Telegram bot commands registered (%d)", len(COMMANDS_FOR_BOTFATHER))
        except Exception as e:
            log.warning("setMyCommands failed: %s", e)

        # Initialize offset to skip any unread updates from previous runs.
        # Get current updates with limit=1, take the latest update_id, set offset.
        try:
            r = await self._client.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": 1, "offset": -1},
            )
            data = r.json()
            updates = data.get("result", []) if data.get("ok") else []
            if updates:
                self._offset = updates[-1]["update_id"] + 1
                log.info("Telegram poller starting offset=%d (skipping past updates)", self._offset)
        except Exception as e:
            log.warning("could not seed offset: %s", e)

        self._task = asyncio.create_task(self._poll_loop())
        log.info("Telegram bot poller started (authorized_user=%d)", self._authorized_user)

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self) -> None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        backoff = 1.0
        while not self._stopped.is_set():
            try:
                r = await self._client.get(
                    f"https://api.telegram.org/bot{token}/getUpdates",
                    params={
                        "offset": self._offset,
                        "timeout": 25,        # long-poll up to 25s
                        "allowed_updates": '["message"]',
                    },
                )
                data = r.json()
                if not data.get("ok"):
                    log.warning("getUpdates not ok: %s", data)
                    await asyncio.sleep(min(backoff, 30.0))
                    backoff = min(backoff * 2, 30.0)
                    continue
                backoff = 1.0
                for upd in data.get("result", []):
                    self._offset = upd["update_id"] + 1
                    msg = upd.get("message")
                    if not msg:
                        continue
                    await self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("poll loop error: %s", e)
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 2, 30.0)

    async def _handle_message(self, msg: dict) -> None:
        text = msg.get("text") or ""
        # Accept both /cmd and ~/cmd forms (FinancePWA brief mentioned legacy ~/ usage)
        if text.startswith("~/"):
            text = text[1:]
        if not text.startswith("/"):
            return  # not a command

        # Parse "/cmd@bot args" or "/cmd args"
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].split("@")[0].lower() if parts else ""
        args = parts[1] if len(parts) > 1 else ""

        sender = msg.get("from") or {}
        sender_id = sender.get("id")
        sender_name = sender.get("first_name") or sender.get("username") or f"user_{sender_id}"

        # Reply target — same chat + same thread the command came from
        chat = msg.get("chat", {})
        chat_id = chat.get("id")
        thread_id = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        msg_id = msg.get("message_id")

        log.info("CMD /%s args=%r chat_id=%s thread_id=%s sender=%s(%s)",
                 cmd, args, chat_id, thread_id, sender_name, sender_id)

        # ── Public commands (portfolio family) — allow non-admin senders ──
        # Routing rules per FinancePWA brief:
        #  - Only respond in Portfolio Ping topic (thread_id == 31)
        #  - Resolve sender_id → 'caspar' | 'sarah' via user_account_map
        #  - Unknown sender → polite registration nudge with their user_id
        if cmd in PUBLIC_COMMANDS or (cmd in {"start", "help"} and thread_id == 31):
            await self._handle_public(cmd, args, chat_id=chat_id, thread_id=thread_id,
                                      sender_id=sender_id, sender_name=sender_name,
                                      reply_to_msg_id=msg_id)
            return

        # ── Admin-only commands (everything else) ──
        if sender_id != self._authorized_user:
            log.info("ignoring admin command from unauthorized user_id=%s", sender_id)
            return

        try:
            await self._dispatch(cmd, args, chat_id=chat_id, thread_id=thread_id)
        except Exception as e:
            log.exception("command /%s failed: %s", cmd, e)
            tg.send(f"⚠️ /{cmd} failed: {e}", chat_id=chat_id, message_thread_id=thread_id,
                    override_mute=True)

    async def _handle_public(
        self, cmd: str, args: str, *,
        chat_id, thread_id, sender_id, sender_name, reply_to_msg_id,
    ) -> None:
        """Portfolio family — anyone in user_account_map can invoke; topic-gated."""
        from . import portfolio as pf

        # Topic gate: only the Portfolio Ping topic (id 31).
        # Silent ignore in other topics so we don't pollute Wave/Macro/IC channels.
        if thread_id != pf.PORTFOLIO_PING_TOPIC_ID:
            log.info("public cmd /%s ignored — wrong topic (got %s, want %s)",
                     cmd, thread_id, pf.PORTFOLIO_PING_TOPIC_ID)
            return

        user_map = pf.user_account_map()
        account = user_map.get(sender_id)

        try:
            if account is None:
                reply = pf.unknown_user_reply(sender_name, sender_id)
            else:
                reply = pf.fetch_summary(account)
        except Exception as e:
            log.exception("public /%s failed: %s", cmd, e)
            reply = f"⚠️ /{cmd} failed: {e}"

        # Reply in-thread, quoting the trigger message
        tg.send(reply, chat_id=chat_id, message_thread_id=thread_id, override_mute=True)

    async def _dispatch(self, cmd: str, args: str, *, chat_id, thread_id) -> None:
        handlers: dict[str, Callable[[str], Awaitable[str]]] = {
            "status":     self._cmd_status,
            "update":     self._cmd_status,   # alias
            "actionplan": self._cmd_actionplan,
            "plan":       self._cmd_actionplan,
            "last":       self._cmd_actionplan,
            "open":       self._cmd_actionplan,
            "icnow":      self._cmd_ic_now,
            "ic":         self._cmd_ic_now,
            "ironcondor": self._cmd_ic_now,
            "shutup":     self._cmd_shutup,
            "mute":       self._cmd_shutup,
            "wake":       self._cmd_wake,
            "unmute":     self._cmd_wake,
            "news":       self._cmd_news,
            "calendar":   self._cmd_calendar,
            "events":     self._cmd_calendar,
            "signals":    self._cmd_signals,
            "help":       self._cmd_help,
            "start":      self._cmd_help,
        }
        h = handlers.get(cmd)
        if h is None:
            reply = f"❓ unknown command /{cmd}\nTry /help"
        else:
            reply = await h(args)
        # Always reply to same thread; override_mute so mute-toggle confirmations land
        tg.send(reply, chat_id=chat_id, message_thread_id=thread_id, override_mute=True)

    # ── Handlers ────────────────────────────────────────────────────────────

    async def _cmd_help(self, args: str) -> str:
        lines = ["🤖 ZeroDTE bot — commands:"]
        for c in COMMANDS_FOR_BOTFATHER:
            lines.append(f"  /{c['command']} — {c['description']}")
        lines.append("")
        lines.append("Aliases: /update, /mute, /unmute, /events, /positions, /snapshot")
        lines.append("")
        lines.append("/portfolio works in the Portfolio Ping topic for both Caspar & Sarah.")
        return "\n".join(lines)

    async def _cmd_status(self, args: str) -> str:
        s = self.orch.state
        ind = s.indicators
        last_sig = s.last_signals[-1] if s.last_signals else None
        feed_ok = self.orch.feed and self.orch.feed.connected
        mute_str = tg.mute_remaining_str()

        lines = [
            f"📊 STATUS · {datetime.now(timezone.utc).strftime('%H:%M UTC')}",
            f"backend: {s.backend_status} · IBKR: {'connected' if feed_ok else 'offline'}",
            f"regime: {s.regime.regime}"
            + (f" · proj H ${s.regime.proj_high:.0f} L ${s.regime.proj_low:.0f}"
               if s.regime.proj_high and s.regime.proj_low else ""),
        ]
        if s.quote:
            lines.append(f"SPX ${s.quote.last:.2f} (bar {s.quote.timestamp[:16]})")
        if ind.rsi is not None:
            lines.append(f"RSI {ind.rsi:.1f} · Stoch {ind.stoch_k:.0f}/{ind.stoch_d:.0f}")
        if ind.trend:
            lines.append(f"trend: {ind.trend} · filter: {'ON' if ind.trend_filter_enabled else 'OFF'}")
        if last_sig:
            lines.append(f"last signal: {last_sig.side} @ {last_sig.triggered_at[11:16]} "
                         f"(conf {last_sig.confluence_score})")
        if mute_str != "not muted":
            lines.append(f"🔕 muted for {mute_str}")
        return "\n".join(lines)

    async def _cmd_shutup(self, args: str) -> str:
        secs = _parse_duration_seconds(args)
        if secs is None:
            tg.set_mute(datetime.now(timezone.utc) + timedelta(days=365))
            return "🔕 Muted indefinitely. /wake to resume."
        until = datetime.now(timezone.utc) + timedelta(seconds=secs)
        tg.set_mute(until)
        return f"🔕 Muted for {tg.mute_remaining_str()}. /wake to resume early."

    async def _cmd_wake(self, args: str) -> str:
        was_muted = tg.is_muted()
        tg.set_mute(None)
        if was_muted:
            return "🔔 Unmuted. Alerts resume now."
        return "🔔 Already unmuted."

    async def _cmd_news(self, args: str) -> str:
        all_news = self.orch.macro.news or []
        hot = [n for n in all_news if n.get("hot")]
        items = (hot or all_news)[:5]
        if not items:
            return "📰 No news cached yet."
        lines = ["📰 Hot macro headlines:" if hot else "📰 Recent macro:"]
        for n in items:
            ts = n.get("datetime", "")[11:16]  # HH:MM
            head = n.get("headline", "")[:120]
            lines.append(f"• [{ts}] {head}")
        return "\n".join(lines)

    async def _cmd_calendar(self, args: str) -> str:
        cal = self.orch.macro.calendar or []
        high = [e for e in cal if e.get("impact") == "high"]
        items = high[:3]
        if not items:
            return "📅 No high-impact events scheduled in cache."
        lines = ["📅 Next high-impact events (US):"]
        for e in items:
            t = e.get("time", "")[:16]
            lines.append(f"• {t}  {e.get('event', '')}")
        return "\n".join(lines)

    async def _cmd_actionplan(self, args: str) -> str:
        """What should I do RIGHT NOW? Synthesizes from open trades + last
        signal + regime state into a one-screen answer."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        now_et = _dt.now(_ET)
        now_sgt = _dt.now()  # local naive — server runs in SGT

        s = self.orch.state
        ind = s.indicators
        open_trades = [t for t in self.orch.paper_trades if not t.closed]
        recent_closed = sorted(
            [t for t in self.orch.paper_trades if t.closed],
            key=lambda t: t.closed_at or "", reverse=True,
        )[:1]
        last_sig = s.last_signals[-1] if s.last_signals else None

        # Get current price for distance-to-target math
        cur_price = s.quote.last if s.quote else (
            self.orch.predictor._buffer[-1].close if self.orch.predictor._buffer else None
        )

        lines = [f"🎯 ACTION PLAN — {now_et.strftime('%H:%M ET')}"]

        if open_trades:
            lines.append(f"\n🟡 OPEN positions ({len(open_trades)}):")
            for t in open_trades:
                side_lbl = "CALL" if t.side == "sell_call_cs" else "PUT "
                lines.append(f"  #{t.trade_no} {side_lbl} {t.instrument} short ${t.short_strike:.0f} · {t.contracts}× ct")
                lines.append(f"    entry ${t.underlying_at_signal:.2f}"
                             + (f" @{t.fired_at[11:16]}" if t.fired_at else ""))
                if t.tp_underlying_target and t.stop_underlying_target:
                    if cur_price:
                        if t.side == "sell_call_cs":
                            tp_dist = (cur_price - t.tp_underlying_target) / cur_price * 100
                            stop_dist = (t.stop_underlying_target - cur_price) / cur_price * 100
                        else:
                            tp_dist = (t.tp_underlying_target - cur_price) / cur_price * 100
                            stop_dist = (cur_price - t.stop_underlying_target) / cur_price * 100
                        lines.append(f"    TP ${t.tp_underlying_target:.2f} ({tp_dist:+.2f}%)"
                                     f" · STOP ${t.stop_underlying_target:.0f} ({stop_dist:+.2f}%)")
                    else:
                        lines.append(f"    TP ${t.tp_underlying_target:.2f} · STOP ${t.stop_underlying_target:.0f}")
            lines.append(f"\nWATCHING for: TP / STOP / TIME(15:45 ET) / EOD(16:00 ET)")
        else:
            # No open trade — show why
            lines.append("")
            if last_sig is not None:
                last_sig_age_min = (now_et - _dt.fromisoformat(last_sig.triggered_at).astimezone(_ET)).total_seconds() / 60
                if last_sig_age_min < 60 * 8:
                    lines.append(f"Last signal: {last_sig.side} @{last_sig.triggered_at[11:16]}"
                                 f" (conf {last_sig.confluence_score})"
                                 f" — {int(last_sig_age_min)} min ago")
                else:
                    lines.append("No signals yet today")
            else:
                lines.append("No signals yet today")

            if recent_closed:
                t = recent_closed[0]
                pnl_str = (f"+${t.pnl:.0f}" if t.pnl and t.pnl >= 0
                           else f"−${abs(t.pnl):.0f}" if t.pnl else "—")
                outcome_lbl = {
                    "managed_profit": "TP",
                    "stopped_breach": "STOP",
                    "time_close": "TIME",
                    "eod_expire": "EOD",
                }.get(t.outcome or "", t.outcome or "?")
                lines.append(f"Last close: #{t.trade_no} {outcome_lbl} {pnl_str}")

            # Show what we're watching for
            lines.append(f"\n🔍 Watching for: stoch reversal cross + trend filter")

        # Wave window status (no new entries after 14:30 ET by default)
        try:
            from .predictor import WAVE_NO_NEW_ENTRY_AFTER_ET_MIN
            cutoff_h, cutoff_m = divmod(WAVE_NO_NEW_ENTRY_AFTER_ET_MIN, 60)
            cutoff_str = f"{cutoff_h:02d}:{cutoff_m:02d} ET"
            now_min = now_et.hour * 60 + now_et.minute
            if now_min >= WAVE_NO_NEW_ENTRY_AFTER_ET_MIN:
                lines.append(f"\n⏰ Wave window CLOSED ({cutoff_str}): no new entries; existing positions managed.")
            elif now_min >= 9 * 60 + 45:  # post-obs window
                remaining_min = WAVE_NO_NEW_ENTRY_AFTER_ET_MIN - now_min
                hh, mm = divmod(remaining_min, 60)
                lines.append(f"\n⏰ Wave window OPEN — {hh}h {mm}m until {cutoff_str} cutoff")
        except Exception:
            pass

        # Compact regime + filter status
        lines.append(f"\nRegime: {s.regime.regime} · trend: {ind.trend}"
                     f" (filter {'ON' if ind.trend_filter_enabled else 'OFF'})")
        if s.regime.proj_high and s.regime.proj_low:
            lines.append(f"Projected H ${s.regime.proj_high:.0f} / L ${s.regime.proj_low:.0f}")
        if cur_price:
            lines.append(f"SPX now ${cur_price:.2f}")
        if ind.rsi is not None:
            lines.append(f"RSI {ind.rsi:.0f} · Stoch {ind.stoch_k:.0f}/{ind.stoch_d:.0f}")

        # Mute state if active
        if tg.is_muted():
            lines.append(f"\n🔕 muted for {tg.mute_remaining_str()}")

        return "\n".join(lines)

    async def _cmd_ic_now(self, args: str) -> str:
        """Force-build the Iron Condor right now using current chain data,
        bypassing the 12:30 ET schedule. Useful when you want to deploy early."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo as _ZI
        _ET = _ZI("America/New_York")
        now_et = _dt.now(_ET)

        # Verify regime classified
        s = self.orch.state
        if s.regime.regime == "pre_obs":
            return ("⚠️ Cannot build IC yet — observation window still open.\n"
                    "Wait until 09:45 ET (regime classifies + projections drawn).")

        # Verify we have a recent bar
        buf = list(self.orch.predictor._buffer)
        if not buf:
            return "⚠️ No bar data yet. Backend may still be warming up."

        last_bar = buf[-1]
        # Force-build the IC by clearing the dedup + calling the builder
        try:
            # Reset the per-day "already built" guard so the builder runs
            self.orch._eod_ic_built_today = None
            await self.orch._maybe_build_eod_ic_force(last_bar)
        except AttributeError:
            # Fallback: call the regular builder; it gates on 12:30 ET so we
            # need to bypass via _force variant below
            return ("⚠️ /icnow handler not fully wired yet — restart backend "
                    "after this update lands.")
        except Exception as e:
            log.exception("/icnow failed: %s", e)
            return f"⚠️ IC build failed: {e}"

        # Inspect what got built
        ic = self.orch.state.iron_condor
        if not ic.available:
            return ("⚠️ IC build attempted but not available.\n"
                    "Most likely: chain fetch failed (XSP/SPX subscription) "
                    "or projection bounds incomplete. Check /status.")

        # Format the response (mirrors what auto-fire ping_iron_condor sends)
        cl = ic.call_leg
        pl = ic.put_leg
        lines = [
            f"🦅 IRON CONDOR (manual /icnow @ {now_et.strftime('%H:%M ET')})",
            f"underlying ${ic.underlying_price:.2f} · expire {ic.expiry or 'today'}",
            f"CALL leg: short ${cl.short_strike:.0f} / long ${cl.long_strike:.0f}",
            f"PUT  leg: short ${pl.short_strike:.0f} / long ${pl.long_strike:.0f}",
        ]
        if ic.total_credit_dollars:
            lines.append(f"credit ${ic.total_credit_dollars:.0f}")
        if ic.total_max_loss_dollars:
            lines.append(f"max loss ${ic.total_max_loss_dollars:.0f}")
        if ic.bpr_estimate_dollars:
            lines.append(f"BPR ~${ic.bpr_estimate_dollars:.0f}")
        lines.append("")
        lines.append("Auto-ping also fired to Iron Condor topic.")
        return "\n".join(lines)

    async def _cmd_signals(self, args: str) -> str:
        sigs = self.orch.state.last_signals[-5:]
        if not sigs:
            return "📶 No signals yet today."
        lines = ["📶 Last signals:"]
        for s in sigs:
            t = s.triggered_at[11:16]
            lines.append(f"• [{t}] {s.side} @ ${s.underlying_price:.2f} · conf {s.confluence_score}/{len(s.confluence)}")
        return "\n".join(lines)
