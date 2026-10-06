#!/usr/bin/env bash
# WaveZero VM bootstrap — Ubuntu 24.04 LTS, run as the deploy user (sudo rights), idempotent.
# Installs Python 3.13 via uv, the exact dependency lock, systemd units (publisher + sampler
# timers enabled; the BACKEND is enabled by cutover.sh only after .env + state are in place).
set -euo pipefail
REPO_URL=${REPO_URL:-https://github.com/xynkro/ZeroDTE.git}
BRANCH=${BRANCH:-wavezero}
BASE=$HOME/Trading
DIR=$BASE/ZeroDTE-Wave

sudo timedatectl set-timezone Asia/Singapore   # mirror the Mac the code was validated on
sudo apt-get update -qq
sudo apt-get install -y -qq git curl ca-certificates build-essential
sudo mkdir -p /var/log/wavezero && sudo chown "$USER":"$USER" /var/log/wavezero

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"

mkdir -p "$BASE"
if [ ! -d "$DIR/.git" ]; then git clone --branch "$BRANCH" "$REPO_URL" "$DIR"; fi
cd "$DIR"
git checkout "$BRANCH" && git pull --ff-only

uv python install 3.13
[ -x .venv/bin/python ] || uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.lock.txt
mkdir -p backend/data/historical backend/data/paper_trades backend/data/backtest_results

# Publisher pushes the wave-data branch over SSH with a repo-scoped deploy key (write access,
# added from the Mac with: gh repo deploy-key add -R xynkro/ZeroDTE --allow-write -t wavezero-vm <pubkey>)
if [ ! -f "$HOME/.ssh/wavezero_deploy" ]; then
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  ssh-keygen -t ed25519 -N "" -C "wavezero-vm-publisher" -f "$HOME/.ssh/wavezero_deploy" -q
fi
grep -q "Host github.com" "$HOME/.ssh/config" 2>/dev/null || cat >> "$HOME/.ssh/config" <<CFG
Host github.com
  IdentityFile $HOME/.ssh/wavezero_deploy
  IdentitiesOnly yes
CFG
chmod 600 "$HOME/.ssh/config"
ssh-keyscan -t ed25519 github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null || true
git remote set-url origin git@github.com:xynkro/ZeroDTE.git
echo "DEPLOY KEY (add on GitHub with write access):"; cat "$HOME/.ssh/wavezero_deploy.pub"

for u in wavezero-backend.service wavezero-publish.service wavezero-publish.timer \
         wavezero-surface.service wavezero-surface.timer; do
  sed -e "s|__USER__|$USER|g" -e "s|__HOME__|$HOME|g" "deploy/vm/$u" | sudo tee "/etc/systemd/system/$u" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable --now wavezero-publish.timer wavezero-surface.timer
echo "bootstrap OK: $(.venv/bin/python --version) in $DIR — now run push_state.sh from the Mac, then cutover.sh here"
