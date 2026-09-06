"""G13: anchor-floor trades a collapsed band at the cushion boundary, is a no-op on wide
bands, never enters the cushion, and the vol gate is caller-optional. Negative controls in."""
import sys, math; sys.path.insert(0, '.')
from backend.app.wave_band_live import decide_band_trades, BandParams, bollinger
flat=[7700+0.3*math.sin(i/3) for i in range(60)]; S0=flat[-1]; cd=S0*0.006
P=BandParams(cushion_pct=0.6, credit_floor=1.0)
assert decide_band_trades(flat,0.0004,60,None,None,P,both_sides=True,anchor_floor=False)==[]
on=decide_band_trades(flat,0.0004,60,None,None,P,both_sides=True,anchor_floor=True)
assert len(on)==2 and {d.side for d in on}=={"sell_put_cs","sell_call_cs"}
put=next(d for d in on if d.side=="sell_put_cs"); call=next(d for d in on if d.side=="sell_call_cs")
assert put.short_strike<=S0-cd and call.short_strike>=S0+cd and put.short_strike%5==0 and call.short_strike%5==0
assert all(d.pct_otm>=0.6 for d in on)
wide=[7700+120*math.sin(i/4) for i in range(60)]
a=decide_band_trades(wide,0.002,60,None,None,P,both_sides=True,anchor_floor=False)
b=decide_band_trades(wide,0.002,60,None,None,P,both_sides=True,anchor_floor=True)
assert a and [(d.side,d.short_strike) for d in a]==[(d.side,d.short_strike) for d in b], "not a no-op on wide bands"
assert decide_band_trades(flat,0.0004,60,0.001,None,P,both_sides=True,anchor_floor=True)==[], "gate broken"
assert decide_band_trades(flat,0.0004,60,None,None,BandParams(cushion_pct=0.6,credit_floor=1e9),both_sides=True,anchor_floor=True)==[]
print("GATE_G13_PASS")
