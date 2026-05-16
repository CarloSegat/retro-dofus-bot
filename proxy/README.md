# Proxy: MITM eyes for the click bot

Read-only TCP MITM that sits between the Dofus Retro client and Ankama's
servers, parses plaintext server-to-client packets, and publishes a JSON
event stream on `127.0.0.1:9999` that the Python bot consumes.

Adapted from the macOS handover. Same Go code, Linux setup instructions.

## What you get

For each game-relevant change (map load, mob spawn/despawn, fight start,
fight end), the proxy emits a JSON line:

```
{"type":"state","ts":..,"map_id":..,"my_cell":..,"in_fight":..,"mobs":[..],"players":[..]}
{"type":"fight_start","ts":..}
{"type":"fight_end","ts":..}
```

`mobs` entries are `{cell, group_id, members}` — `members` is the list of
mob gfx ids so you can identify packs.

## One-time Linux setup

You need two loopback IPs (one per upstream) and `/etc/hosts` entries that
hijack the two Ankama hostnames:

```bash
# Add 127.0.0.2 as a second loopback alias.
sudo ip addr add 127.0.0.2/8 dev lo

# Hijack both hostnames.
echo '127.0.0.1  dofusretro-co-production.ankama-games.com  # miner-proxy' \
    | sudo tee -a /etc/hosts
echo '127.0.0.2  dofusretro-ga-allisteria.ankama-games.com  # miner-proxy' \
    | sudo tee -a /etc/hosts
```

No `mDNSResponder` flush needed on Linux. systemd-resolved (if you use it)
honours /etc/hosts directly.

Tested with Dofus Retro under Wine. Wine routes through host networking
and reads the host's `/etc/hosts`, so no extra Wine config is required.

## Run

```bash
cd proxy
sudo go run ./cmd/proxy
# or: sudo go run ./cmd/proxy --events 127.0.0.1:9999
```

Needs sudo to bind port 443. Leave it running; launch Dofus Retro normally.

## Verify the event stream

In another terminal:

```bash
python3 test_proxy_eyes.py
```

You should see `STATE` lines every time you move maps, mobs spawn/despawn,
or a fight starts/ends.

## Cleanup

```bash
sudo sed -i '/miner-proxy/d' /etc/hosts
sudo ip addr del 127.0.0.2/8 dev lo
```

## Limitations (carried over from the macOS handover)

- **Read-only.** Client-to-server packets are encrypted by the BC anti-cheat
  after the early handshake. We forward those bytes verbatim and don't
  decrypt them. The bot still drives the game by clicking, not by injection.
- The first time the proxy sees a session, it parses the AYK login response
  to learn the real game-server address. If the game listener gets a
  connection before this happens (rare), it drops it; restart Dofus.
- `resolveDOH` uses Cloudflare DNS-over-HTTPS so the proxy never loops
  itself when /etc/hosts redirects the Ankama hostnames to localhost.
  Requires outbound HTTPS to `cloudflare-dns.com`.
