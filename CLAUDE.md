# auto-fighter

Proxy-driven Dofus Retro fighter. Split off from the miner project
(`simple-miner-dofus`) and tuned for **Marx-Rockfeller**, a Sacrieur on
the `berlinthree` Ankama account. Reads the game's TCP stream as "eyes"
so the bot engages mobs by *cell id* instead of color-matching the
screen.

## Architecture in one paragraph

A Go MITM proxy (`proxy/`) sits between the Dofus client and Ankama servers
via `/etc/hosts` + a `127.0.0.2` loopback alias. It forwards bytes verbatim
(client→server is encrypted by BC anti-cheat — don't try to inject) and
parses the **plaintext server→client** stream into a `MapState`
(my_cell, map_id, mob groups, players, in-fight roster). The proxy fans out
JSON events on `127.0.0.1:9999`. Python scripts read that, combine it with
the **cell calibration** in `config.json` to convert cell ids to screen
pixels, and drive `pyautogui`.

## Entrypoints

- `main.py` — the fighter. Engage nearest mob (out of fight), then per
  turn (in fight): walk adjacent to the closest alive enemy from
  `fight_entities`, cast Sacrid Foot (`sacrid_foot_hotkey`) once if
  adjacent and `ap >= sacrid_foot_ap_cost`, pass. One cast per turn
  (game cooldown). All targeting/AP off proxy GTM data, no screen
  detection.
- `calibrate_cells.py [N]` — proxy-driven cell→screen calibration. Click N cells,
  walks character there, fits `(origin_x, origin_y, cell_w, cell_h)`. Writes
  `config.json.cell_calibration`. Run once per window size.
- `ignore_challenges_and_trades.py` — optional standalone helper that
  polls for trade/challenge popups and clicks Ignore. Runs in parallel
  to `main.py`.

## Cell geometry — the load-bearing thing

Dofus iso grid is **rotated 45°**. Cell numbering is *not* a simple chess
grid:

- Sub-rows alternate width: **even = 14 cells, odd = 15 cells (offset half
  cell left)**. Two sub-rows = **29 cells = one chess-row**.
- Edge-adjacent neighbor deltas: NE=−14, SE=+15, NW=−15, SW=+14. Implied:
  E=+1, S=+29, W=−1, N=−29.
- `cell_grid.cell_to_xy(cell, origin_x, origin_y, cell_w, cell_h)`,
  `cell_grid.cell_distance(a, b)` (Dofus "Po" range, L1 in iso (u,v) basis),
  and `cell_grid.neighbors(cell)` (the 4 edge-adjacent cells, used by
  the Sacrid loop's walk-to-adjacent step).
- Calibration is per-window-size; re-run `calibrate_cells.py` if you resize
  the game.

## Key proxy packets (parsed in `proxy/internal/proxy/state.go`)

| Packet | Meaning | What we extract |
|--------|---------|-----------------|
| `ASK\|<id>\|<name>\|...` | Character chosen | `my_id` |
| `GDM\|<mapId>\|...` | Map change | `map_id`; clears mobs/players/fight |
| `GM\|+<cell>;...;<-id>;...;-3;<gfx^lvl,...>` | Mob group spawn | `mobs[cell]` (subkind `-3` only) |
| `GM\|+<cell>;...;<+id>;<name>;...` | Player spawn | `players[id]` |
| `GA0;1;<actor>;<path>` | Out-of-fight move | last 2 chars of path = dofus64-encoded dest cell |
| `GA;1;<actor>;<path>` | In-fight / mob move | same; updates `my_cell` or mob's cell |
| `GS` or `GS\|...` | Fight start | `in_fight = true` |
| `GA;905;...` | Fight end (explicit) | `in_fight = false` |
| `GTM\|<id>;<status>;<hp>;<ap>;<mp>;<cell>;;<hp_max>\|...` | In-fight roster | `fight_entities[id]` (collapsed form `<id>;1` = dead) |

Implicit fallback: **any `GDM` clears `in_fight`** — covers fights that end
via teleport without a `GA;905;`.

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

# Terminal 3: calibrate once per window size
python3 -u calibrate_cells.py 5

# Terminal 3: run the fighter
python3 -u main.py
```

## Config knobs (config.json)

- `cell_calibration`: written by `calibrate_cells.py`. Don't hand-edit.
- `pass_turn_hotkey`: list of pyautogui key names, default `["ctrl","e"]`.
  **On Ubuntu/Linux Dofus reads Super, not Ctrl** → use `["winleft","e"]`.
- `sacrid_foot_hotkey`: pyautogui key for Marx-Rockfeller's Sacrid Foot
  slot. **Required** — `main.py` refuses to start if empty so a
  placeholder doesn't silently miscast. Currently `"2"`.
- `sacrid_foot_ap_cost`: AP per Sacrid Foot cast (default 4).
- `sacrid_cast_wait_sec`: pause after each cast so the proxy `GTM` update
  with the new AP/HP arrives (default 0.8).
- `sacrid_walk_wait_sec`: max wait for `my_cell` to settle after a walk
  click (default 2.0).

## Things that have bitten us

- **`pyautogui.moveTo` before a pynput right-click silently breaks Dofus
  retro on X11.** Memory `feedback_right_click_pynput.md`. (Sacrid Foot
  uses left-click target-cell, so this doesn't bite `main.py` — but
  it's still the right pattern if a future spell needs right-click.)
- **Cell formula:** `row=c//14, col=c%14` is WRONG for Dofus. Use the
  29-cells-per-pair sub-row layout above.
- **`GS` prefix** matches multiple Dofus packets if too greedy. Match
  exactly `pkt == "GS"` or `strings.HasPrefix(pkt, "GS|")`.
- **Stale `in_fight`**: if proxy connects mid-fight and misses `GA;905;`,
  the flag wedges. The `GDM`-clears-`in_fight` fallback handles this on
  next map change.
- **Stdout buffering through `tee`**: run python with `-u` or output may
  appear to "do nothing" until the process exits.
