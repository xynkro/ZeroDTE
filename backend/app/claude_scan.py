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

SYSTEM = """You are the intraday read for WaveZero, a 0DTE SPX/SPY premium-selling desk. \
You are given live context: today's bars (open, high, low, last, VWAP, last-hour change), the \
prior close, the market-implied move to the close from the ATM straddle, executable 0DTE spread \
credits at 0.2/0.3/0.5% OTM on both sides, dealer gamma walls when available, and today's \
scheduled macro events. Produce ONE calibrated read of where SPY is more likely to close \
relative to now, and the price at which that read is WRONG. If the read is non-neutral with \
confidence >= the desk threshold, the desk sells ONE defined-risk credit spread with its short \
strike AT your invalidation level (a put spread below for lean=up, a call spread above for \
lean=down), managed by rules (take-profit, breach stop, 15:25 ET close). So: the invalidation \
must be a level you would bet is NOT touched before the close, typically 0.2-0.6% away; nearer \
pays more and gets touched more. Say neutral when the straddle-implied move is not favourable to \
one side or an event lands inside the session. You are SCORED on direction vs the close and on \
dollars; overconfidence and vagueness both count against you. Never trade the news; price it.

Return STRICT JSON only, exactly this schema:
{"lean": "up|down|neutral",
 "confidence": 0.0,
 "level": 0.0,
 "invalidation": 0.0,
 "regime_read": "calm|normal|trend_risk|event_risk",
 "event_risks": ["..."],
 "note": "<=200 chars: the one reason for the lean and the one thing that breaks it — quote ALL levels in SPX points (SPY×10), never SPY"}
(level and invalidation are SPY prices; invalidation below spot for up, above for down. In the note, write levels as SPX, e.g. 7760 not 776.)"""


