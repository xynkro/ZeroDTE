#!/usr/bin/env python3
"""Publish a public-safe monitor.json snapshot to the `data` branch.

The GitHub Pages dashboard (https://xynkro.github.io/ZeroDTE/) runs read-only
off this snapshot. We read the LOCAL backend, strip everything sensitive, and
force-update a single-commit orphan `data` branch using git plumbing — so this
never touches the working tree/index/HEAD and the branch never grows history.

Public-safe by construction: only strategy aggregates + directional-spread
paper trades go in. No API keys, no account balance, no personal positions.

Guard: if the backend is unreachable we EXIT WITHOUT PUBLISHING, so a transient
backend outage never wipes the last good snapshot on the dashboard.

Run by launchd every few minutes (com.caspar.zerodte-publish). Safe anytime.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

REPO = os.path.expanduser("~/Documents/Trading/ZeroDTE")
BACKEND = "http://127.0.0.1:8765"
BRANCH = "data"

# Only these trade fields are published — a deliberate whitelist (everything the
# dashboard renders, nothing it doesn't). Keeps the public surface minimal.
TRADE_FIELDS = (
    "trade_no", "fired_at", "closed_at", "side", "instrument",
    "short_strike", "long_strike", "estimated_credit", "outcome",
    "pnl", "peak_pct_kept", "current_stop_pct_kept", "broker_status",
    "strategy", "contracts",
)


def _get(path: str, default):
    try:
        with urllib.request.urlopen(BACKEND + path, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"warn: GET {path} -> {e}", file=sys.stderr)
        return default


def backend_alive() -> bool:
    try:
        with urllib.request.urlopen(BACKEND + "/api/status", timeout=6) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def build_snapshot() -> dict:
    stats = _get("/api/monitor/stats", {})
    trades_raw = _get("/api/paper_trades", [])
    alpaca_raw = _get("/api/alpaca/status", {}) or {}
    tg_raw = _get("/api/telegram/prefs", {}) or {}
    debrief = _get("/api/debrief", {}) or {}
    signals = _get("/api/signals", {}) or {}
    ds = [t for t in trades_raw if t.get("strategy") == "directional_spread"]
    trades = [{k: t.get(k) for k in TRADE_FIELDS} for t in ds]
    alpaca = {
        "enabled": alpaca_raw.get("enabled", False),
        "trading_enabled": alpaca_raw.get("trading_enabled", False),
        "open_orders": alpaca_raw.get("open_orders", 0),
        "base_url": alpaca_raw.get("base_url", ""),
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stats": stats,
        "trades": trades,
        "alpaca": alpaca,
        # Current Telegram prefs + the type registry, so the phone PWA can render
        # and pre-fill the settings panel with no backend connection.
        "telegram_prefs": tg_raw.get("prefs", {}),
        "telegram_types": tg_raw.get("message_types", []),
        # Auto session debrief (latest session) — rendered read-only on the phone.
        "debrief": debrief,
        # The 'brain' cockpit — latest signal + sell zones + open positions, so the
        # phone Signals tab works off the snapshot (countdown/P&L tick client-side).
        "signals": signals,
    }


# Explicit identity so `commit-tree` works regardless of global git config or
# launchd's minimal environment (user.name/email may be unset).
_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "ZeroDTE Publisher",
    "GIT_AUTHOR_EMAIL": "publisher@zerodte.local",
    "GIT_COMMITTER_NAME": "ZeroDTE Publisher",
    "GIT_COMMITTER_EMAIL": "publisher@zerodte.local",
}


def _git(args: list[str], stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, input=stdin, env=_GIT_ENV,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def publish(snapshot: dict) -> str:
    """Force-update orphan `data` branch with monitor.json via plumbing."""
    payload = json.dumps(snapshot, indent=2)
    blob = _git(["hash-object", "-w", "--stdin"], stdin=payload)
    tree = _git(["mktree"], stdin=f"100644 blob {blob}\tmonitor.json\n")
    msg = f"data: monitor snapshot {snapshot['generated_at']}"
    commit = _git(["commit-tree", tree, "-m", msg])  # no -p => orphan, 1 commit
    _git(["push", "-f", "origin", f"{commit}:refs/heads/{BRANCH}"])
    return commit


def main() -> int:
    if not backend_alive():
        print("backend unreachable — skipping publish (keeping last good snapshot)",
              file=sys.stderr)
        return 0
    snap = build_snapshot()
    commit = publish(snap)
    n_closed = (snap.get("stats") or {}).get("total", 0)
    n_open = len((snap.get("stats") or {}).get("open_trades", []))
    print(f"published {commit[:9]} — {n_closed} closed, {n_open} open, "
          f"{len(snap['trades'])} trades, at {snap['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
