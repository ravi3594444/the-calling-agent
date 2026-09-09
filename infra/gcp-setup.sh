#!/usr/bin/env bash
#
# Provision the GCE VM for the calling agent. Run this from your laptop with
# gcloud authenticated; it does not run on the server.
#
#   ./infra/gcp-setup.sh
#
# Sizing rationale: the server is a byte relay between two WebSockets. It does
# no transcoding and no local inference -- all STT/LLM/TTS work happens inside
# the AssemblyAI Voice Agent API -- so 2 vCPU / 2 GB is ample.
#
# Region is close to neutral for latency: the VM is one hop in a chain, so
# caller -> VM -> AssemblyAI sums to roughly the same total wherever it sits.
# asia-south1 is chosen for lower RTT when testing from an Indian phone.
set -euo pipefail

PROJECT="${PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
NAME="${NAME:-calling-agent}"
REGION="${REGION:-asia-south1}"
ZONE="${ZONE:-asia-south1-a}"
MACHINE="${MACHINE:-e2-small}"
DISK_GB="${DISK_GB:-30}"

if [[ -z "$PROJECT" ]]; then
  echo "ERROR: no project set. Run: gcloud config set project YOUR_PROJECT" >&2
  exit 1
fi

echo "Project : $PROJECT"
echo "Instance: $NAME ($MACHINE, ${DISK_GB}GB) in $ZONE"
echo

gcloud services enable compute.googleapis.com --project "$PROJECT"

# A static IP matters more than it looks: an ephemeral address changes on every
# stop/start, which breaks the DuckDNS record and forces certificate re-issue.
if ! gcloud compute addresses describe "${NAME}-ip" \
      --region "$REGION" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute addresses create "${NAME}-ip" \
    --region "$REGION" --project "$PROJECT"
fi
IP=$(gcloud compute addresses describe "${NAME}-ip" \
      --region "$REGION" --project "$PROJECT" --format='value(address)')
echo "Static IP: $IP"

# Only 80 and 443 are opened. 80 is required for the Let's Encrypt HTTP-01
# challenge; the app port itself stays bound to loopback behind Caddy.
if ! gcloud compute firewall-rules describe "${NAME}-web" \
      --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "${NAME}-web" \
    --project "$PROJECT" \
    --allow tcp:80,tcp:443 \
    --target-tags "$NAME" \
    --description "HTTP/HTTPS for the calling agent (80 needed for ACME)"
fi

if ! gcloud compute instances describe "$NAME" \
      --zone "$ZONE" --project "$PROJECT" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" \
    --project "$PROJECT" \
    --zone "$ZONE" \
    --machine-type "$MACHINE" \
    --image-family debian-12 \
    --image-project debian-cloud \
    --boot-disk-size "${DISK_GB}GB" \
    --boot-disk-type pd-balanced \
    --address "$IP" \
    --tags "$NAME" \
    --metadata-from-file startup-script=infra/server-bootstrap.sh
else
  echo "Instance $NAME already exists; leaving it alone."
fi

cat <<NEXT

Done. Next steps:

  1. Point DuckDNS at this machine.
     Create a subdomain at https://www.duckdns.org and set its IP to:

         $IP

  2. SSH in:

         gcloud compute ssh $NAME --zone $ZONE --project $PROJECT

  3. On the server, follow docs/DEPLOY.md -- clone the repo, write .env with
     ASSEMBLYAI_API_KEY and PUBLIC_HOSTNAME, then: make deploy-up

  4. Open https://<your-subdomain>.duckdns.org on your phone and tap Start.
NEXT
