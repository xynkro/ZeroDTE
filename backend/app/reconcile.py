"""Ledger ⇄ Alpaca reconciliation.

The council's data-integrity guardrail: a recorded "trade" that the broker never
actually saw silently poisons the validation sample. This confirms every recorded
directional-spread trade that CLAIMS it reached the broker has a matching Alpaca
order. Anything that doesn't match is an ORPHAN — a data-integrity flag.

Run on-demand via GET /api/reconcile, and once a day appended to the EOD summary.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# broker_status values that ASSERT the trade actually reached Alpaca
_REACHED_BROKER = {"submitted", "filled", "closed"}


async def reconcile(orch) -> dict:
    """Compare the in-memory ledger against Alpaca's recent orders.

    Returns {ok, checked, matched, orphans[], alpaca_orders, note}. `ok` is True
    when every recorded-as-executed trade matches a real Alpaca order.
    """
    ds = [t for t in orch.paper_trades if getattr(t, "strategy", None) == "directional_spread"]
    claimed = [t for t in ds if t.broker_status in _REACHED_BROKER]

    trader = getattr(orch, "alpaca_trader", None)
    if trader is None:
        return {"ok": None, "reason": "alpaca_trader not initialized",
                "checked": len(claimed), "matched": 0, "orphans": [], "alpaca_orders": 0}
    try:
        orders = await trader.get_orders(status="all")  # recent (<=50) orders
    except Exception as e:  # noqa: BLE001
        return {"ok": None, "reason": f"alpaca fetch failed: {e}",
                "checked": len(claimed), "matched": 0, "orphans": [], "alpaca_orders": 0}

    ids = {str(o.get("id")) for o in orders if o.get("id")}
    matched, orphans = [], []
    for t in claimed:
        oid = getattr(t, "alpaca_order_id", None)
        if oid and str(oid) in ids:
            matched.append(t.trade_no)
        else:
            orphans.append({"trade_no": t.trade_no, "alpaca_order_id": oid,
                            "broker_status": t.broker_status})
    return {
        "ok": len(orphans) == 0,
        "checked": len(claimed),
        "matched": len(matched),
        "orphans": orphans,
        "alpaca_orders": len(orders),
        "note": "orphan = a trade recorded as executed with no matching Alpaca order "
                "(note: Alpaca order list is the most-recent 50, so very old trades may not match)",
    }


def summarize(rc: dict) -> str:
    """One-line human/Telegram summary of a reconcile result."""
    if rc.get("ok") is None:
        return f"reconcile: skipped ({rc.get('reason', 'unavailable')})"
    if not rc.get("checked"):
        return "reconcile: no executed trades to check"
    if rc.get("orphans"):
        nums = ", #".join(str(o["trade_no"]) for o in rc["orphans"])
        return (f"⚠️ reconcile: {len(rc['orphans'])}/{rc['checked']} recorded trades have NO "
                f"matching Alpaca order (#{nums}) — data-integrity flag")
    return f"✓ reconcile: all {rc['matched']} recorded trades match the broker"
