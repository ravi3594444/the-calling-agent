#!/usr/bin/env bash
#
# Install the DuckDNS updater on the server. Run as root:
#
#   sudo DUCKDNS_SUBDOMAIN=my-agent DUCKDNS_TOKEN=xxxx ./infra/duckdns/install.sh
set -euo pipefail

: "${DUCKDNS_SUBDOMAIN:?set DUCKDNS_SUBDOMAIN}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

install -m 0755 "$HERE/duckdns-update.sh" /usr/local/bin/duckdns-update.sh
install -d -m 0700 /etc/duckdns
# 0600: the token is a credential.
cat > /etc/duckdns/duckdns.env <<CFG
DUCKDNS_SUBDOMAIN=$DUCKDNS_SUBDOMAIN
DUCKDNS_TOKEN=$DUCKDNS_TOKEN
CFG
chmod 0600 /etc/duckdns/duckdns.env

install -m 0644 "$HERE/duckdns-update.service" /etc/systemd/system/
install -m 0644 "$HERE/duckdns-update.timer" /etc/systemd/system/

systemctl daemon-reload
systemctl enable --now duckdns-update.timer
systemctl start duckdns-update.service

echo
systemctl status duckdns-update.service --no-pager | tail -5
echo
echo "Timer installed. Verify with: systemctl list-timers duckdns-update.timer"
