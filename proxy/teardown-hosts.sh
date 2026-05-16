#!/usr/bin/env bash
# Undo proxy/setup-hosts.sh:
# - Remove hijack lines from /etc/hosts.
# - Drop the 127.0.0.2 loopback alias.
#
# Run as root:  sudo bash proxy/teardown-hosts.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must be run as root: sudo bash $0" >&2
    exit 1
fi

echo "==> removing miner-proxy lines from /etc/hosts"
sed -i '/miner-proxy/d' /etc/hosts

echo "==> dropping 127.0.0.2 loopback alias"
if ip addr show lo | grep -q '127\.0\.0\.2'; then
    ip addr del 127.0.0.2/8 dev lo
fi

echo "done. dofus will resolve normally on next launch."
