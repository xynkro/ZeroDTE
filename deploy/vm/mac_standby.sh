#!/usr/bin/env bash
# Run ON THE MAC at cutover: stop the Mac copies so two engines never trade one account.
# Reversible: launchctl load the same plists to bring the Mac back.
set -euo pipefail
for p in com.caspar.wavezero-backend com.caspar.wavezero-publish com.caspar.wavezero-surface; do
  launchctl unload "$HOME/Library/LaunchAgents/$p.plist" 2>/dev/null && echo "unloaded $p" || echo "$p not loaded"
done
sleep 2; launchctl list | grep -E "wavezero" || echo "Mac WaveZero jobs: none running"
