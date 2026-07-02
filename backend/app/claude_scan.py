"""Claude morning scan — a SCORED ADVISOR, never a decision-maker.

One API call per session (~09:45–09:59 ET, before the 10:00 band decision) asking
Claude for a structured pre-market risk read. The verdict is LOGGED (data/
claude_scan.jsonl) and shown on the dashboard — it gates NOTHING. After 25+
sessions we score it against real outcomes exactly like RSI/Stoch/GEX (point-
biserial + filter test, wave_failure_analysis.py pattern). It earns a vote in the
trade path only if it separates winners from losers where the oscillators (|r|<0.1)
could not. Until then it is a measured commentator.

Why this could plausibly add value where price-derived gates can't: scheduled-event
awareness (FOMC/CPI afternoons after calm mornings — the Schwartz gate's blind
spot), overnight/geopolitical context, and coil-before-event days. Why it must be
scored first: no historical Claude exists, so like GEX this is forward-only data.

Hard rules: fail-soft everywhere (no key / API error / bad JSON → logged no-op);
NEVER raises into the bar loop; temperature 0 and a fixed schema for scoring
integrity; one call per session (persisted marker).
"""
from __future__ import annotations

import json
import logging
import os
import re

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
SCAN_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "claude_scan.jsonl")

SYSTEM = """You are the pre-market risk assessor for a deterministic 0DTE SPX \
premium-selling system (WaveZero). At ~10:00 ET it may sell ONE defined-risk SPY \
credit spread at the Bollinger-band extreme, but only on days where morning realized \
vol is above its running median (vol released) and the market pays >=10% of width. \
Your job is NOT to pick trades. It is to flag the risk context that realized-vol \
statistics cannot see: scheduled macro events later today (FOMC/CPI/NFP/auctions), \
overnight or geopolitical developments, index-level regime fragility, and \
coil-before-event conditions. Be calibrated and terse. You are being SCORED against \
realized outcomes; overconfidence and vagueness both count against you.

Return STRICT JSON only, exactly this schema:
{"regime_read": "calm|normal|trend_risk|event_risk",
 "direction_lean": "up|down|neutral",
 "confidence": 0.0,
 "event_risks": ["..."],
 "would_trade_band": true,
 "note": "<=200 chars"}"""


def build_context(orch) -> dict:
    """Compact market context from what the backend already knows. Defensive:
    every field is best-effort — a missing feed never blocks the scan."""
    ctx: dict = {}
    try:
        from datetime import datetime
        from .orchestrator import ET  # type: ignore
        now = datetime.now(ET)
        ctx["session"] = now.strftime("%A %Y-%m-%d")
        ctx["time_et"] = now.strftime("%H:%M")
    except Exception:  # noqa: BLE001
        pass
    try:
        buf = list(orch.predictor._buffer)
        if buf:
            ctx["spot"] = round(buf[-1].close, 2)
            today = [b.close for b in buf[-12:]]
            ctx["open_move_pct"] = round(100.0 * (today[-1] - today[0]) / today[0], 3)
    except Exception:  # noqa: BLE001
        pass
    try:
        ctx["daily_atr"] = round(orch._daily_atr, 1)
    except Exception:  # noqa: BLE001
        pass
    try:
        g = orch.state.gex or {}
        ctx["gex"] = {"regime": g.get("regime"), "net_gex_b": g.get("net_gex_b"),
                      "call_wall": g.get("call_wall"), "put_wall": g.get("put_wall"),
                      "max_pain": (g.get("oi") or {}).get("max_pain"),
                      "gamma_flip": (g.get("oi") or {}).get("gamma_flip")}
    except Exception:  # noqa: BLE001
        pass
    try:
        evs = [e for e in (orch.macro._calendar or [])
               if (e.get("impact") or "").lower() in ("high", "medium")][:8]
        ctx["macro_events_today"] = [
            {"event": e.get("event"), "time_utc": e.get("time"), "impact": e.get("impact")}
            for e in evs]
    except Exception:  # noqa: BLE001
        ctx["macro_events_today"] = "unavailable (calendar feed degraded)"
    return ctx


async def run_scan(context: dict, api_key: str, model: str,
                   timeout: float = 45.0) -> dict | None:
    """One Messages-API call → parsed verdict dict, or None on any failure."""
    import httpx
    body = {
        "model": model,
        "max_tokens": 400,
        "temperature": 0,
        "system": SYSTEM,
        "messages": [{
            "role": "user",
            "content": ("Pre-market context (JSON):\n" + json.dumps(context) +
                        "\n\nReturn the verdict JSON now."),
        }],
    }
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01",
               "content-type": "application/json"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0)) as c:
            r = await c.post(API_URL, json=body, headers=headers)
            r.raise_for_status()
            data = r.json()
            text = "".join(b.get("text", "") for b in data.get("content", [])
                           if b.get("type") == "text")
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                log.warning("claude_scan: no JSON in response (%.120s)", text)
                return None
            verdict = json.loads(m.group(0))
            usage = data.get("usage", {})
            verdict["_model"] = data.get("model", model)
            verdict["_tokens"] = {"in": usage.get("input_tokens"),
                                  "out": usage.get("output_tokens")}
            return verdict
    except Exception as e:  # noqa: BLE001 — advisor must never break anything
        log.warning("claude_scan failed: %s", e)
        return None


def append_scan(rec: dict) -> None:
    """One JSON line per session — the dataset the advisor gets SCORED on."""
    try:
        os.makedirs(os.path.dirname(SCAN_PATH), exist_ok=True)
        with open(SCAN_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("claude_scan append failed: %s", e)
