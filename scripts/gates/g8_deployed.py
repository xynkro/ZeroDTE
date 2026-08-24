"""G8: backend live, feed connected, band armed, trial ledger intact after deploy."""
import sys, json, urllib.request
sys.path.insert(0, '.')
from backend.app.debrief import trial_status, _load_trial_ledger

with urllib.request.urlopen("http://127.0.0.1:8766/api/status", timeout=10) as r:
    st = json.loads(r.read().decode())
assert st.get("backend_status") == "ok", f"backend_status={st.get('backend_status')}"
assert st.get("feed_connected") is True, "feed not connected"
assert st.get("trading_enabled") is True, "trading disabled"
band = st.get("band") or {}
assert band.get("enabled") is True, "band not enabled in live API"
assert band.get("armed") is True, "band NOT armed (median seed failed)"
# trial memory must survive the deploy
led = _load_trial_ledger()
assert len(led) >= 4, f"trial ledger lost trades: {len(led)}"
ts = trial_status([])
assert ts.get("n_real", 0) >= 4, f"scoreboard shows {ts.get('n_real')} real trades"
assert ts.get("real_sum", 0) > 0, "trial P&L no longer positive"
print(f"  backend ok | feed {st.get('feed_type')} | armed | ledger {len(led)} trades, "
      f"n_real={ts['n_real']} ${ts['real_sum']:+.0f}")
print("GATE_G8_PASS")
