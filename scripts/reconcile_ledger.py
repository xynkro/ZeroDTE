#!/usr/bin/env python3
"""Reconcile MODEL P&L (what the debrief/log/kill-switch use) against BROKER TRUTH
(real Alpaca option fills). Read-only.

For each ET day it shows:
  • broker realized  — net of every one of OUR option fills that day (SPY/XSP/SPX
    OCC only; CasaaFinance equities filtered out)
  • model total      — ic_real_net + wave_pnl from debrief_log.jsonl
  • gap              — broker − model. Non-zero = the system's reported P&L is NOT
    what the broker actually did (the integrity hole found on 2026-06-23).

Run: PYTHONPATH=. .venv/bin/python scripts/reconcile_ledger.py
"""
from __future__ import annotations

import asyncio
import json

import backend.app.config  # noqa: F401 — load .env
from backend.app.alpaca_trader import AlpacaTrader
from backend.app import broker_ledger as bl


def _model_by_day(path: str = "backend/data/debrief_log.jsonl") -> dict:
    out: dict[str, dict] = {}
    try:
        for line in open(path):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError:
                continue
            d = r.get("date")
            if not d:
                continue
            ic = r.get("ic_real_net")
            wv = r.get("wave_pnl")
            out[d] = {
                "ic": ic, "wave": wv,
                "total": (ic or 0.0) + (wv or 0.0),
            }
    except OSError:
        pass
    return out


async def main():
    trader = AlpacaTrader()
    try:
        broker = await bl.fetch_realized(trader, days_back=10)
    finally:
        await trader.close()
    model = _model_by_day()

    days = sorted(set(broker) | set(model))
    if not days:
        print("No data (no broker fills and no debrief_log rows).")
        return

    print("=== Broker-truth vs model reconciliation (option fills only) ===")
    print(f"{'date':12} {'broker $':>10} {'model $':>10} {'gap':>9}  {'fills':>5}  detail")
    print("  " + "-" * 78)
    tb = tm = 0.0
    for d in days:
        b = broker.get(d)
        m = model.get(d)
        bval = b["realized"] if b else None
        mval = m["total"] if m else None
        gap = (bval - mval) if (bval is not None and mval is not None) else None
        tb += bval or 0.0
        tm += mval or 0.0
        detail = ""
        if m:
            detail = f"ic={m['ic']} wave={m['wave']}"
        print(f"{d:12} {('—' if bval is None else f'{bval:+.0f}'):>10} "
              f"{('—' if mval is None else f'{mval:+.0f}'):>10} "
              f"{('—' if gap is None else f'{gap:+.0f}'):>9}  "
              f"{(b['fills'] if b else 0):>5}  {detail}")
    print("  " + "-" * 78)
    print(f"{'TOTAL':12} {tb:>+10.0f} {tm:>+10.0f} {tb-tm:>+9.0f}")
    print("\n  broker = net of real SPY/XSP/SPX option fills (CasaaFinance equities excluded)")
    print("  model  = ic_real_net + wave_pnl from debrief_log.jsonl")
    print("  Any non-zero gap = reported P&L diverges from what the broker actually did.")


if __name__ == "__main__":
    asyncio.run(main())
