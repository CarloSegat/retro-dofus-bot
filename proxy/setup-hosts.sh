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

# NOTE: `sed -i` works in-place via rename(2), which fails on Docker's
# bind-mounted /etc/hosts ("Device or resource busy"). Stream to a temp
# file and overwrite via `cat >` so the inode stays the same.
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

echo "==> stripping any prior dofusretro/ankama-games/miner-proxy entries"
sed -e '/ankama-games/d' -e '/miner-proxy/d' -e '/dofusretro/d' "$HOSTS" > "$TMP"
cat "$TMP" > "$HOSTS"

echo "==> adding fresh hijack lines"
printf '127.0.0.1  dofusretro-co-production.ankama-games.com  # miner-proxy\n' >> "$HOSTS"
printf '127.0.0.2  dofusretro-ga-allisteria.ankama-games.com  # miner-proxy\n' >> "$HOSTS"

echo "==> ensuring 127.0.0.2/8 loopback alias"
if ip addr show lo | grep -q '127\.0\.0\.2'; then
    echo "    already present, skipping"
elif ip addr add 127.0.0.2/8 dev lo 2>/dev/null; then
    echo "    added"
else
    # Containers usually lack NET_ADMIN. The alias is cosmetic anyway --
    # lo is configured as 127.0.0.1/8, which covers all of 127.0.0.0/8,
    # so bind(127.0.0.2:443) and connect(127.0.0.2:443) both work without it.
    echo "    WARN: could not add 127.0.0.2/8 alias (missing NET_ADMIN?)."
    echo "          Proceeding -- 127.0.0.2 is reachable via lo's 127/8 anyway."
fi

echo
echo "==> verification (should print exactly 2 lines):"
grep -E 'ankama-games|miner-proxy' "$HOSTS"
echo
echo "==> loopback aliases:"
ip addr show lo | awk '/inet 127/ {print "    " $0}'
echo
echo "done."
