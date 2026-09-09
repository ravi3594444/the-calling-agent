#!/usr/bin/env bash
#
# Keep a DuckDNS record pointing at this machine.
#
# With a reserved static IP this is belt-and-braces, but it costs nothing and
# saves you if the address ever changes. Reads config from
# /etc/duckdns/duckdns.env:
#
#     DUCKDNS_SUBDOMAIN=my-agent      # no .duckdns.org suffix
#     DUCKDNS_TOKEN=xxxxxxxx-xxxx-...
#
# Leaving `ip=` empty makes DuckDNS use the source address of the request,
# which is what we want -- no need to detect our own IP.
set -euo pipefail

CONFIG=/etc/duckdns/duckdns.env
[[ -r "$CONFIG" ]] || { echo "missing $CONFIG" >&2; exit 1; }
# shellcheck source=/dev/null
source "$CONFIG"

: "${DUCKDNS_SUBDOMAIN:?set DUCKDNS_SUBDOMAIN in $CONFIG}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN in $CONFIG}"

response=$(curl -fsS --retry 3 --retry-delay 5 --max-time 30 \
  "https://www.duckdns.org/update?domains=${DUCKDNS_SUBDOMAIN}&token=${DUCKDNS_TOKEN}&ip=")

# DuckDNS answers with the literal string "OK" or "KO" -- no status code hint.
if [[ "$response" == OK* ]]; then
  echo "duckdns: ${DUCKDNS_SUBDOMAIN}.duckdns.org updated"
else
  echo "duckdns: update failed (response: ${response:-empty}); check the token" >&2
  exit 1
fi
