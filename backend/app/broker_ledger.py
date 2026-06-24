"""Broker-truth ledger — the canonical realized P&L, computed from REAL Alpaca
fills, not model estimates.

This is the reconciliation layer the system was missing: every other P&L number
(debrief, dashboard, improve_loop gates, the daily-loss kill-switch) is computed
from `trade.pnl`, which is a Black-Scholes / estimated-credit MODEL value. Models
drift — on 2026-06-23 the model reported the Wave book two contradictory ways
(+$180 vs +$65), and neither matched the broker. Fills don't drift. This module
reads them.

SAFETY: the Alpaca paper account is SHARED with CasaaFinance (equities). We filter
to OPTION OCC symbols ONLY, so CasaaFinance's equity fills are never counted. This
module is strictly READ-ONLY — it fetches activities and nets cashflow, nothing else.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")

# OCC option symbol: ROOT(1-6 alnum) + YYMMDD + C/P + strike×1000 (8 digits).
# e.g. SPY260623P00734600. Matches ZeroDTE's instruments; rejects bare equities (AMD).
_OCC_RE = re.compile(r"^[A-Z]{1,6}\d{6}[CP]\d{8}$")

# OCC underlying root we trade (SPY at 1/10 SPX scale). Guards against ever summing
# a non-SPY option that might appear on the shared account.
_OUR_ROOTS = ("SPY", "XSP", "SPX")


def is_our_option(symbol: str) -> bool:
    """True iff `symbol` is one of OUR option OCC symbols (never an equity)."""
    if not symbol or not _OCC_RE.match(symbol):
        return False
    root = re.match(r"^[A-Z]{1,6}", symbol).group(0)
    # strike×1000 is the last 8 chars; the root is everything before YYMMDD+C/P
    root = symbol[: len(symbol) - 15]
    return root in _OUR_ROOTS


def _fill_cashflow(fill: dict, multiplier: int = 100) -> float | None:
    """Signed $ cashflow of ONE option fill: +received when we SOLD, −paid when we
    BOUGHT. None if the fill can't be parsed."""
    try:
        price = float(fill.get("price"))
        qty = abs(float(fill.get("qty")))
    except (TypeError, ValueError):
        return None
    if qty <= 0:
        return None
    side = (fill.get("side") or "").lower()
    # Alpaca option fills use three side tags: 'sell_short' (open a short leg),
    # 'sell' (sell-to-close a long leg) → both cash IN (+); 'buy' covers both
    # buy-to-open (long wing) and buy-to-close (cover a short) → cash OUT (−).
    if side.startswith("sell"):
        sign = 1.0
    elif side.startswith("buy"):
        sign = -1.0
    else:
        return None
    return sign * price * qty * multiplier


def _activity_day(a: dict) -> str | None:
    """ET trading-day for an activity. FILLs carry `transaction_time`; non-trade
    activities (FEE) carry `date`. Returns 'YYYY-MM-DD' or None."""
    t = a.get("transaction_time") or a.get("date") or ""
    try:
        if "T" in t or "Z" in t or "+" in t:
            return datetime.fromisoformat(t.replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
        return t[:10] or None       # bare 'YYYY-MM-DD'
    except (ValueError, TypeError):
        return None


def realized_by_day(fills: list[dict], fees: list[dict] | None = None) -> dict:
    """Net realized $ per ET trading day, from OUR option fills (+ fees if given).

    A day's realized P&L = sum of signed cashflow across all our option fills that
    day (sells positive, buys negative). For a same-day-expiry book where every
    position opens and closes (or expires) the same session, this equals true
    realized P&L. Expiry of a short with no buy-to-close fill correctly contributes
    only its opening credit (you keep it).

    `realized` is FILL-only (back-compat). `fees` is the summed FEE net_amount
    (OCC/ORF/TAF/CAT — always ≤0, and they POST a day late, so they attach to the
    posting day, not the trade day). `realized_net` = realized + fees is the true
    net cash and is what the kill-switch + reconciler should read.

    Returns {date_str: {realized, fees, realized_net, fills, symbols}}.
    """
    by_day: dict[str, dict] = defaultdict(
        lambda: {"realized": 0.0, "fees": 0.0, "fills": 0, "_syms": set()})
    skipped_non_option = 0
    for f in fills:
        sym = f.get("symbol", "")
        if not is_our_option(sym):
            skipped_non_option += 1
            continue
        cf = _fill_cashflow(f)
        if cf is None:
            continue
        d = _activity_day(f)
        if d is None:
            continue
        rec = by_day[d]
        rec["realized"] += cf
        rec["fills"] += 1
        rec["_syms"].add(sym)
    for fee in (fees or []):
        try:
            amt = float(fee.get("net_amount"))
        except (TypeError, ValueError):
            continue
        d = _activity_day(fee)
        if d is None:
            continue
        by_day[d]["fees"] += amt
    if skipped_non_option:
        log.info("broker_ledger: skipped %d non-option fills (CasaaFinance equities)", skipped_non_option)
    return {d: {"realized": round(v["realized"], 2), "fees": round(v["fees"], 2),
                "realized_net": round(v["realized"] + v["fees"], 2),
                "fills": v["fills"], "symbols": len(v["_syms"])}
            for d, v in sorted(by_day.items())}


async def fetch_realized(trader, days_back: int = 7, now: datetime | None = None) -> dict:
    """Pull our option fills + fees for the last `days_back` ET days and net them
    per day. `trader` is an AlpacaTrader. Read-only. Returns realized_by_day()."""
    now = now or datetime.now(ET)
    after = (now - timedelta(days=days_back)).astimezone(ET).replace(hour=0, minute=0, second=0)
    until = now
    fills = await trader.get_fill_activities(after.isoformat(), until.isoformat())
    fees = await trader.get_fee_activities(after.isoformat(), until.isoformat())
    return realized_by_day(fills, fees)
