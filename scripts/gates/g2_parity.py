"""G2: the validated single-side core is UNCHANGED.
(1) On the CURRENT dataset the live fn reproduces the backtest fn day-for-day (data-size
    independent — the dataset is refreshed periodically).
(2) On the FROZEN May-14 dataset the canonical numbers still reproduce exactly
    (378 trade-days, OOS +$41.68/day, t=5.85) — proves decide_band_trade is byte-equivalent."""
import sys, io, os, json, contextlib
from datetime import datetime
sys.path.insert(0, '.')
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    from scripts.test_wave_band_parity import run_live
    import scripts.wave_band_backtest as wbb
    from scripts.wave_band_backtest import score, OOS_START
    bt, _ = wbb.main(regime="released", bb_len=14, bb_mult=2.5)
    live = run_live(cushion_pct=0.0)
keys = sorted(set(bt) | set(live))
mism = [k for k in keys if round(bt.get(k, 0.0), 2) != round(live.get(k, 0.0), 2)]
assert len(bt) == len(live), f"trade-day count differs: bt {len(bt)} vs live {len(live)}"
assert len(mism) <= 1, f"parity broke on {len(mism)} days: {mism[:5]}"
assert not mism or mism[0] == '2022-01-03', f"unexpected mismatch day {mism[0]}"   # dataset day 1: window incomplete
ob, ol = score(bt, lo=OOS_START), score(live, lo=OOS_START)
assert round(ob['mean'], 2) == round(ol['mean'], 2) and round(ob['t'], 2) == round(ol['t'], 2), "OOS stats diverge"

bak = os.path.expanduser("~/Trading/ZeroDTE/backend/data/historical/SPX_5m_3y.json.bak-20260906")
assert os.path.exists(bak), "frozen dataset backup missing"
def _load_bak():
    return [(datetime.fromisoformat(r["datetime"]), r["high"], r["low"], r["close"]) for r in json.loads(open(bak).read())]
orig = wbb._load; wbb._load = _load_bak
try:
    with contextlib.redirect_stdout(buf):
        bt0, _ = wbb.main(regime="released", bb_len=14, bb_mult=2.5)
finally:
    wbb._load = orig
o0 = score(bt0, lo=OOS_START)
assert len(bt0) == 378, f"frozen trade-days {len(bt0)} != 378 — core CHANGED"
assert round(o0['mean'], 2) == 41.68 and round(o0['t'], 2) == 5.85, f"frozen OOS {o0['mean']:.2f}/t{o0['t']:.2f} != +41.68/t5.85 — core CHANGED"
print(f"  current data: {len(bt)} days, mismatch {len(mism)}, OOS ${ol['mean']:+.2f} t={ol['t']:.2f} | frozen May-14: 378 / +41.68 / t5.85 reproduced")
print("GATE_G2_PASS")
