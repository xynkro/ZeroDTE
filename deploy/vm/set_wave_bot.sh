#!/usr/bin/env bash
# Give WaveZero its OWN Telegram bot so /read and /flat work without fighting other pollers.
# Run ON THE MAC. Prompts for the bot token (hidden input, never echoed, never written locally),
# installs it on the VM over SSH, routes Wave messages to Caspar's DM, enables the poller, restarts.
#   1) In Telegram: @BotFather → /newbot → name "WaveZero" → username e.g. wavezero_caspar_bot → copy the token
#   2) ./deploy/vm/set_wave_bot.sh
#   3) In Telegram: open the new bot, press Start (required before a bot can DM you), then: /read up 7760
set -euo pipefail
H=${1:-wavezero.us-east1-b.wavezero-paper-1006}
read -r -s -p "Paste the WaveZero bot token (input hidden): " TOKEN; echo
[[ "$TOKEN" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]] || { echo "that does not look like a bot token"; exit 1; }
ME=$(curl -s "https://api.telegram.org/bot${TOKEN}/getMe" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['username'] if d.get('ok') else 'INVALID')")
[ "$ME" != "INVALID" ] || { echo "Telegram rejected the token"; exit 1; }
echo "bot @$ME verified"
printf '%s' "$TOKEN" | ssh "$H" 'cd Trading/ZeroDTE-Wave && umask 077 && T=$(cat) && python3 - "$T" <<PY
import sys
tok=sys.argv[1]
lines=open(".env").read().split("\n"); out=[]; seen=set()
want={"TELEGRAM_BOT_TOKEN":tok,"TELEGRAM_POLLER_ENABLED":"true","TELEGRAM_GROUP_CHAT_ID":"","TELEGRAM_TOPIC_ZERO_DTE":"","TELEGRAM_TOPIC_MACRO":"","TELEGRAM_TOPIC_IRON_CONDOR":""}
for l in lines:
    k=l.split("=",1)[0].strip() if "=" in l and not l.lstrip().startswith("#") else None
    if k in want: out.append(f"{k}={want[k]}" + ("" if k=="TELEGRAM_BOT_TOKEN" else "   # WaveZero own bot 2026-10-30: DM routing")); seen.add(k)
    else: out.append(l)
for k,v in want.items():
    if k not in seen: out.append(f"{k}={v}")
open(".env","w").write("\n".join(out)); print("VM .env: own bot installed, DM routing, poller on")
PY
sudo systemctl restart wavezero-backend.service; sleep 30; grep -E "bot poller started|commands registered|409" /var/log/wavezero/backend_stderr.log | tail -2 | cut -c1-100'
echo "done — now press Start on @$ME in Telegram, then send: /read up 7760"
