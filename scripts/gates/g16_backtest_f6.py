"""G16: Config F6 measured INDEPENDENTLY on the refreshed data: OOS beats E, positive on
the exact zero-trade fortnight, and max drawdown at DEPLOYED sizing < the 15% halt."""
import sys, statistics as st; sys.path.insert(0, '.')
from scripts.wave_lowvol_backtest import run, L10
from scripts.wave_band_backtest import OOS_START
from backend.app.config import settings as s
from backend.app.directional_spread_manager import recommend_contracts
ct,_=recommend_contracts(92.0,4,4); unit=ct/10.0            # backtest unit = 1 SPX = 10 SPY ct
f6=run(L10, gate=False, cushion=0.6, anchor_floor=True); e=run(L10, gate=True, cushion=0.4, anchor_floor=False)
days=sorted(f6[2]); ser=[f6[0].get(d,0.0)*unit for d in days]
eq=peak=dd=0.0
for v in ser:
    eq+=v; peak=max(peak,eq); dd=max(dd,peak-eq)
dd_pct=100*dd/s.ACCOUNT_SIZE_USD
oos=[d for d in days if d>=OOS_START]
f6_oos=st.mean(f6[0].get(d,0.0) for d in oos); e_oos=st.mean(e[0].get(d,0.0) for d in oos)
fort=[d for d in days if d>="2026-08-24"]; ftr=[p for d,p in f6[1] if d>="2026-08-24"]
assert len(fort)>=8, "refreshed data missing the fortnight"
assert len(ftr)>=20 and sum(ftr)>0, f"fortnight: {len(ftr)} trades ${sum(ftr):+.0f}"
assert f6_oos>e_oos, f"F6 OOS ${f6_oos:.1f} does not beat E ${e_oos:.1f}"
assert dd_pct<15.0, f"max DD {dd_pct:.1f}% at {ct}ct breaches the 15% halt"
tr=[p for _,p in f6[1]]; wr=sum(1 for t in tr if t>0)/len(tr)
assert wr>0.75, f"WR {wr:.2%}"
print(f"  {ct}ct = {unit:.1f} unit | maxDD {dd_pct:.1f}% | OOS F6 ${f6_oos:+.1f} vs E ${e_oos:+.1f}/sess | fortnight {len(ftr)} trades ${sum(ftr)*unit:+.0f} live-scale | WR {wr:.1%}")
print("GATE_G16_PASS")
