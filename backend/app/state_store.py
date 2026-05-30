"""Persist live trading state across backend restarts.

Saves to data/live_state.json (debounced, ~1s after last change). Reloads on
orchestrator init so backend restarts during the session don't wipe:

  • signal_history       — all SignalEvents fired today
  • paper_trades         — every PaperTrade opened (with their resolved exits)
  • iron_condor_history  — every IC build (auto + /icnow)
  • _trade_seq_today     — the next trade_no counter (don't reset to #1)
  • _eod_ic_built_today  — date string for the auto-IC-once-per-day gate

Design: write-debounced, read-once-at-startup. The persisted state is per-day
(date string in the file) so a backend booted Tuesday morning doesn't load
Monday's open positions.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "live_state.json"
DEBOUNCE_SEC = 1.0   # write at most once per second after a change


class StateStore:
    """Thread-safe debounced JSON store. Use save_async() after mutations;
    load() once at startup."""

    def __init__(self):
        self._lock = threading.Lock()
        self._dirty = False
        self._last_write_ts = 0.0
        self._writer_thread: threading.Thread | None = None
        self._stop = threading.Event()

    def load(self) -> dict | None:
        """Load state from disk. Returns None if file missing / unreadable /
        date mismatch (= state from a previous trading day, ignore it).
        """
        if not STATE_PATH.exists():
            return None
        try:
            data = json.loads(STATE_PATH.read_text())
        except Exception as e:
            log.warning("state_store load failed: %s", e)
            return None

        # Day check — if persisted state is from a prior session day in ET,
        # discard it. Tomorrow's session shouldn't load yesterday's trades.
        saved_date = data.get("session_date")
        today_et = datetime.now(ET).strftime("%Y-%m-%d")
        if saved_date != today_et:
            log.info(
                "state_store: persisted state is from %s, today is %s — discarding",
                saved_date, today_et,
            )
            return None
        log.info(
            "state_store: loaded session %s (signals=%d, paper_trades=%d, ic_builds=%d)",
            saved_date,
            len(data.get("signal_history") or []),
            len(data.get("paper_trades") or []),
            len(data.get("iron_condor_history") or []),
        )
        return data

    def save(self, payload: dict) -> None:
        """Synchronous write — sets session_date stamp and writes JSON.
        Used by the debounced async writer."""
        try:
            payload = dict(payload)  # shallow copy
            payload["session_date"] = datetime.now(ET).strftime("%Y-%m-%d")
            payload["last_written"] = datetime.utcnow().isoformat()
            STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, default=_json_default, indent=0))
            tmp.replace(STATE_PATH)  # atomic
            with self._lock:
                self._last_write_ts = time.time()
        except Exception as e:
            log.warning("state_store save failed: %s", e)

    def save_async(self, payload_callable):
        """Mark state dirty + ensure debounce writer is running.
        payload_callable returns a fresh dict on each invocation."""
        with self._lock:
            self._dirty = True
            self._payload_fn = payload_callable
            if self._writer_thread is None or not self._writer_thread.is_alive():
                self._writer_thread = threading.Thread(
                    target=self._writer_loop, daemon=True, name="state-store-writer",
                )
                self._writer_thread.start()

    def _writer_loop(self):
        while not self._stop.is_set():
            time.sleep(DEBOUNCE_SEC)
            with self._lock:
                if not self._dirty:
                    continue
                payload = self._payload_fn()
                self._dirty = False
            self.save(payload)

    def stop(self):
        self._stop.set()


def _json_default(obj):
    """Coerce datetimes / Pydantic models to JSON-friendly forms."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    return str(obj)


# Module-level singleton
store = StateStore()
