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
pixels, and drive the game through `xdotool` (wrapped in `utils.click` /
`utils.right_click` / `utils.press`). No simulated input ever bypasses
those helpers.

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

## Fight state is a tri-state machine

`fight_phase` (carried in every snapshot, emitted in `fight_engage` /
`fight_start` / `fight_end` events):

| Phase | Meaning |
|-------|---------|
| `idle` | Not in a fight. Mobs visible on the map, can engage. |
| `placement` | Just engaged. Placement screen up, 30s placement timer running. We can ready up. |
| `combat` | Combat actually started — turns flowing (`GTM` roster + `GTS<actor>` turn-start), spells cast. |

`Snapshot.in_combat` / `in_placement` / `in_fight` (= `phase != "idle"`)
are convenience properties on the Python side.

## Key proxy packets (parsed in `proxy/internal/proxy/state.go`)

| Packet | Meaning | Effect |
|--------|---------|--------|
| `ASK\|<id>\|<name>\|...` | Character chosen | `my_id` |
| `GDM\|<mapId>\|...` | Map change | `map_id`; clears mobs/players/fight; force `phase=idle` |
| `GM\|+<cell>;...;<-id>;...;-3;<gfx^lvl,...>` | Mob group spawn | `mobs[cell]` (subkind `-3` only) |
| `GM\|+<cell>;...;<+id>;<name>;...` | Player spawn | `players[id]` |
| `GA0;1;<actor>;<path>` | Out-of-fight move | last 2 chars of path = dofus64-encoded dest cell |
| `GA;1;<actor>;<path>` | In-fight / mob move | same; updates `my_cell` or mob's cell |
| `GA;905;<myId>;` | **Fight engage** (placement starts) | `phase: idle → placement`, emits `fight_engage`. Other actors' `GA;905;` ignored. |
| `GS` or `GS\|...` | **Combat start** (placement timer expired / everyone ready) | `phase: * → combat`, emits `fight_start` |
| `GE<xp>;<level>;...` | **Fight end** (post-fight XP summary) | `phase: * → idle`, emits `fight_end` |
| `GTM\|<id>;<status>;<hp>;<ap>;<mp>;<cell>;;<hp_max>\|...` | In-fight roster | `fight_entities[id]` (collapsed form `<id>;1` = dead) |
| `GTS<actor>\|<dur_ms>\|<turn_n>` | Turn-start for `<actor>` | `turn_actor`/`turn_number`/`turn_started_at_ms`; emits `turn_start` event. Main fighter waits for `actor==my_id`, then `turn_start_settle_sec` (1.5s) before acting. `GTF<actor>`/`GTR<actor>` (turn-finish/ready) are NOT parsed — we pass-turn ourselves and don't care about mob turn boundaries. |

The placement-start burst on the wire is:
`GA;905;<myId>;` → `GM\|--<groupId>` → `GJK2\|0\|1\|0\|<placement_ms>\|<n>` →
`GP<teamA>\|<teamB>\|<flag>` → `GM\|+<cell>;...;-1;973;-2;<gfx^lvl>;...`
(in-fight mob spawn) → `ILF<n>` → `GA;950;...` (initial fight actions) →
`GM\|+<cell>;...;<myId>;<myName>;...` (me placed). The proxy only keys off
the first packet (`GA;905;<myId>;`); the rest are sequencing detail.

**Fallbacks** (for the proxy attaching mid-fight or missing a packet):
- `GTM\|...` seen while phase != combat → promote to `combat` (GTM
  only fires inside an active fight).
- `GDM\|<mapId>` map change while phase != idle → force back to `idle`.

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
- `pass_turn_hotkey`: single key name (string), default `"e"`. Passed to
  xdotool via `utils.press`.
- `sacrid_foot_hotkey`: key for Marx-Rockfeller's Sacrid Foot slot
  (currently `"2"`). **Required** — `main.py` refuses to start if empty so
  a placeholder doesn't silently miscast.
- `sacrid_foot_ap_cost`: AP per Sacrid Foot cast (default 4).
- `sacrid_cast_wait_sec`: pause after each cast so the proxy `GTM` update
  with the new AP/HP arrives (default 0.8).
- `sacrid_walk_wait_sec`: max wait for `my_cell` to settle after a walk
  click (default 2.0).

## Things that have bitten us

- **`pyautogui.click` and pynput's `Button.left` both silently drop
  events in Dofus spell-aim mode.** The spell stays armed, the cursor
  visually sits on the target, but `my_ap` doesn't decrement.
  `xdotool click --delay 120 1` (via `utils.click`) goes through.
  Memory `feedback_spell_click_pynput.md`. **Do not** call pyautogui or
  pynput's Controller for input simulation from anywhere outside
  `utils.py`.
- **Cell formula:** `row=c//14, col=c%14` is WRONG for Dofus. Use the
  29-cells-per-pair sub-row layout above.
- **`GS` prefix** matches multiple Dofus packets if too greedy. Match
  exactly `pkt == "GS"` or `strings.HasPrefix(pkt, "GS|")`.
- **`GA;905;<actorId>;` is engage, NOT fight-end.** Earlier versions of
  the proxy treated it as fight-end, which meant `in_fight` only flipped
  true ~30 s after a click (when `GS` arrived). The 30s gap is the
  placement timer (`GJK2|...|30000|...`), not network latency.
- **Stale `phase`**: if the proxy connects mid-fight and misses
  `GA;905;`/`GS`/`GE`, a `GTM` in flight will still promote phase to
  `combat`, and the `GDM`-clears-phase fallback handles teleport-out.
- **Stdout buffering through `tee`**: run python with `-u` or output may
  appear to "do nothing" until the process exits.
