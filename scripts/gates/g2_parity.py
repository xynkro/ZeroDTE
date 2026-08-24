"""G2: the validated single-side core is unchanged — parity vs the backtest."""
import sys, io, contextlib
sys.path.insert(0, '.')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    exec(open('scripts/test_wave_band_parity.py').read().replace('__main__', '__parity__'))
    from scripts.test_wave_band_parity import run_live
    from scripts.wave_band_backtest import main as bt_main, score, OOS_START
    bt, _ = bt_main(regime="released", bb_len=14, bb_mult=2.5)
    live = run_live(cushion_pct=0.0)
keys = sorted(set(bt) | set(live))
mism = [k for k in keys if round(bt.get(k, 0.0), 2) != round(live.get(k, 0.0), 2)]
assert len(bt) == 378, f"backtest trade-days changed: {len(bt)} (expected 378)"
assert len(live) == 378, f"live-fn trade-days changed: {len(live)}"
# only the known first-day-of-dataset warmup may differ
assert len(mism) <= 1, f"parity broke on {len(mism)} days: {mism[:5]}"
if mism:
    assert mism[0] == '2022-01-03', f"unexpected mismatch day {mism[0]}"  # first day of the dataset: Bollinger window incomplete
o = score(live, lo=OOS_START)
assert round(o['mean'], 2) == 41.68, f"OOS mean drifted: {o['mean']}"
assert round(o['t'], 2) == 5.85, f"OOS t drifted: {o['t']}"
print("GATE_G2_PASS")
