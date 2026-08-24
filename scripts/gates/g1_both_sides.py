"""G1: decide_band_trades returns 2 sides when both qualify, 1 when one does, 0 when none."""
import sys, math
sys.path.insert(0, '.')
from backend.app.wave_band_live import decide_band_trades, BandParams

closes = [5000 + 2.0*math.sin(i/7) + 0.05*i for i in range(80)]
r5, pr = 0.0012, 60

# permissive floor + zero cushion -> BOTH sides should qualify
both = decide_band_trades(closes, r5, pr, None, None,
                          BandParams(credit_floor=1.0, cushion_pct=0.0), both_sides=True)
assert len(both) == 2, f"expected 2 sides, got {len(both)}"
assert {d.side for d in both} == {"sell_put_cs", "sell_call_cs"}, f"sides: {[d.side for d in both]}"
assert both[0].short_strike != both[1].short_strike, "both sides share a strike"
# put strike must be BELOW spot, call ABOVE
put = next(d for d in both if d.side == "sell_put_cs")
call = next(d for d in both if d.side == "sell_call_cs")
assert put.short_strike < closes[-1] < call.short_strike, "strikes straddle spot incorrectly"

# single-side mode -> at most one
one = decide_band_trades(closes, r5, pr, None, None,
                         BandParams(credit_floor=1.0, cushion_pct=0.0), both_sides=False)
assert len(one) <= 1, f"single-side returned {len(one)}"

# impossible floor -> zero (negative control: proves the filter can actually reject)
none_ = decide_band_trades(closes, r5, pr, None, None,
                           BandParams(credit_floor=10**9, cushion_pct=0.0), both_sides=True)
assert none_ == [], f"impossible floor still returned {len(none_)}"

# vol gate must reject when r5 <= median (negative control)
gated = decide_band_trades(closes, r5, pr, r5 * 2, None,
                           BandParams(credit_floor=1.0, cushion_pct=0.0), both_sides=True)
assert gated == [], "vol gate failed to reject a below-median day"
print("GATE_G1_PASS")
