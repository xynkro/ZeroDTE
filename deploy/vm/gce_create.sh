#!/usr/bin/env bash
# Run ON THE MAC after `gcloud auth login`. Creates the smallest always-on VM that fits
# (backend RSS on the Mac = 35 MB): e2-micro, us-east1 (Always-Free eligible, 1 per account),
# Ubuntu 24.04 LTS, 20 GB standard disk, SSH only (no HTTP exposure; the API binds loopback).
# Usage: deploy/vm/gce_create.sh <project-id> [zone]
set -euo pipefail
PROJECT=${1:?gcp project id}
ZONE=${2:-us-east1-b}
NAME=${NAME:-wavezero}
gcloud config set project "$PROJECT" >/dev/null
gcloud services enable compute.googleapis.com --project "$PROJECT"
if ! gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" --project "$PROJECT" --zone "$ZONE" \
    --machine-type e2-micro --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
    --boot-disk-size 20GB --boot-disk-type pd-standard --shielded-secure-boot \
    --metadata enable-oslogin=false --labels app=wavezero,env=paper
fi
gcloud compute instances describe "$NAME" --zone "$ZONE" --project "$PROJECT" \
  --format='value(name,status,machineType.basename(),networkInterfaces[0].accessConfigs[0].natIP)'
echo "ssh: gcloud compute ssh $NAME --zone $ZONE --project $PROJECT"
