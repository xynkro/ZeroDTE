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

# Economic calendar source: ForexFactory's free, keyless JSON feed (via the
# faireconomy CDN). Finnhub's /calendar/economic is paid-tier (403 on free keys),
# so we source the calendar here instead. ~this-week + next-week of events with
# impact (High/Medium/Low), forecast, previous, actual.
FF_CALENDAR_URLS = [
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_nextweek.json",
]

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
        # When the calendar endpoint is unavailable (e.g. Finnhub's economic
        # calendar is a PAID-tier endpoint → 403 on the free plan), record the
        # reason ONCE and stop hammering it every 15 min. None = available.
        self._calendar_disabled_reason: str | None = None
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
        """Pull the US economic calendar from ForexFactory's free JSON feed and map
        it to our schema. Finnhub's calendar is paid-tier, so this is the source."""
        if not self._client:
            return
        try:
            events, ok_any = [], False
            for url in FF_CALENDAR_URLS:
                try:
                    r = await self._client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                    r.raise_for_status()
                    raw = r.json()
                except Exception as e:  # noqa: BLE001
                    log.warning("calendar fetch %s failed: %s", url.rsplit("/", 1)[-1], e)
                    continue
                ok_any = True
                for e in (raw or []):
                    if (e.get("country") or "") != "USD":   # US events only
                        continue
                    t = self._parse_ff_time(e.get("date", ""))
                    if t is None:
                        continue
                    events.append({
                        "country": "US",
                        "event": e.get("title", ""),
                        "impact": (e.get("impact") or "low").lower(),  # High/Medium/Low → lower
                        "time": t,                                     # UTC "YYYY-MM-DD HH:MM:SS"
                        "estimate": (e.get("forecast") or None),
                        "actual": (e.get("actual") or None),
                        "prev": (e.get("previous") or None),
                        "unit": None,
                    })
            if not ok_any:
                # Both feeds unreachable — degrade honestly (don't imply a clear week).
                self._calendar_disabled_reason = "economic-calendar feed unreachable (ForexFactory) — events not tracked"
                self._last_calendar_fetch = datetime.now(timezone.utc)
                log.warning("Economic calendar unavailable: %s", self._calendar_disabled_reason)
                return
            if not events:
                # HTTP succeeded but 0 US events survived the USD filter — possible
                # ForexFactory country-field encoding change. Don't silently appear green:
                # in_blackout_window() would return (False, None) on an empty calendar,
                # bypassing the FOMC blackout gate.
                self._calendar_disabled_reason = (
                    "economic-calendar returned 0 US events — possible ForexFactory encoding mismatch"
                )
                self._last_calendar_fetch = datetime.now(timezone.utc)
                log.warning("Calendar: fetch succeeded but 0 events passed USD filter")
                return
            self._calendar_disabled_reason = None   # recovered
            events.sort(key=lambda x: x["time"])
            self._calendar = events
            self._last_calendar_fetch = datetime.now(timezone.utc)
            log.info("MacroFeed calendar (ForexFactory): %d US events", len(events))
        except Exception as e:  # noqa: BLE001
            log.error("calendar fetch failed: %s", e)

    @property
    def news(self) -> list[dict]:
        return self._news

    @property
    def calendar(self) -> list[dict]:
        return self._calendar

    @property
    def calendar_status(self) -> dict:
        """Honest calendar health for the UI — distinguishes 'no events' from
        'calendar unavailable' so the Macro view doesn't imply a clear week when
        we simply can't see the events."""
        return {
            "available": self._calendar_disabled_reason is None,
            "reason": self._calendar_disabled_reason,
            "events": len(self._calendar),
        }

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
    def _parse_ff_time(s: str) -> str | None:
        """ForexFactory date "2026-06-15T08:30:00-04:00" → UTC "YYYY-MM-DD HH:MM:SS"
        (the format _parse_event_time consumes). All-day / unparseable → None."""
        if not s:
            return None
        try:
            return datetime.fromisoformat(s).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return None

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
