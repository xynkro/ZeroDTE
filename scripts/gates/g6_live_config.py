"""G6: live config is armed with Config E values and the whole app imports at the deployed path."""
import sys
sys.path.insert(0, '.')
import backend.app.config
from backend.app import orchestrator, api          # full import must not raise
from backend.app.config import settings as s
from backend.app.wave_band_live import decide_band_trades, decide_band_trade

slots = [x.strip() for x in str(s.WAVE_BAND_ENTRY_SLOTS).split(',') if x.strip()]
assert s.WAVE_BAND_STRATEGY_ENABLED is True, "band strategy disabled"
assert s.WAVE_BAND_BOTH_SIDES is True, "both-sides (condor) not enabled"
assert len(slots) == 10, f"ladder has {len(slots)} slots, expected 10"
assert abs(s.WAVE_BAND_CUSHION_PCT - 0.4) < 1e-9, f"cushion {s.WAVE_BAND_CUSHION_PCT} != 0.4"
assert abs(s.WAVE_BAND_REAL_CREDIT_FLOOR - 10.0) < 1e-9, "real-credit floor is not 10%/ct"
assert abs(s.DIRECTIONAL_TP_TARGET - 40.0) < 1e-9, f"TP {s.DIRECTIONAL_TP_TARGET} != 40"
assert s.MEIC_ENABLED is False, "MEIC must stay off on the Wave instance"
assert callable(decide_band_trades) and callable(decide_band_trade)
print("GATE_G6_PASS")