async def build_context_async(orch) -> dict:
    """build_context + live option surface (ATM straddle, OTM credits) and today's bar
    structure. Every addition is best-effort; a failure leaves the base context intact."""
    ctx = build_context(orch)
    try:
        from datetime import datetime
        from .orchestrator import ET  # type: ignore
        buf = list(orch.predictor._buffer)
        now = datetime.now(ET)
        date = now.strftime("%Y-%m-%d")
        def _t(b):
            return b.time.astimezone(ET) if b.time.tzinfo else b.time.replace(tzinfo=ET)
        rth = [b for b in buf if (9, 30) <= (_t(b).hour, _t(b).minute) < (16, 0)]
        today = [b for b in rth if _t(b).strftime("%Y-%m-%d") == date]
        prev = [b for b in rth if _t(b).strftime("%Y-%m-%d") < date]
        if today:
            o = today[0].open; last = today[-1].close
            vol = [getattr(b, "volume", 0) or 0 for b in today]
            vw = (sum(((b.high + b.low + b.close) / 3.0) * v for b, v in zip(today, vol)) / sum(vol)) if sum(vol) > 0 \
                 else sum((b.high + b.low + b.close) / 3.0 for b in today) / len(today)
            lh = today[-12:]
            ctx["today"] = {"open": round(o, 1), "high": round(max(b.high for b in today), 1),
                            "low": round(min(b.low for b in today), 1), "last": round(last, 1),
                            "vwap": round(vw, 1), "bars": len(today),
                            "from_open_pct": round(100 * (last / o - 1), 3),
                            "last_hour_pct": round(100 * (last / lh[0].open - 1), 3) if lh else None,
                            "minutes_to_close": max(0, 16 * 60 - (now.hour * 60 + now.minute))}
            if prev:
                ctx["prev_close"] = round(prev[-1].close, 1)
                ctx["gap_pct"] = round(100 * (o / prev[-1].close - 1), 3)
        if getattr(orch, "alpaca_trader", None) is not None and today:
            from .nbbo_chain import fetch_chain_nbbo
            spot_spy = today[-1].close / 10.0
            nb = await fetch_chain_nbbo(orch.alpaca_trader, spot_spy, now.strftime("%y%m%d"))
            puts = {r["strike"]: r for r in nb.get("puts") or []}
            calls = {r["strike"]: r for r in nb.get("calls") or []}
            atm = float(round(spot_spy))
            strad = None
            if atm in puts and atm in calls:
                strad = round(puts[atm]["mid"] + calls[atm]["mid"], 2)
            surf = {}
            for pct in (0.2, 0.3, 0.5):
                kp = float(round(spot_spy * (1 - pct / 100))); kc = float(round(spot_spy * (1 + pct / 100)))
                from .config import settings as _settings
                w = float(_settings.SPY_WING_DOLLARS)
                sp, lp = puts.get(kp), puts.get(kp - w); sc, lc = calls.get(kc), calls.get(kc + w)
                surf[f"{pct}%"] = {"put": {"short": kp, "bid": sp["bid"] if sp else None,
                                           "spread_credit": round(sp["bid"] - lp["ask"], 2) if (sp and lp) else None},
                                   "call": {"short": kc, "bid": sc["bid"] if sc else None,
                                            "spread_credit": round(sc["bid"] - lc["ask"], 2) if (sc and lc) else None}}
            ctx["options_0dte"] = {"spot_spy": round(spot_spy, 2), "atm_straddle_mid": strad,
                                   "implied_move_pct_to_close": round(100 * strad / spot_spy, 3) if strad else None,
                                   "spread_width": w, "surface": surf}
    except Exception as e:  # noqa: BLE001
        ctx["context_note"] = f"live surface unavailable: {e}"
    return ctx


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
        from datetime import datetime as _dt
        from .orchestrator import ET as _ET  # type: ignore
        _today = _dt.now(_ET).date()
        evs = []
        for e in (orch.macro._calendar or []):
            if (e.get("impact") or "").lower() not in ("high", "medium"):
                continue
            t = orch.macro._parse_event_time(e.get("time") or "")
            if t is not None and t.date() == _today:
                evs.append({**e, "time_et": t.strftime("%H:%M")})
        evs = evs[:8]
        ctx["macro_events_today"] = [
            {"event": e.get("event"), "time_et": e.get("time_et"), "impact": e.get("impact")}
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
        "max_tokens": 1200,
        # no `temperature`: the Claude 5 models reject it (HTTP 400 "deprecated for this model", 2026-10-10)
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
            if r.status_code >= 400:
                log.warning("claude_scan HTTP %s: %s", r.status_code, r.text[:300])
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


async def run_scan_cli(context: dict, model: str, bin_path: str,
                       timeout: float = 60.0) -> dict | None:
    """Fallback transport: the authenticated Claude Code CLI in headless print mode
    (MEICZero's claude_analyst pattern) — rides the existing subscription, NO API
    key needed. Trade-off vs the API: no temperature control (tag the transport in
    the record so scoring can distinguish). Fail-soft like everything else."""
    import asyncio
    prompt = (SYSTEM + "\n\nPre-market context (JSON):\n" + json.dumps(context) +
              "\n\nReturn the verdict JSON now.")
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, "-p", prompt, "--model", model, "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        if proc.returncode != 0:
            log.warning("claude_scan CLI rc=%s (%s)", proc.returncode, (err or b"")[:120])
            return None
        text = json.loads(out.decode()).get("result") or ""
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            log.warning("claude_scan CLI: no JSON in response (%.120s)", text)
            return None
        verdict = json.loads(m.group(0))
        verdict["_model"] = model
        return verdict
    except asyncio.TimeoutError:
        try:
            if proc:
                proc.kill()
        except Exception:  # noqa: BLE001
            pass
        log.warning("claude_scan CLI: timeout after %ss", timeout)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("claude_scan CLI transport failed: %s", e)
        return None


def append_scan(rec: dict) -> None:
    """One JSON line per session — the dataset the advisor gets SCORED on."""
    try:
        os.makedirs(os.path.dirname(SCAN_PATH), exist_ok=True)
        with open(SCAN_PATH, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:  # noqa: BLE001
        log.debug("claude_scan append failed: %s", e)
