"""NBBO entry plane for MEIC — executable-quote chain + smile-consistent deltas.

WHY THIS EXISTS: the CBOE delayed-mid chain systematically mispriced entries —
on 2026-07-01 the model credit ran 2.6–4.6× BELOW the real fill, and the '16Δ'
picker sold near-ATM strikes into a trending tape (13:00 condor collected 47%
of wing = nowhere near 16Δ). That one split data plane poisoned everything
downstream: strike placement, the min-credit gate, the stop anchor, slippage
telemetry. This module prices the SAME SPY strikes we actually trade off the
SAME feed that fills them (Alpaca NBBO), with PER-STRIKE implied vol so deltas
respect the smile.

Conventions:
  - Works natively in SPY (integer $1 strikes, the instrument we submit).
  - Returns SPX-scale (×10 strikes, ×1000 per-share→$ credits) in the exact
    shape of gex.pick_iron_condor, so the builder consumes either source.
  - Fail-closed: no fresh two-sided quote → strike dropped; either side
    unpickable → ok=False and the caller falls back / skips loudly.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import bs_pricing as bs

log = logging.getLogger(__name__)


def _fresh(ts: str | None, now_ts: float, max_stale_sec: float) -> bool:
    if not ts:
        return False
    try:
        qt = datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return False
    return (now_ts - qt) <= max_stale_sec


async def fetch_chain_nbbo(trader, spot_spy: float, expiry_yymmdd: str,
                           span_pct: float = 0.035,
                           max_stale_sec: float = 300.0) -> dict:
    """Pull a fresh two-sided NBBO chain around spot for today's SPY expiry.

    Returns {"calls": [{strike,bid,ask,mid}...], "puts": [...]} — only strikes
    with a fresh, two-sided quote survive. One batched quotes call (~2·N syms).
    """
    lo = int(spot_spy * (1.0 - span_pct))
    hi = int(round(spot_spy * (1.0 + span_pct))) + 1
    strikes = list(range(lo, hi + 1))
    syms: dict[str, tuple[str, int]] = {}
    for k in strikes:
        syms[trader._occ_symbol("SPY", expiry_yymmdd, "call", float(k))] = ("calls", k)
        syms[trader._occ_symbol("SPY", expiry_yymmdd, "put", float(k))] = ("puts", k)
    quotes = await trader.get_option_quotes(list(syms))
    now_ts = datetime.now(timezone.utc).timestamp()
    out: dict = {"calls": [], "puts": []}
    for sym, (side, k) in syms.items():
        q = quotes.get(sym)
        if not q:
            continue
        bid, ask = q.get("bid"), q.get("ask")
        if bid is None or ask is None or ask <= 0 or bid < 0 or ask < bid:
            continue
        if not _fresh(q.get("ts"), now_ts, max_stale_sec):
            continue
        mid = (bid + ask) / 2.0
        if mid <= 0:
            continue
        out[side].append({"strike": float(k), "bid": bid, "ask": ask, "mid": mid})
    out["calls"].sort(key=lambda r: r["strike"])
    out["puts"].sort(key=lambda r: r["strike"])
    return out


def _pick_side(rows: list[dict], spot: float, target_delta: float,
               wing_spy: float, is_call: bool,
               exclude: set[float]) -> dict | None:
    """Pick short strike nearest |delta|==target with a quoted, unblocked wing.

    Per-strike tv from each option's own mid (smile-consistent). Candidates
    missing a wing quote or colliding with an open MEIC leg are skipped at
    SELECTION time — structurally better than nudging at submit."""
    by_strike = {r["strike"]: r for r in rows}
    best = None
    for r in rows:
        k = r["strike"]
        if k in exclude:
            continue
        if is_call and k <= spot:
            continue
        if not is_call and k >= spot:
            continue
        tv = bs.implied_tv(r["mid"], spot, k, is_call)
        if tv is None:
            continue
        delta = bs.call_delta(spot, k, tv) if is_call else abs(bs.put_delta(spot, k, tv))
        if delta > 0.30:      # never sell near-ATM no matter what the chain says
            continue
        # Hard OTM floor: wide/poisoned afternoon quotes can invert to garbage
        # IV and make a near-money strike LOOK like target delta (2026-07-02
        # 14:00 sold a 744C with spot 743.4 — 0.08% OTM — as '16Δ'; it finished
        # ITM at the bell and only the assignment guard saved it). Distance is
        # quote-independent: shorts must sit at least MIN_OTM_PCT away.
        otm_pct = (k - spot) / spot if is_call else (spot - k) / spot
        if otm_pct < 0.004:   # 0.4% ≈ 3 SPY pts — well outside one-bell noise
            continue
        long_k = float(round(k + wing_spy)) if is_call else float(round(k - wing_spy))
        lr = by_strike.get(long_k)
        if lr is None or long_k in exclude:
            # try ±$1 to find a quoted, unblocked wing before giving up
            alt = long_k + (1.0 if is_call else -1.0)
            lr = by_strike.get(alt)
            if lr is None or alt in exclude:
                continue
            long_k = alt
        credit_mid = r["mid"] - lr["mid"]
        credit_cons = r["bid"] - lr["ask"]     # worst-case executable
        if credit_mid <= 0:
            continue
        score = abs(delta - target_delta)
        if best is None or score < best["score"]:
            best = {"score": score, "short": k, "long": long_k, "delta": delta,
                    "credit_mid": credit_mid, "credit_cons": credit_cons, "tv": tv}
    return best


def pick_iron_condor_nbbo(chain: dict, spot_spy: float, target_delta: float,
                          wing_spx: float, expiry_yymmdd: str,
                          exclude_spy_strikes: set[float] | None = None,
                          min_side_credit_usd: float = 0.0,
                          allow_one_sided: bool = False) -> dict:
    """Per-side picker. Output mirrors gex.pick_iron_condor (SPX-scale) with
    extras: *_credit_cons_usd (bid/ask-conservative credits), source='nbbo',
    sides ('both'|'call_only'|'put_only'), dropped (why a side was cut).

    MEIC canon: a side whose CONSERVATIVE credit can't clear
    min_side_credit_usd doesn't trade. With allow_one_sided the surviving side
    trades alone as a plain credit spread; otherwise both must pass."""
    exclude = exclude_spy_strikes or set()
    wing_spy = wing_spx / 10.0
    call = _pick_side(chain.get("calls", []), spot_spy, target_delta, wing_spy,
                      True, exclude)
    put = _pick_side(chain.get("puts", []), spot_spy, target_delta, wing_spy,
                     False, exclude)
    dropped: list[str] = []
    if call and min_side_credit_usd > 0 and call["credit_cons"] * 1000.0 < min_side_credit_usd:
        dropped.append(f"call ${call['credit_cons'] * 1000.0:.0f}<{min_side_credit_usd:.0f}")
        call = None
    if put and min_side_credit_usd > 0 and put["credit_cons"] * 1000.0 < min_side_credit_usd:
        dropped.append(f"put ${put['credit_cons'] * 1000.0:.0f}<{min_side_credit_usd:.0f}")
        put = None
    if call and put:
        sides = "both"
    elif allow_one_sided and (call or put):
        sides = "call_only" if call else "put_only"
    else:
        return {"ok": False, "dropped": dropped,
                "error": f"no tradable sides (call={'ok' if call else 'cut'}, "
                         f"put={'ok' if put else 'cut'}; {'; '.join(dropped) or 'unpickable'}; "
                         f"chain {len(chain.get('calls', []))}C/{len(chain.get('puts', []))}P)"}
    if sides != "both":
        side = call or put
        usd = side["credit_mid"] * 1000.0
        wing = abs(side["long"] - side["short"]) * 10.0
        out = {
            "ok": True, "source": "nbbo", "sides": sides, "dropped": dropped,
            "spot": spot_spy * 10.0, "expiry": expiry_yymmdd,
            "total_credit_usd": round(usd, 2),
            "max_loss_usd": round(wing * 100.0 - usd, 2),
            "credit_pct_of_wing": round(usd / (wing * 100.0) * 100.0, 1) if wing else 0.0,
        }
        if call:
            out.update({"short_call": call["short"] * 10.0, "long_call": call["long"] * 10.0,
                        "short_call_delta": round(call["delta"], 4),
                        "call_credit_usd": round(usd, 2),
                        "call_credit_cons_usd": round(call["credit_cons"] * 1000.0, 2),
                        "call_wing": wing})
        else:
            out.update({"short_put": put["short"] * 10.0, "long_put": put["long"] * 10.0,
                        "short_put_delta": round(-put["delta"], 4),
                        "put_credit_usd": round(usd, 2),
                        "put_credit_cons_usd": round(put["credit_cons"] * 1000.0, 2),
                        "put_wing": wing})
        return out
    # SPY per-share → SPX-scale contract $ (×100 multiplier ×10 scale)
    call_usd = call["credit_mid"] * 1000.0
    put_usd = put["credit_mid"] * 1000.0
    total = call_usd + put_usd
    call_wing = abs(call["long"] - call["short"]) * 10.0   # SPX $ per share
    put_wing = abs(put["short"] - put["long"]) * 10.0
    max_wing_usd = max(call_wing, put_wing) * 100.0
    return {
        "ok": True, "source": "nbbo", "sides": "both", "dropped": dropped,
        "spot": spot_spy * 10.0, "expiry": expiry_yymmdd,
        "short_call": call["short"] * 10.0, "long_call": call["long"] * 10.0,
        "short_put": put["short"] * 10.0, "long_put": put["long"] * 10.0,
        "short_call_delta": round(call["delta"], 4),
        "short_put_delta": round(-put["delta"], 4),
        "call_credit_usd": round(call_usd, 2), "put_credit_usd": round(put_usd, 2),
        "total_credit_usd": round(total, 2),
        "call_credit_cons_usd": round(call["credit_cons"] * 1000.0, 2),
        "put_credit_cons_usd": round(put["credit_cons"] * 1000.0, 2),
        "call_wing": call_wing, "put_wing": put_wing,
        "max_loss_usd": round(max_wing_usd - total, 2),
        "credit_pct_of_wing": round(total / max_wing_usd * 100.0, 1) if max_wing_usd else 0.0,
    }
