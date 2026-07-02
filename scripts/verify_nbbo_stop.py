"""Verify the NBBO breakeven-stop mark (fix for the Jun-30 phantom stops).

Unit-tests `Orchestrator._ic_buyback_nbbo` in isolation with stubbed quotes:
  1. PHANTOM (tonight's condor 1): fresh NBBO mids ≈ real close → buyback well
     BELOW threshold → the stop would NOT have fired (the whole point).
  2. BREACH: short put deep ITM → buyback ABOVE threshold → stop fires.
  3. SCALE: buyback is SPX-scale (×10 of the SPY spread cost).
  4/5/6. FAIL-SAFE: a missing leg / one-sided (ask≤0) / stale quote → None
     (caller HOLDS; never closes on a bad mark).

Run:  PYTHONPATH=. .venv/bin/python scripts/verify_nbbo_stop.py
"""
import asyncio
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

from backend.app.orchestrator import Orchestrator
from backend.app.alpaca_trader import AlpacaTrader
from backend.app.config import settings

FRESH = datetime.now(timezone.utc).isoformat()
STALE = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()

# Tonight's condor 1: SPY 743/740 put spread, 750/752 call spread → SPX ×10.
IC = SimpleNamespace(
    call_leg=SimpleNamespace(instrument="SPX", short_strike=7500.0, long_strike=7520.0),
    put_leg=SimpleNamespace(instrument="SPX", short_strike=7430.0, long_strike=7400.0),
)
REAL_CREDIT = 640.0                                   # SPX-scale (SPY $64 ×10)
THRESHOLD = REAL_CREDIT * settings.IC_STOP_BUFFER     # 640 × 1.05 = 672


def trader(quotes_by_role, drop=(), ts=FRESH):
    """Stub trader: maps the 4 requested symbols to quotes by position
    (sc, lc, sp, lp), so we don't need to reconstruct the OCC strings."""
    roles = ["sc", "lc", "sp", "lp"]

    class T:
        _occ_symbol = staticmethod(AlpacaTrader._occ_symbol)

        async def get_option_quotes(self, symbols):
            out = {}
            for sym, role in zip(symbols, roles):
                if role in drop:
                    continue
                b, a = quotes_by_role[role]
                out[sym] = {"bid": b, "ask": a, "ts": ts}
            return out
    return T()


def orch(t):
    o = Orchestrator.__new__(Orchestrator)   # bypass heavy __init__
    o.alpaca_trader = t
    return o


# (bid, ask) per leg
PHANTOM = {"sc": (0.05, 0.07), "lc": (0.01, 0.03), "sp": (0.16, 0.20), "lp": (0.05, 0.09)}
BREACH  = {"sc": (0.00, 0.02), "lc": (0.00, 0.01), "sp": (2.10, 2.30), "lp": (0.50, 0.70)}


async def main():
    fails = []

    # 1 + 3: phantom → below threshold, and SPX-scale (×10)
    r = await orch(trader(PHANTOM))._ic_buyback_nbbo(IC)
    assert r is not None, "phantom returned None"
    buyback, call_c, put_c, src = r
    # SPY: call (0.06−0.02)=0.04→$4, put (0.18−0.07)=0.11→$11 = $15 ×10 = $150 SPX
    ok1 = abs(buyback - 150.0) < 1.0 and buyback < THRESHOLD and src == "nbbo"
    print(f"1. PHANTOM  buyback=${buyback:.0f} (want ~150, < ${THRESHOLD:.0f})  "
          f"call=${call_c:.0f} put=${put_c:.0f}  → {'NO stop ✅' if ok1 else 'FAIL ❌'}")
    if not ok1:
        fails.append("phantom")

    # 2: breach → above threshold
    r = await orch(trader(BREACH))._ic_buyback_nbbo(IC)
    buyback2 = r[0]
    ok2 = buyback2 > THRESHOLD
    print(f"2. BREACH   buyback=${buyback2:.0f} (want > ${THRESHOLD:.0f})            "
          f"→ {'STOP fires ✅' if ok2 else 'FAIL ❌'}")
    if not ok2:
        fails.append("breach")

    # 4: missing leg → None
    r = await orch(trader(PHANTOM, drop=("sp",)))._ic_buyback_nbbo(IC)
    ok4 = r is None
    print(f"4. MISSING leg quote        → {'HOLD (None) ✅' if ok4 else 'FAIL ❌'}")
    if not ok4:
        fails.append("missing")

    # 5: one-sided (ask ≤ 0) → None
    r = await orch(trader({**PHANTOM, "sp": (0.16, 0.0)}))._ic_buyback_nbbo(IC)
    ok5 = r is None
    print(f"5. ONE-SIDED (ask=0)        → {'HOLD (None) ✅' if ok5 else 'FAIL ❌'}")
    if not ok5:
        fails.append("one-sided")

    # 6: stale quote → None
    r = await orch(trader(PHANTOM, ts=STALE))._ic_buyback_nbbo(IC)
    ok6 = r is None
    print(f"6. STALE quote (2h old)     → {'HOLD (None) ✅' if ok6 else 'FAIL ❌'}")
    if not ok6:
        fails.append("stale")

    print()
    if fails:
        print(f"❌ FAILED: {', '.join(fails)}")
        raise SystemExit(1)
    print("✅ ALL PASS — NBBO mark is scale-correct and fail-safe. "
          "Tonight's condor 1 would NOT have phantom-stopped.")


if __name__ == "__main__":
    asyncio.run(main())
