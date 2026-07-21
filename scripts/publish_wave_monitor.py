#!/usr/bin/env python3
"""Publish WaveZero's public-safe wave_monitor.json to the `wave-data` branch.

WHY A SEPARATE BRANCH: the main instance's publisher force-updates an ORPHAN
`data` branch whose tree contains ONLY monitor.json — two publishers on one
branch would wipe each other's file on every push. WaveZero therefore owns
`wave-data` (same single-file orphan pattern, zero interference), and the
public PWA's Wave tab reads this file while MEIC's tabs keep reading `data`.

Public-safe by construction (same discipline as the main publisher): strategy
aggregates + directional-spread paper trades + the band decision journal +
the Claude-scan verdict. No keys, no account balances, no CasaaFinance.

Backend outage => exit WITHOUT publishing (never wipe the last good snapshot).
Run by launchd com.caspar.wavezero-publish. Safe anytime.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

REPO = os.path.expanduser("~/Documents/Trading/ZeroDTE-Wave")
BACKEND = "http://127.0.0.1:8766"
BRANCH = "wave-data"
FILENAME = "wave_monitor.json"
JOURNAL = os.path.join(REPO, "backend", "data", "band_decisions.jsonl")
SCANS = os.path.join(REPO, "backend", "data", "claude_scan.jsonl")

TRADE_FIELDS = (
    "trade_no", "fired_at", "closed_at", "side", "instrument",
    "short_strike", "long_strike", "estimated_credit", "outcome",
    "pnl", "peak_pct_kept", "current_stop_pct_kept", "broker_status",
    "strategy", "contracts", "broker_realized_credit", "broker_realized_pnl",
    "entry_mid_quote", "exit_mid_quote", "exit_reason", "closed",
)


def _get(path: str, default):
    try:
        with urllib.request.urlopen(BACKEND + path, timeout=8) as r:
            return json.loads(r.read().decode())
    except Exception as e:  # noqa: BLE001
        print(f"warn: GET {path} -> {e}", file=sys.stderr)
        return default


def _jsonl_tail(path: str, n: int) -> list:
    try:
        with open(path) as f:
            rows = [json.loads(x) for x in f if x.strip()]
        return rows[-n:]
    except Exception:  # noqa: BLE001
        return []


def backend_alive() -> bool:
    try:
        with urllib.request.urlopen(BACKEND + "/api/status", timeout=6) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def build_snapshot() -> dict:
    status = _get("/api/status", {}) or {}
    trades_raw = _get("/api/paper_trades", [])
    debrief = _get("/api/debrief", {}) or {}
    wave_hist = _get("/api/wave/history", {}) or {}
    ds = [t for t in trades_raw if t.get("strategy") == "directional_spread"]
    trades = [{k: t.get(k) for k in TRADE_FIELDS} for t in ds]
    # Claude scan: verdict only (never the raw context — keep the surface minimal)
    scan_rows = _jsonl_tail(SCANS, 1)
    scan = None
    if scan_rows and scan_rows[-1].get("verdict"):
        v = scan_rows[-1]["verdict"]
        scan = {"date": scan_rows[-1].get("date"),
                "regime_read": v.get("regime_read"),
                "direction_lean": v.get("direction_lean"),
                "confidence": v.get("confidence"),
                "would_trade_band": v.get("would_trade_band")}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "instance": "wavezero",
        "trades": trades,
        "debrief": debrief,                      # includes the 🧪 trial block
        "wave_history": wave_hist,               # fresh-baseline filtered
        "band": status.get("band"),              # armed / today's decision
        "band_journal": _jsonl_tail(JOURNAL, 15),  # why each day did/didn't trade
        "claude_scan": scan,
        # The pre-split book (old shared account, stoch config, closed 2026-06-30) —
        # rescued from the fossil monitor.json so the FULL month stays findable.
        "legacy": _load_legacy(),
    }


def _load_legacy():
    try:
        with open(os.path.join(REPO, "backend", "data", "legacy_wave_snapshot.json")) as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return None


_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "WaveZero Publisher",
    "GIT_AUTHOR_EMAIL": "publisher@wavezero.local",
    "GIT_COMMITTER_NAME": "WaveZero Publisher",
    "GIT_COMMITTER_EMAIL": "publisher@wavezero.local",
}


def _git(args: list[str], stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, input=stdin, env=_GIT_ENV,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def publish(snapshot: dict) -> str:
    payload = json.dumps(snapshot, indent=2)
    blob = _git(["hash-object", "-w", "--stdin"], stdin=payload)
    tree = _git(["mktree"], stdin=f"100644 blob {blob}\t{FILENAME}\n")
    msg = f"wave-data: snapshot {snapshot['generated_at']}"
    commit = _git(["commit-tree", tree, "-m", msg])   # orphan, single commit
    _git(["push", "-f", "origin", f"{commit}:refs/heads/{BRANCH}"])
    return commit


def main() -> int:
    if not backend_alive():
        print("backend unreachable — skipping publish (keeping last good snapshot)",
              file=sys.stderr)
        return 0
    snap = build_snapshot()
    commit = publish(snap)
    print(f"published {commit[:9]} — {len(snap['trades'])} trades, "
          f"journal {len(snap['band_journal'])} rows, at {snap['generated_at']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
