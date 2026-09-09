#!/usr/bin/env bash
#
# GCE startup script: installs Docker so the machine is ready to run the app.
# Runs as root on first boot. Deliberately does not fetch the app or any
# secrets -- you do that over SSH per docs/DEPLOY.md.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y ca-certificates curl gnupg git make

install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg \
  | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg

echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list

apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

systemctl enable --now docker

# Let the default login user run docker without sudo.
for u in $(ls /home); do usermod -aG docker "$u" 2>/dev/null || true; done

echo "bootstrap complete" > /var/log/calling-agent-bootstrap.done
