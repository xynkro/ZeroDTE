"""Macro news + economic calendar via Finnhub.

Free tier: 60 calls/min. We poll every 5 min for news, every 15 min for calendar.
The calendar gives us a "macro blackout" window — within 30 min of high-impact
event, we tag the signal/dashboard so the trader knows to stand aside.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from .config import settings


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

FINNHUB_BASE = "https://finnhub.io/api/v1"

# Refresh intervals
NEWS_REFRESH_SEC = 5 * 60        # 5 min
CALENDAR_REFRESH_SEC = 15 * 60   # 15 min

# Blackout window: how close to a high-impact event we suppress signals.
# General events: ±15min before / ±5min after (matches CBOE Henry Schwartz article).
# FOMC events: tighter ±30min before, ±15min after (gamma is brutal during the announcement).
BLACKOUT_BEFORE_MIN = 15
BLACKOUT_AFTER_MIN = 5
BLACKOUT_FOMC_BEFORE_MIN = 30
BLACKOUT_FOMC_AFTER_MIN = 15

# Keywords that mark an event as FOMC-class (auto-tightens its blackout)
FOMC_KEYWORDS = ["fomc", "federal reserve", "fed interest", "fed rate", "rate decision",
                 "powell", "fed chair"]

# Keywords for hot-topic news that could move markets sharply
HOT_KEYWORDS = [
    "fed", "fomc", "powell", "rate cut", "rate hike", "inflation",
    "cpi", "ppi", "jobs", "payroll", "unemployment", "gdp",
    "war", "strike", "missile", "attack", "iran", "russia", "ukraine", "china",
    "tariff", "trump", "biden", "election", "shutdown",
    "spx", "spy", "circuit breaker", "crash", "rally",
]


def _is_hot_news(headline: str, summary: str = "") -> bool:
    text = (headline + " " + summary).lower()
    return any(kw in text for kw in HOT_KEYWORDS)


class MacroFeed:
    """Polls Finnhub for general news + economic calendar; caches results."""

    def __init__(self):
        self.api_key = settings.FINNHUB_API_KEY
        self._news: list[dict] = []
        self._calendar: list[dict] = []
        self._last_news_fetch: datetime | None = None
        self._last_calendar_fetch: datetime | None = None
        self._client: httpx.AsyncClient | None = None
        self._task: asyncio.Task | None = None
        # Edge-tracking for Telegram pushes — we only ping NEW hot headlines,
        # not every poll cycle. Initialised on first refresh so startup catalog
        # doesn't flood the Macro topic.
        self._pinged_news_ids: set[int] = set()
        self._first_news_fetch_done: bool = False

    async def start(self):
        if not self.api_key:
            log.warning("FINNHUB_API_KEY not set — macro feed disabled")
            return
        self._client = httpx.AsyncClient(timeout=15.0)
        self._task = asyncio.create_task(self._poll_loop())
        log.info("MacroFeed started")

    async def stop(self):
        if self._task:
            self._task.cancel()
        if self._client:
            await self._client.aclose()

    async def _poll_loop(self):
        # Initial fetch
        await self._refresh_news()
        await self._refresh_calendar()

        while True:
            try:
                await asyncio.sleep(60)
                now = datetime.now(timezone.utc)
                if (self._last_news_fetch is None or
                        (now - self._last_news_fetch).total_seconds() >= NEWS_REFRESH_SEC):
                    await self._refresh_news()
                if (self._last_calendar_fetch is None or
                        (now - self._last_calendar_fetch).total_seconds() >= CALENDAR_REFRESH_SEC):
                    await self._refresh_calendar()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error("macro poll loop error: %s", e)

    async def _refresh_news(self):
        if not self._client:
            return
        try:
            r = await self._client.get(
                f"{FINNHUB_BASE}/news",
                params={"category": "general", "token": self.api_key},
            )
            r.raise_for_status()
            raw = r.json()
            # Keep only headlines from last 24h, sort newest first
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            news = []
            for n in raw:
                ts = datetime.fromtimestamp(n.get("datetime", 0), tz=timezone.utc)
                if ts < cutoff:
                    continue
                hot = _is_hot_news(n.get("headline", ""), n.get("summary", ""))
                news.append({
                    "id": n.get("id"),
                    "datetime": ts.isoformat(),
                    "headline": n.get("headline", ""),
                    "summary": (n.get("summary", "") or "")[:200],
                    "source": n.get("source", ""),
                    "url": n.get("url", ""),
                    "hot": hot,
                })
            news.sort(key=lambda x: x["datetime"], reverse=True)
            self._news = news[:30]  # cap at 30
            self._last_news_fetch = datetime.now(timezone.utc)
            log.info("MacroFeed news refreshed: %d items (%d hot)",
                     len(news), sum(1 for n in news if n["hot"]))

            # Telegram: ping NEW hot headlines to "Macro Financial News" topic.
            # First refresh ever just seeds the dedup set (don't flood with
            # the 24h backlog on startup).
            if not self._first_news_fetch_done:
                self._pinged_news_ids = {n["id"] for n in news if n.get("id") is not None}
                self._first_news_fetch_done = True
            else:
                from . import telegram as tg
                new_hot = [n for n in news
                           if n.get("hot") and n.get("id") is not None
                           and n["id"] not in self._pinged_news_ids]
                # Cap at 3 per refresh to avoid drowning if the feed catches up
                for n in new_hot[:3]:
                    try:
                        tg.ping_macro_news(
                            headline=n.get("headline", ""),
                            summary=n.get("summary"),
                            url=n.get("url"),
                        )
                        self._pinged_news_ids.add(n["id"])
                    except Exception as e:
                        log.warning("ping_macro_news failed: %s", e)
        except Exception as e:
            log.error("news fetch failed: %s", e)

    async def _refresh_calendar(self):
        if not self._client:
            return
        try:
            today = datetime.now(ET).date()
            end = today + timedelta(days=14)
            r = await self._client.get(
                f"{FINNHUB_BASE}/calendar/economic",
                params={
                    "token": self.api_key,
                    "from": today.isoformat(),
                    "to": end.isoformat(),
                },
            )
            r.raise_for_status()
            raw = r.json()
            events = []
            for e in raw.get("economicCalendar", []) or []:
                # We mostly care about US events
                if (e.get("country") or "") != "US":
                    continue
                events.append({
                    "country": e.get("country"),
                    "event": e.get("event", ""),
                    "impact": e.get("impact", "low"),  # high / medium / low
                    "time": e.get("time", ""),       # "YYYY-MM-DD HH:MM:SS"
                    "estimate": e.get("estimate"),
                    "actual": e.get("actual"),
                    "prev": e.get("prev"),
                    "unit": e.get("unit"),
                })
            events.sort(key=lambda x: x["time"])
            self._calendar = events
            self._last_calendar_fetch = datetime.now(timezone.utc)
            log.info("MacroFeed calendar refreshed: %d US events (next 14d)", len(events))
        except Exception as e:
            log.error("calendar fetch failed: %s", e)

    @property
    def news(self) -> list[dict]:
        return self._news

    @property
    def calendar(self) -> list[dict]:
        return self._calendar

    def next_high_impact(self, within_hours: float = 24.0) -> dict | None:
        """Return the soonest high-impact US event within `within_hours`."""
        now = datetime.now(ET)
        cutoff = now + timedelta(hours=within_hours)
        for e in self._calendar:
            if e.get("impact") != "high":
                continue
            t = self._parse_event_time(e.get("time", ""))
            if t is None:
                continue
            if now <= t <= cutoff:
                return {**e, "_t_iso": t.isoformat(), "_minutes_until": int((t - now).total_seconds() / 60)}
        return None

    def in_blackout_window(self) -> tuple[bool, dict | None]:
        """True if we're within blackout window of a high-impact event.
        FOMC-class events get a tighter window (±30min before, ±15 after)."""
        now = datetime.now(ET)
        for e in self._calendar:
            if e.get("impact") != "high":
                continue
            t = self._parse_event_time(e.get("time", ""))
            if t is None:
                continue
            event_name = (e.get("event") or "").lower()
            is_fomc = any(kw in event_name for kw in FOMC_KEYWORDS)
            before_min = BLACKOUT_FOMC_BEFORE_MIN if is_fomc else BLACKOUT_BEFORE_MIN
            after_min = BLACKOUT_FOMC_AFTER_MIN if is_fomc else BLACKOUT_AFTER_MIN
            delta_min = (t - now).total_seconds() / 60
            if -after_min <= delta_min <= before_min:
                return True, {
                    **e, "_t_iso": t.isoformat(),
                    "_minutes_until": int(delta_min),
                    "_is_fomc": is_fomc,
                    "_blackout_window": f"±{before_min}min" + (" (FOMC)" if is_fomc else ""),
                }
        return False, None

    @staticmethod
    def _parse_event_time(s: str) -> datetime | None:
        if not s:
            return None
        try:
            # Finnhub time is in UTC e.g. "2026-05-15 12:30:00"
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            return dt.astimezone(ET)
        except Exception:
            return None
