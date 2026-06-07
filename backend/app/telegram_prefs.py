"""Runtime-editable Telegram message preferences.

Stored in backend/data/telegram_prefs.json, edited from the backend-served
dashboard (GET/POST /api/telegram/prefs). telegram.py consults these on every
send. DEFAULTS reproduce the pre-customization behaviour EXACTLY, so an absent
or partial file changes nothing — customization is purely additive.

Single-process model: the API and the orchestrator share this module, so a
save() updates the in-memory cache that telegram.py reads on the next ping.
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_PATH = Path(__file__).resolve().parent.parent / "data" / "telegram_prefs.json"
_lock = threading.Lock()
_cache: dict | None = None

# Registry of every alert type the bot can push. `key` must match the type
# string passed by each ping_* function. Drives the dashboard's push-toggle list.
MESSAGE_TYPES = [
    {"key": "entry",            "label": "Entry signals",            "group": "Trades"},
    {"key": "exit",             "label": "Exit / TP / stop",         "group": "Trades"},
    {"key": "iron_condor",      "label": "Iron Condor build",        "group": "Trades"},
    {"key": "ic_stop",          "label": "IC breakeven stop",        "group": "Trades"},
    {"key": "daily_loss_limit", "label": "Daily loss-limit hit",     "group": "Risk"},
    {"key": "feed_stale",       "label": "Feed-stale warning",       "group": "Risk"},
    {"key": "iv_gate_skip",     "label": "IC skipped (VIX too high)", "group": "Risk"},
    {"key": "session_open",     "label": "Session open + regime",    "group": "Heartbeat"},
    {"key": "morning_alive",    "label": "Good-morning heartbeat",   "group": "Heartbeat"},
    {"key": "midday_status",    "label": "Midday status (quiet days)", "group": "Heartbeat"},
    {"key": "macro_blackout",   "label": "Macro don't-trade",        "group": "Macro"},
    {"key": "macro_news",       "label": "Macro news",               "group": "Macro"},
    {"key": "eod_wave",         "label": "EOD wave summary",         "group": "Summaries"},
    {"key": "eod_iron_condor",  "label": "EOD Iron Condor summary",  "group": "Summaries"},
]

DEFAULTS: dict = {
    "types": {t["key"]: True for t in MESSAGE_TYPES},
    "prefix": "",
    "footer": "",
    "link": {"enabled": True, "style": "plain"},   # style: "plain" | "button"
    "detail": {"factors": True, "plan": True, "sizing": True},
}


def _deep_merge(base: dict, over: dict | None) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    data: dict = {}
    try:
        if _PATH.exists():
            data = json.loads(_PATH.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("telegram_prefs load failed (%s) — using defaults", e)
        data = {}
    merged = _deep_merge(DEFAULTS, data)
    # Guarantee every known type has an explicit flag (new types default ON).
    for t in MESSAGE_TYPES:
        merged["types"].setdefault(t["key"], True)
    _cache = merged
    return _cache


def get() -> dict:
    return load()


def save(new_prefs: dict) -> dict:
    """Merge new prefs over current, persist, refresh cache. Returns the result."""
    global _cache
    merged = _deep_merge(load(), new_prefs or {})
    # Sanitize link.style
    if merged.get("link", {}).get("style") not in ("plain", "button"):
        merged["link"]["style"] = "plain"
    with _lock:
        _PATH.parent.mkdir(parents=True, exist_ok=True)
        _PATH.write_text(json.dumps(merged, indent=2))
    _cache = merged
    return merged


def type_enabled(key: str) -> bool:
    return bool(load()["types"].get(key, True))
