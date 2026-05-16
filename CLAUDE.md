# auto-fighter

Proxy-driven Dofus Retro fighter. Split off from the miner project
(`simple-miner-dofus`) and tuned for **Marx-Rockfeller**, a Sacrieur on
the `berlinthree` Ankama account. Reads the game's TCP stream as "eyes"
so the bot engages mobs by *cell id* instead of color-matching the
screen.

## Architecture

A Go MITM proxy (`proxy/`) sits between the Dofus client and Ankama
servers via `/etc/hosts` + a `127.0.0.2` loopback alias. It forwards
bytes verbatim (client→server is encrypted by BC anti-cheat — don't try
to inject) and parses the **plaintext server→client** stream into a
`MapState` (my_cell, map_id, mob groups, players, in-fight roster).
The proxy fans out JSON events on `127.0.0.1:9999`. Python scripts read
that, combine it with the **cell calibration** in `config.json` to
convert cell ids to screen pixels, and drive the game through `xdotool`
(wrapped in `utils.click` / `utils.right_click` / `utils.press`). No
simulated input ever bypasses those helpers.

Deeper references:
- [`docs/proxy_protocol.md`](docs/proxy_protocol.md) — fight state
  machine, packet table, placement burst, fallbacks
- [`docs/cell_geometry.md`](docs/cell_geometry.md) — iso grid math,
  neighbor deltas, calibration

## Entrypoints

- `main.py` — the fighter. Idle: engage nearest valid mob, navigate
  NSEW to a calibrated neighbour map when no valid mob exists.
  Combat: walk adjacent to the locked target, cast Sacrid Foot once
  if `ap >= sacrid_foot_ap_cost`, pass. All targeting/AP from proxy
  GTM data, no screen detection.
- `calibrate_map_cells.py <world_x> <world_y> [N]` — per-map calibration.
  Phase 1: click N (default 2) starting cells. Phase 2: click the
  N/E/S/W switch-map cells (press `s` to skip a direction the map
  lacks). Phase 3: click obstacle cells, Esc when done. Writes
  `map_data/<world_x>_<world_y>.json` (cells / switch_cells / obstacles).
  Depends on `config.json.cell_calibration` being already populated.
- `nav_graph.py` — print the connectivity graph between calibrated
  maps. Use to spot missing return-cell calibrations.
- `ignore_challenges_and_trades.py` — optional standalone helper that
  polls for trade/challenge popups and clicks Ignore. Runs in parallel
  to `main.py`.

## One-time setup (Linux)

```
sudo bash proxy/setup-hosts.sh   # /etc/hosts hijack + 127.0.0.2 alias
```

To undo: `sudo bash proxy/teardown-hosts.sh`.

## Running everything together

```bash
# Terminal 1: proxy (run from auto-fighter/)
sudo go run ./proxy/cmd/proxy --events 127.0.0.1:9999 2>&1 | tee /tmp/proxy.log

# Terminal 2: launch Dofus, log in, pick character (proxy must see ASK)

# Terminal 3: calibrate a new map (starts, NSEW exits, obstacles)
python3 -u calibrate_map_cells.py <world_x> <world_y> [N]

# Terminal 3: run the fighter
python3 -u main.py
```

Run python with `-u` or stdout buffered through `tee` makes the bot
look frozen.

## Config knobs (config.json)

- `cell_calibration`: pixel-to-cell transform. Don't hand-edit.
- `pass_turn_hotkey`: single key name, default `"e"`.
- `sacrid_foot_hotkey`: key for Sacrid Foot slot (e.g. `"2"`).
  **Required** — `main.py` refuses to start if empty.
- `sacrid_foot_ap_cost`: AP per Foot cast (default 4).
- `sacrid_buff_hotkey` / `sacrid_buff_ap_cost` / `sacrid_buff_max_dist`:
  Strength Punishment self-buff slot, cost, and the max Po distance
  at which the buff is worth casting.
- `sacrid_cast_wait_sec`: pause after each cast so the proxy `GTM`
  update with the new AP/HP arrives (default 0.8).
- `sacrid_walk_wait_sec`: max wait for `my_cell` to settle after a
  walk click (default 2.0).
- `empty_map_respawn_sec`: cooldown before the navigator will walk
  back into a map it just found empty (default 240).

## Things that have bitten us

- **`pyautogui.click` and pynput's `Button.left` both silently drop
  events in Dofus spell-aim mode.** The spell stays armed, cursor on
  target, but `my_ap` doesn't decrement. `xdotool click --delay 120 1`
  (via `utils.click`) goes through. **Do not** call pyautogui or
  pynput's `Controller` for input simulation from anywhere outside
  `utils.py`. See memory `feedback_spell_click_pynput.md`.
- **`GS` prefix** matches multiple Dofus packets if too greedy. Match
  exactly `pkt == "GS"` or `strings.HasPrefix(pkt, "GS|")`.
- **`GA;905;<actorId>;` is engage, NOT fight-end.** See
  `docs/proxy_protocol.md` for the full timing story.
- See `docs/cell_geometry.md` for the cell-formula trap.
