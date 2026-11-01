"""Pure test of live_call.morning_range_pct — the v3.1 loud-morning gate for the 13:00 mech arm."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from datetime import datetime
from zoneinfo import ZoneInfo
from app.predictor import Bar
from app.live_call import morning_range_pct
ET = ZoneInfo("America/New_York")
def bar(h, m, o, hi, lo, c, day=2):
    return Bar(time=datetime(2026, 11, day, h, m, tzinfo=ET), open=o, high=hi, low=lo, close=c, volume=1)
bars = [bar(9, 25, 6800, 6810, 6790, 6800),            # pre-open bar: ignored
        bar(9, 30, 6800, 6820, 6795, 6810),            # first RTH bar → open 6800
        bar(10, 0, 6810, 6860, 6805, 6850),            # high 6860
        bar(12, 30, 6850, 6855, 6790, 6800),           # low 6790 → range 70/6800 = 1.03%
        bar(13, 0, 6800, 6900, 6700, 6800),            # at/after the slot: ignored
        bar(10, 0, 6800, 7000, 6600, 6800, day=1)]     # yesterday: ignored
pct, n = morning_range_pct(bars, date="2026-11-02", slot_min=13 * 60)
assert n == 3, n
assert abs(pct - 70 / 6800 * 100) < 1e-9, pct
# scale-free: SPY×10 engine bars and SPY bars give the same %
pct10, _ = morning_range_pct([Bar(time=b.time, open=b.open / 10, high=b.high / 10, low=b.low / 10, close=b.close / 10, volume=1) for b in bars],
                             date="2026-11-02", slot_min=13 * 60)
assert abs(pct10 - pct) < 1e-9
# too few bars → None, not a false skip
assert morning_range_pct(bars[:2], date="2026-11-02", slot_min=13 * 60) == (None, 1)
assert morning_range_pct([], date="2026-11-02", slot_min=13 * 60) == (None, 0)
print(f"ok — morning range {pct:.3f}% over {n} bars; gate at 1.0% would skip: {pct > 1.0}")
