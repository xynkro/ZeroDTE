#!/usr/bin/env bash
# Run ON THE VM after push_state.sh copied .env + state. Starts the backend and proves it.
set -euo pipefail
cd "$HOME/Trading/ZeroDTE-Wave"
test -s .env || { echo "no .env — run push_state.sh from the Mac first"; exit 1; }
grep -q '^ALPACA_BASE_URL=https://paper-api.alpaca.markets' .env || { echo "REFUSING: .env is not paper"; exit 1; }
test -s backend/data/historical/SPX_5m_3y.json || { echo "missing historical data"; exit 1; }
sudo systemctl enable --now wavezero-backend.service
sleep 30
curl -sf -m 10 http://127.0.0.1:8766/api/status | python3 -c "
import sys,json; d=json.load(sys.stdin)
print('ok',d.get('ok'),'feed',d.get('feed_type'),'connected',d.get('feed_connected'),'alpaca',d.get('alpaca_ready'),'trading',d.get('trading_enabled'),'band',d.get('band',{}).get('armed'))"
sudo systemctl --no-pager --lines=5 status wavezero-backend.service | tail -6
