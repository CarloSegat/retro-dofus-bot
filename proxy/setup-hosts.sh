#!/usr/bin/env bash
# One-time setup for the Dofus Retro MITM proxy.
#
# - Removes any stale/garbled entries from previous attempts.
# - Adds the two /etc/hosts hijack lines correctly on one line each.
# - Adds the 127.0.0.2 loopback alias if missing.
#
# Run as root:  sudo bash proxy/setup-hosts.sh
#
# Reverse with: sudo bash proxy/teardown-hosts.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "must be run as root: sudo bash $0" >&2
    exit 1
fi

HOSTS=/etc/hosts
BAK=/etc/hosts.bak.$(date +%Y%m%d-%H%M%S)

echo "==> backing up $HOSTS -> $BAK"
cp -a "$HOSTS" "$BAK"

echo "==> stripping any prior dofusretro/ankama-games/miner-proxy entries"
sed -i -e '/ankama-games/d' -e '/miner-proxy/d' -e '/dofusretro/d' "$HOSTS"

echo "==> adding fresh hijack lines"
printf '127.0.0.1  dofusretro-co-production.ankama-games.com  # miner-proxy\n' >> "$HOSTS"
printf '127.0.0.2  dofusretro-ga-allisteria.ankama-games.com  # miner-proxy\n' >> "$HOSTS"

echo "==> ensuring 127.0.0.2/8 loopback alias"
if ip addr show lo | grep -q '127\.0\.0\.2'; then
    echo "    already present, skipping"
else
    ip addr add 127.0.0.2/8 dev lo
    echo "    added"
fi

echo
echo "==> verification (should print exactly 2 lines):"
grep -E 'ankama-games|miner-proxy' "$HOSTS"
echo
echo "==> loopback aliases:"
ip addr show lo | awk '/inet 127/ {print "    " $0}'
echo
echo "done."
