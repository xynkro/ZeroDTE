"""Cross-restart dedup state for one-shot Telegram alerts.

Stores per-session keys ("which session_open did we already ping?", "which
macro event?", etc.) to a small JSON file. Survives backend restarts so we
don't double-ping the user when redeploying the orchestrator.

Used by:
  orchestrator._refresh_state — session-open ping
  orchestrator._refresh_state — macro blackout ping
  orchestrator._fire_eod_summary — EOD wave + IC summaries
  macro_news (eventually) — first-time-seen-headline pings

Lightweight: file is ≤ 1 KB, written on each update, read once at startup.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any


log = logging.getLogger(__name__)

_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "dedup_state.json"


def _load() -> dict:
    if not _STATE_PATH.exists():
        return {}
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception as e:
        log.warning("failed to load dedup state: %s", e)
        return {}


def _save(d: dict) -> None:
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(d, indent=0))
    except Exception as e:
        log.warning("failed to save dedup state: %s", e)


_state = _load()


def get(key: str) -> Any:
    return _state.get(key)


def set(key: str, value: Any) -> None:
    """Set a dedup key + persist immediately."""
    _state[key] = value
    _save(_state)


def already_done(key: str, value: Any) -> bool:
    """True if key already has this value (= we should skip the action)."""
    return _state.get(key) == value


def mark_done(key: str, value: Any) -> None:
    set(key, value)


def all_state() -> dict:
    """Return a copy of the entire dedup state. For /status diagnostics."""
    return dict(_state)


# ── EOD summary: PER-DATE dedup ──────────────────────────────────────────────
# The old scheme stored ONE value under "eod_summary_fired" = the latest date,
# overwritten each day. So once Jun-11 fired, already_done("eod_summary_fired",
# "2026-06-10") compared "2026-06-11" == "2026-06-10" → False, letting a stale
# Jun-10 "live" bar RE-FIRE Jun-10's summary (with contradictory recomputed
# state). Per-date keys remember every fired session independently and make the
# fire idempotent regardless of bar/restart ordering. Back-compat: the legacy
# single slot still counts as "done" so we don't re-fire on the upgrade.
def eod_done(date_str: str) -> bool:
    if _state.get(f"eod_summary_fired_{date_str}"):
        return True
    return _state.get("eod_summary_fired") == date_str   # legacy single slot


def eod_mark(date_str: str) -> None:
    _state[f"eod_summary_fired_{date_str}"] = True
    _state["eod_summary_fired"] = date_str               # keep for /status readers
    _save(_state)
