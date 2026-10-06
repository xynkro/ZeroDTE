# WaveZero on an always-on VM

Why: the Mac slept through 5 sessions in 3 weeks (Sep 14–17, Oct 2). A paper bot that is
asleep is not a bot. Everything here is paper-only; `cutover.sh` refuses a non-paper `.env`.

Order of operations (market closed):
1. Mac: `gcloud auth login --no-launch-browser` (Caspar approves in the browser, pastes the code).
2. Mac: `deploy/vm/gce_create.sh <project>` → e2-micro `wavezero` in us-east1-b.
3. VM:  `gcloud compute ssh wavezero --zone us-east1-b -- 'bash -s' < deploy/vm/bootstrap.sh`
        (prints the publisher deploy key) → Mac: `gh repo deploy-key add -R xynkro/ZeroDTE --allow-write -t wavezero-vm key.pub`
4. Mac: `deploy/vm/push_state.sh <ssh target>` → copies `.env` (chmod 600), `backend/data/*`, the 11 MB SPX history.
5. Mac: `deploy/vm/mac_standby.sh` → unloads the three Mac launchd jobs (two engines must never share the account).
6. VM:  `deploy/vm/cutover.sh` → enables the backend, prints `/api/status`.
Rollback: `launchctl load` the Mac plists, `sudo systemctl disable --now wavezero-backend` on the VM.

Runtime: Python 3.13 via uv, `requirements.lock.txt` (frozen from the validated Mac venv),
TZ=Asia/Singapore to mirror the Mac the code was validated on, logs in /var/log/wavezero/.
