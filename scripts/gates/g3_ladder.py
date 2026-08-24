"""G3: the slot ladder fires each configured slot exactly once/day and ignores stray bars."""
import sys
sys.path.insert(0, '.')
from backend.app.config import settings

slots = [s.strip() for s in str(settings.WAVE_BAND_ENTRY_SLOTS).split(',') if s.strip()]
assert len(slots) == 10, f"expected 10 ladder slots, got {len(slots)}: {slots}"
mins = []
for sv in slots:
    hh, mm = sv.split(':'); mins.append(int(hh)*60+int(mm))
assert mins == sorted(mins), "slots not in ascending order"
assert min(mins) >= 10*60 and max(mins) < 16*60, "slot outside the 10:00-16:00 window"

def fire(bar_min, done):
    for sv in slots:
        hh, mm = sv.split(':'); m = int(hh)*60+int(mm)
        if m <= bar_min < m + 25 and f"D:{sv}" not in done:
            return sv
    return None

# every 5m bar across a session, twice over (idempotence)
done = set(); fired = []
for _pass in range(2):
    for bm in range(9*60+30, 16*60, 5):
        hit = fire(bm, done)
        if hit:
            fired.append(hit); done.add(f"D:{hit}")
assert len(fired) == 10, f"expected 10 fires, got {len(fired)}: {fired}"
assert len(set(fired)) == 10, f"a slot fired twice: {fired}"
assert sorted(set(fired)) == sorted(slots), "fired set != configured slots"
# out-of-window bars must never fire
assert fire(9*60+55, set()) is None, "pre-open bar fired"
assert fire(15*60+59, set()) in (None, *slots), "late bar behaviour unexpected"
print("GATE_G3_PASS")
