"""GitOps control channel — apply PWA-issued commands from the `control` branch.

The public Pages PWA can't reach this backend directly, so it commits a
control.json to the repo's `control` branch (using the user's OWN repo-scoped
GitHub token, held only in their browser). This poller reads that branch every
~60 s via an authenticated `git fetch` (no extra token here — it reuses the
existing gh credential helper) and applies:

  • telegram_prefs — DESIRED STATE, applied whenever the integer `v` increases
  • command        — a one-shot ("test_telegram"), applied once per `command_nonce`

The backend is NEVER exposed to the internet — GitHub is the message bus. State
(last-applied v + nonce) persists to backend/data/control_state.json so a restart
never re-applies a stale one-shot command. Settings re-apply is idempotent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
from pathlib import Path

from . import telegram as tg
from . import telegram_prefs

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[2]
_STATE = REPO / "backend" / "data" / "control_state.json"
BRANCH = "control"
POLL_SECONDS = 60


def _load_state() -> dict:
    try:
        return json.loads(_STATE.read_text())
    except Exception:  # noqa: BLE001
        return {"applied_v": -1, "applied_nonce": None}


def _save_state(s: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(s))
    except Exception as e:  # noqa: BLE001
        log.warning("control_state save failed: %s", e)


def _read_control() -> dict | None:
    """Blocking: fetch the control branch, return parsed control.json (or None)."""
    try:
        subprocess.run(["git", "fetch", "-q", "origin", BRANCH], cwd=REPO,
                       capture_output=True, text=True, timeout=25, check=True)
    except Exception as e:  # noqa: BLE001 — branch may not exist yet / offline
        log.debug("control fetch skipped: %s", e)
        return None
    out = subprocess.run(["git", "show", "FETCH_HEAD:control.json"], cwd=REPO,
                         capture_output=True, text=True)
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except Exception:  # noqa: BLE001
        return None


def _apply(ctrl: dict, state: dict) -> bool:
    """Apply control.json against last-applied state. Returns True if anything changed."""
    changed = False
    # Desired-state telegram prefs — applied when the version increases.
    try:
        v = int(ctrl.get("v", 0))
    except (TypeError, ValueError):
        v = 0
    if v > int(state.get("applied_v", -1)):
        tp = ctrl.get("telegram_prefs")
        if isinstance(tp, dict):
            telegram_prefs.save(tp)
            log.info("control: applied telegram_prefs (v%d)", v)
        state["applied_v"] = v
        changed = True
    # One-shot command — applied once per nonce.
    nonce = ctrl.get("command_nonce")
    cmd = ctrl.get("command")
    if cmd and nonce and nonce != state.get("applied_nonce"):
        if cmd == "test_telegram":
            tg.ping_test()
            log.info("control: executed one-shot 'test_telegram'")
        else:
            log.warning("control: ignoring unknown command %r", cmd)
        state["applied_nonce"] = nonce
        changed = True
    return changed


async def run() -> None:
    log.info("control poller started (branch=%s, every %ds)", BRANCH, POLL_SECONDS)
    loop = asyncio.get_event_loop()
    while True:
        try:
            ctrl = await loop.run_in_executor(None, _read_control)
            if ctrl:
                state = _load_state()
                if _apply(ctrl, state):
                    _save_state(state)
        except Exception as e:  # noqa: BLE001 — must never kill the loop
            log.warning("control poll error: %s", e)
        await asyncio.sleep(POLL_SECONDS)
