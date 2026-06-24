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

    print("=== Broker-truth vs model reconciliation (option fills + fees) ===")
    print(f"{'date':12} {'broker $':>9} {'fees':>6} {'net $':>8} {'model $':>9} {'gap':>8}  {'fills':>5}  detail")
    print("  " + "-" * 86)
    tb = tm = tf = 0.0
    for d in days:
        b = broker.get(d)
        m = model.get(d)
        bval = b["realized"] if b else None
        fval = b.get("fees", 0.0) if b else None
        nval = b.get("realized_net", bval) if b else None   # fills + fees = true net cash
        mval = m["total"] if m else None
        gap = (nval - mval) if (nval is not None and mval is not None) else None
        tb += bval or 0.0
        tf += fval or 0.0
        tm += mval or 0.0
        detail = f"ic={m['ic']} wave={m['wave']}" if m else ""
        print(f"{d:12} {('—' if bval is None else f'{bval:+.0f}'):>9} "
              f"{('—' if not fval else f'{fval:+.2f}'):>6} "
              f"{('—' if nval is None else f'{nval:+.0f}'):>8} "
              f"{('—' if mval is None else f'{mval:+.0f}'):>9} "
              f"{('—' if gap is None else f'{gap:+.0f}'):>8}  "
              f"{(b['fills'] if b else 0):>5}  {detail}")
    print("  " + "-" * 86)
    print(f"{'TOTAL':12} {tb:>+9.0f} {tf:>+6.2f} {tb+tf:>+8.0f} {tm:>+9.0f} {(tb+tf)-tm:>+8.0f}")
    print("\n  broker = net of real SPY/XSP/SPX option fills (CasaaFinance equities excluded)")
    print("  net    = broker + fees (OCC/ORF/TAF/CAT) = TRUE net cash — the canonical number")
    print("  model  = ic_real_net + wave_pnl from debrief_log.jsonl · gap = net − model")


if __name__ == "__main__":
    asyncio.run(main())
