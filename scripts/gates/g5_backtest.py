"""G5: Config E has positive expectancy, measured independently from trade-level stats.
Applies the bear case's own falsifiable test: win_rate*avgWin > loss_rate*avgLoss."""
import sys, statistics as st
sys.path.insert(0, '.')
from scripts.wave_final_strategy import run, L10
from scripts.wave_band_backtest import OOS_START
from backend.app.quant_utils import newey_west_tstat

daily, trades, sessions, peak = run(L10, both=True)          # Config E
assert len(trades) > 500, f"too few trades to judge: {len(trades)}"
w = [t for t in trades if t > 0]; l = [t for t in trades if t <= 0]
wr = len(w)/len(trades); aw = st.mean(w); al = abs(st.mean(l))
ev = wr*aw - (1-wr)*al                                        # measured, not copied
per_day = len(trades)/sessions
days = [v for k, v in sorted(daily.items()) if k >= OOS_START]
t = newey_west_tstat(days)["t"]

assert ev > 0, f"NEGATIVE expectancy ${ev:.2f}/trade — bear case wins"
assert wr*aw > (1-wr)*al, "fails the bear-case EV inequality"
assert t > 2.0, f"OOS t-stat {t:.2f} below the 2.0 significance bar"
assert per_day >= 2.0, f"only {per_day:.2f} trades/day — volume goal unmet"
assert st.mean(days) > 0, "OOS daily mean is not positive"
# one-side control: Config E must beat it on $/day (proves both-sides earns its risk)
d1, tr1, s1, _ = run((600, 660, 720), both=False)
days1 = [v for k, v in sorted(d1.items()) if k >= OOS_START]
assert st.mean(days) > st.mean(days1), "both-sides does not beat one-side on $/day"
print(f"  EV ${ev:+.2f}/trade | WR {100*wr:.1f}% avgW ${aw:.0f} avgL ${al:.0f} | "
      f"{per_day:.2f}/day | OOS ${st.mean(days):+.0f}/d t={t:.2f} | one-side ${st.mean(days1):+.0f}/d")
print("GATE_G5_PASS")
