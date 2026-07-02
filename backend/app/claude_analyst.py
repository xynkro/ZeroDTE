"""Claude SHADOW analyst — live market reads that influence NOTHING (yet).

THE CONTRACT (DECISION.md discipline):
  - Claude is NOT in the trade loop. The engine's hard rule stands: entries,
    stops, and sizing stay deterministic. This module runs AFTER each slot
    decision is already made, fire-and-forget, and logs a structured read
    next to what the machine did.
  - Every read lands in backend/data/claude_shadow.jsonl. After ~20 clean
    nights, scripts/score_claude_shadow.py joins reads to broker-truth
    outcomes; ONLY a read that proves predictive earns a live gate, through
    the improve-loop like any other signal.
  - Fail-soft: no key, CLI missing, timeout, bad JSON → None + one log line.
    A Claude failure can never delay, block, or alter a trade.

Transport: the authenticated Claude Code CLI in headless print mode
(`claude -p … --output-format json`), so no API key is required on this Mac.
If ANTHROPIC_API_KEY ever lands in .env, the direct API would be the upgrade.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

log = logging.getLogger(__name__)

SHADOW_PATH = Path(__file__).resolve().parents[1] / "data" / "claude_shadow.jsonl"

_SYSTEM = (
    "You are a 0DTE SPX/SPY index-options tape analyst. Given one snapshot, "
    "assess the environment for SELLING same-day-expiry ~16-delta iron condors "
    "(profit if the index stays range-bound into 16:00 ET). "
    "Respond with STRICT JSON only — no prose, no markdown fences: "
    '{"condor_env":"friendly"|"neutral"|"hostile",'
    '"threat_side":"call"|"put"|"both"|"none",'
    '"confidence":<0.0-1.0>,'
    '"one_line":"<=120 chars rationale"}'
)


def gather_context(orch, bar, slot: str) -> dict:
    """Snapshot of what the machine can see — all fields best-effort."""
    ctx: dict = {"slot_et": slot,
                 "ts_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    try:
        ctx["spx_spot"] = round(bar.close, 2)
        ctx["spy_spot"] = round(bar.close * 0.1, 2)
    except Exception:
        pass
    try:
        rg = orch.state.regime
        ctx["regime"] = rg.regime
        ctx["obs_drift_pct"] = rg.obs_drift_pct
        ctx["proj_high"] = rg.proj_high
        ctx["proj_low"] = rg.proj_low
    except Exception:
        pass
    try:
        from .vix_gate import check_iv_safe
        _, vix, src = check_iv_safe(threshold=999.0)
        if vix:
            ctx["vix"] = round(vix, 2)
    except Exception:
        pass
    try:
        g = orch.state.gex or {}
        ctx["gex_regime"] = g.get("regime")
        ctx["gex_flip"] = g.get("flip_point") or g.get("flip")
    except Exception:
        pass
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        opens = [b for b in orch.state.iron_condor_history
                 if b.build_id and b.build_id.startswith(f"ic_{today}")
                 and b.broker_status == "submitted"]
        ctx["open_condors"] = [
            {"call": (b.call_leg.short_strike if b.call_leg else None),
             "put": (b.put_leg.short_strike if b.put_leg else None)} for b in opens]
    except Exception:
        pass
    return ctx


async def _run_claude(prompt: str) -> str | None:
    """Headless CLI call. Returns the model's raw text or None."""
    bin_path = settings.CLAUDE_SHADOW_BIN
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, "-p", prompt,
            "--model", settings.CLAUDE_SHADOW_MODEL,
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            proc.communicate(), timeout=settings.CLAUDE_SHADOW_TIMEOUT_SEC)
        if proc.returncode != 0:
            log.info("claude shadow: CLI rc=%s (%s)", proc.returncode,
                     (err or b"")[:120])
            return None
        env = json.loads(out.decode())
        return env.get("result")
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        log.info("claude shadow: timeout after %ss", settings.CLAUDE_SHADOW_TIMEOUT_SEC)
        return None
    except Exception as e:  # noqa: BLE001
        log.info("claude shadow: transport failed (%s)", e)
        return None


def _parse_read(raw: str | None) -> dict | None:
    if not raw:
        return None
    txt = raw.strip()
    if txt.startswith("```"):
        txt = txt.strip("`")
        txt = txt[txt.find("{"):]
    try:
        start, end = txt.index("{"), txt.rindex("}") + 1
        d = json.loads(txt[start:end])
    except (ValueError, json.JSONDecodeError):
        return None
    if d.get("condor_env") not in ("friendly", "neutral", "hostile"):
        return None
    if d.get("threat_side") not in ("call", "put", "both", "none"):
        d["threat_side"] = "none"
    try:
        d["confidence"] = max(0.0, min(1.0, float(d.get("confidence", 0.0))))
    except (TypeError, ValueError):
        d["confidence"] = 0.0
    d["one_line"] = str(d.get("one_line", ""))[:160]
    return {k: d[k] for k in ("condor_env", "threat_side", "confidence", "one_line")}


def _log_row(row: dict) -> None:
    try:
        SHADOW_PATH.parent.mkdir(parents=True, exist_ok=True)
        with SHADOW_PATH.open("a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception as e:  # noqa: BLE001
        log.info("claude shadow: log write failed (%s)", e)


async def shadow_read_task(orch, bar, slot: str, engine_action: str,
                           engine_detail: str = "") -> None:
    """Fire-and-forget per-slot read. Never raises; never touches trading."""
    try:
        ctx = gather_context(orch, bar, slot)
        prompt = (_SYSTEM + "\n\nSNAPSHOT:\n"
                  + json.dumps(ctx, default=str)
                  + f"\n\nENGINE ACTION (already taken, do not second-guess): "
                    f"{engine_action} {engine_detail}".strip())
        read = _parse_read(await _run_claude(prompt))
        row = {"date": datetime.now().strftime("%Y-%m-%d"), "slot": slot,
               "ctx": ctx, "engine_action": engine_action,
               "engine_detail": engine_detail[:160], "read": read}
        _log_row(row)
        if read:
            log.info("🔮 claude shadow [%s]: %s (threat=%s conf=%.2f) — %s | engine did: %s",
                     slot, read["condor_env"], read["threat_side"],
                     read["confidence"], read["one_line"], engine_action)
        else:
            log.info("claude shadow [%s]: no read (transport/parse) — engine unaffected", slot)
    except Exception as e:  # noqa: BLE001
        log.info("claude shadow task failed harmlessly: %s", e)
