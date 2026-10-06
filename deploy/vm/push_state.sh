#!/usr/bin/env bash
# Run ON THE MAC. Copies the gitignored secrets + live state + historical data to the VM.
# Usage: deploy/vm/push_state.sh <ssh-target e.g. user@host or a gcloud-managed host alias>
set -euo pipefail
T=${1:?ssh target}
SRC="$HOME/Trading/ZeroDTE-Wave"
DST="Trading/ZeroDTE-Wave"
ssh "$T" "mkdir -p $DST/backend/data/historical"
scp -q "$SRC/.env" "$T:$DST/.env"
scp -q "$SRC"/backend/data/*.jsonl "$SRC"/backend/data/*.json "$T:$DST/backend/data/" 2>/dev/null || true
scp -q "$(readlink -f "$SRC/backend/data/historical/SPX_5m_3y.json")" "$T:$DST/backend/data/historical/SPX_5m_3y.json"
ssh "$T" "chmod 600 $DST/.env; ls -la $DST/.env $DST/backend/data | head -20"
