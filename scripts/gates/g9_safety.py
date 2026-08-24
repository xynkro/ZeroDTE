"""G9: paper-only invariants. Negative controls included so absence is provable."""
import sys, re, pathlib
sys.path.insert(0, '.')
from backend.app.config import settings as s

assert s.ALPACA_BASE_URL == "https://paper-api.alpaca.markets", f"NOT paper: {s.ALPACA_BASE_URL}"
assert s.PAPER_BROKER == "alpaca", f"broker {s.PAPER_BROKER}"
assert "api.alpaca.markets" not in s.ALPACA_BASE_URL.replace("paper-api.alpaca.markets", ""), "live endpoint leaked"
assert getattr(s, "IBKR_PORT", 7497) != 7496, "IBKR port raised to the live-money port"
# no source file may reference the real-money endpoint
src = pathlib.Path("backend/app")
bad = []
for f in src.glob("*.py"):
    t = f.read_text()
    for m in re.finditer(r"https://api\.alpaca\.markets", t):
        bad.append(f.name)
assert not bad, f"real-money endpoint referenced in {set(bad)}"
# positive control: the detector must actually be able to find that string
probe = "https://api.alpaca.markets"
assert re.search(r"https://api\.alpaca\.markets", probe), "detector is broken (positive control failed)"
# kill-switch must not be invoked anywhere in the band entry path
orch = pathlib.Path("backend/app/orchestrator.py").read_text()
band_fn = orch.split("async def _maybe_open_band_trade")[1].split("\n    async def ")[0]
assert "close_all_positions" not in band_fn, "kill-switch called from the band entry path"
print("GATE_G9_PASS")
