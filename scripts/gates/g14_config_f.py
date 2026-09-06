"""G14: Config F is what the engine will load, and the orchestrator honours the flags."""
import sys, inspect; sys.path.insert(0, '.')
from backend.app.config import settings as s
from backend.app import orchestrator as om
assert s.WAVE_BAND_STRATEGY_ENABLED and s.WAVE_BAND_BOTH_SIDES and s.WAVE_BAND_ENTRY_NBBO
assert s.WAVE_BAND_VOL_GATE is False, "vol gate still ON"
assert s.WAVE_BAND_ANCHOR_FLOOR is True, "anchor-floor OFF"
assert abs(s.WAVE_BAND_CUSHION_PCT-0.6)<1e-9, f"cushion {s.WAVE_BAND_CUSHION_PCT}"
assert abs(s.WAVE_BAND_REAL_CREDIT_FLOOR-10.0)<1e-9 and abs(s.DIRECTIONAL_TP_TARGET-40.0)<1e-9
assert len([x for x in s.WAVE_BAND_ENTRY_SLOTS.split(',') if x.strip()])==10
assert s.MEIC_ENABLED is False
src=inspect.getsource(om.Orchestrator._maybe_open_band_trade)
assert "settings.WAVE_BAND_VOL_GATE" in src and "anchor_floor=settings.WAVE_BAND_ANCHOR_FLOOR" in src
assert "NBBO unpriceable" in src, "G12 diagnostic missing"
print("GATE_G14_PASS")
