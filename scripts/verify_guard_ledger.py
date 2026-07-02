"""Verify the assignment guard + slot ledger (no network, no real orders).

Guard: fires ONLY in the pre-close window, ONLY within buffer of a short
strike, once per condor, closing with reason=assignment_guard. Early-close
aware via the cached calendar minute. Ledger: consecutive identical actions
collapse; action changes append.

Run:  PYTHONPATH=. .venv/bin/python scripts/verify_guard_ledger.py
"""
import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace

import backend.app.orchestrator as om
from backend.app.orchestrator import Orchestrator, ET
from backend.app.models import DashboardState

fails = []


def check(name, cond, detail=""):
    print(f"  {'✅' if cond else '❌'} {name}{('  — ' + detail) if detail else ''}")
    if not cond:
        fails.append(name)


class DedupStub:
    def __init__(self):
        self.d = {}
    def get(self, k):
        return self.d.get(k)
    def set(self, k, v):
        self.d[k] = v
    def already_done(self, k, v):
        return self.d.get(k) == v
    def mark_done(self, k, v):
        self.d[k] = v


def mk_orch(now_hhmm: str, close_min: int = 16 * 60):
    o = Orchestrator.__new__(Orchestrator)
    o.state = DashboardState(ts="t")
    o.alpaca_trader = object()          # non-None → guard proceeds
    o._is_live_bar = True
    today = datetime.now(ET).strftime("%Y-%m-%d")
    o._close_min_cache = (today, close_min)
    o.closed = []
    async def fake_close(ic, reason="stop"):
        o.closed.append((ic.build_id, reason))
        ic.broker_status = "closed_assign"
        ic.close_reason = reason
    o._close_alpaca_ic = fake_close
    hh, mm = now_hhmm.split(":")
    real_now = datetime.now(ET)
    fixed = real_now.replace(hour=int(hh), minute=int(mm), second=0)
    class FakeDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed if tz else fixed.replace(tzinfo=None)
    om.datetime = FakeDT
    return o, today


def mk_ic(today, cs=7500.0, ps=7430.0):
    leg = lambda s, l: SimpleNamespace(short_strike=s, long_strike=l, instrument="SPX")
    return SimpleNamespace(
        build_id=f"ic_{today}_{uuid.uuid4().hex[:4]}", broker_status="submitted",
        call_leg=leg(cs, cs + 20), put_leg=leg(ps, ps - 20),
        available=True, close_reason=None)


async def main():
    om.dedup = DedupStub()
    bar_at = lambda spy: SimpleNamespace(close=spy * 10.0)   # bar is SPX-scale

    print("1. outside window (14:00) → no action even if ITM")
    o, today = mk_orch("14:00")
    ic = mk_ic(today); o.state.iron_condor_history = [ic]
    await o._maybe_assignment_guard(bar_at(750.2))
    check("no close before window", not o.closed)

    print("2. in window (15:50), spot within $0.50 of short call → closes")
    o, today = mk_orch("15:50")
    ic = mk_ic(today); o.state.iron_condor_history = [ic]
    await o._maybe_assignment_guard(bar_at(749.8))   # short call 750, buf 0.50
    check("closed once", len(o.closed) == 1, str(o.closed))
    check("reason=assignment_guard", o.closed and o.closed[0][1] == "assignment_guard")
    await o._maybe_assignment_guard(bar_at(749.8))
    check("dedup: no second close", len(o.closed) == 1)

    print("3. in window, spot mid-range → holds")
    o, today = mk_orch("15:50")
    ic = mk_ic(today); o.state.iron_condor_history = [ic]
    await o._maybe_assignment_guard(bar_at(746.0))   # 4 from call, 3 from put
    check("no close when safe", not o.closed)

    print("4. put side threat closes too")
    o, today = mk_orch("15:52")
    ic = mk_ic(today); o.state.iron_condor_history = [ic]
    await o._maybe_assignment_guard(bar_at(743.3))   # short put 743, buf .50
    check("put-side close", len(o.closed) == 1)

    print("5. early-close day (13:00): window is 12:45-13:00, not 15:45")
    o, today = mk_orch("12:50", close_min=13 * 60)
    ic = mk_ic(today); o.state.iron_condor_history = [ic]
    await o._maybe_assignment_guard(bar_at(749.9))
    check("early-close window honored", len(o.closed) == 1)
    o2, today = mk_orch("15:50", close_min=13 * 60)
    ic2 = mk_ic(today); o2.state.iron_condor_history = [ic2]
    await o2._maybe_assignment_guard(bar_at(749.9))
    check("after early close → inert", not o2.closed)

    print("6. slot ledger collapse/append")
    o, today = mk_orch("11:00")
    o._record_slot(today, "13:00", "skip_regime", "volatile")
    o._record_slot(today, "13:00", "skip_regime", "volatile")
    rows = [r for r in o.state.meic_slots if r["slot"] == "13:00"]
    check("identical action collapsed", len(rows) == 1)
    o._record_slot(today, "13:00", "built", "NBBO ic_x $300")
    rows = [r for r in o.state.meic_slots if r["slot"] == "13:00"]
    check("action change appended", len(rows) == 2, str(rows))

    print()
    if fails:
        print(f"❌ FAILED: {fails}")
        raise SystemExit(1)
    print("✅ ALL PASS — guard windows/buffer/dedup/early-close correct; ledger sane.")


if __name__ == "__main__":
    asyncio.run(main())
