#!/usr/bin/env bash
# Config L v3 on the VM: settings + restart + verification. Run ON THE VM after the code is pulled.
set -euo pipefail
cd "$HOME/Trading/ZeroDTE-Wave"
python3 - <<'PY'
want={"CALL_MECH_ENABLED":"true","CALL_MECH_SLOT":"13:00","CALL_MECH_DIST_PCT":"0.30","CALL_STOP_MULT":"1.0",
      "CALL_MAX_OPEN":"2","CALL_MAX_TRADES_PER_DAY":"2","CALL_SLOTS":"13:00","CALL_AUTO_SUBMIT":"false",
      "CALL_AM_MIN_CONF":"0","CALL_MIN_CONF":"0.55","CALL_FLOOR_PCT_OF_WIDTH":"6","CALL_MIN_DIST_PCT":"0.30",
      "DIRECTIONAL_TP_TARGET":"60","WAVE_TIME_STOP_MIN":"35","PROBE_UNDERLYING":"XSP","PAPER_BROKER":"alpaca",
      "TELEGRAM_POLLER_ENABLED":"true"}
lines=open(".env").read().split("\n"); out=[]; seen=set()
for l in lines:
    k=l.split("=",1)[0].strip() if "=" in l and not l.lstrip().startswith("#") else None
    if k in want: out.append(f"{k}={want[k]}   # Config L v3 2026-10-30"); seen.add(k)
    else: out.append(l)
for k,v in want.items():
    if k not in seen: out.append(f"{k}={v}   # Config L v3 2026-10-30")
open(".env","w").write("\n".join(out)); print("env:", len(want), "keys set")
PY
grep -q '^ALPACA_BASE_URL=https://paper-api.alpaca.markets' .env || { echo "REFUSING: not paper"; exit 1; }
sudo systemctl restart wavezero-backend.service
sleep 35
curl -sf localhost:8766/api/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('backend ok',d['ok'],'feed',d['feed_type'],'alpaca',d['alpaca_ready'],'trading',d['trading_enabled'])"
PYTHONPATH=. .venv/bin/python -c "from backend.app.config import settings as s; print('mech',s.CALL_MECH_ENABLED,s.CALL_MECH_SLOT,s.CALL_MECH_DIST_PCT,'| stop',s.CALL_STOP_MULT,'| open',s.CALL_MAX_OPEN,'| poller',s.TELEGRAM_POLLER_ENABLED,'| auto',s.CALL_AUTO_SUBMIT)"
sleep 5; grep -E "bot poller started|Telegram bot commands registered|poll loop error|409" /var/log/wavezero/backend_stderr.log | tail -3 | cut -c1-120
