"""G17: deployed on the PRIMARY feed (the boot-retry proof), armed, ledger intact."""
import sys, json, urllib.request; sys.path.insert(0, '.')
from backend.app.debrief import trial_status, _load_trial_ledger
with urllib.request.urlopen("http://127.0.0.1:8766/api/status", timeout=10) as r: st=json.loads(r.read().decode())
assert st.get("backend_status")=="ok" and st.get("feed_connected") is True and st.get("trading_enabled") is True
b=st.get("band") or {}; assert b.get("enabled") and b.get("armed"), "band not armed"
assert st.get("feed_type")=="alpaca", f"NOT on the primary feed: {st.get('feed_type')} (boot retry failed?)"
led=_load_trial_ledger(); ts=trial_status([])
assert len(led)>=4 and ts.get("n_real",0)>=4 and ts.get("real_sum",0)>0, "trial ledger lost"
print(f"  feed alpaca | armed | ledger {len(led)} trades n_real={ts['n_real']} ${ts['real_sum']:+.0f}")
print("GATE_G17_PASS")
