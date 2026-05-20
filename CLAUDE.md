# auto-fighter

Proxy-driven Dofus Retro fighter. Split off from the miner project
(`simple-miner-dofus`) and tuned for **Marx-Rockfeller**, a Sacrieur on
the `berlinthree` Ankama account. Reads the game's TCP stream as "eyes"
so the bot engages mobs by *cell id* instead of color-matching the
screen.

## Architecture

The Dofus Retro Linux client is `dofus1electron` — a native x86-64 ELF
Electron app (Chromium under the hood). **Wine is not required** on
either the host or the desktop container; older notes that mention Wine
are describing the same Electron client.

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
  Combat: walk toward nearest live enemy, cast Dissolution (self-cast
  water AoE hitting the 4 edge-adjacent cells) once if anything is
  adjacent and `ap >= sacrid_dissolution_ap_cost`. If Dissolution
  can't fire and there's an enemy on the same iso axis with LoS in
  range, cast Attraction to pull it in for next turn. Pass. No target
  lock -- Dissolution doesn't care which mob it hits, so we re-pick
  nearest each turn. All targeting/AP from proxy GTM data, no screen
  detection.
- `calibrate_map_cells.py <world_x> <world_y> [N]` — per-map calibration.
  Phase 1: click N (default 2) starting cells. Phase 2: click the
  N/E/S/W switch-map cells (press `n` to skip a direction the map
  lacks). Phase 3: click obstacle cells, Esc when done. Upserts a row
  in the `maps` table and the per-cell child rows (`start_cells`,
  `switch_cells`, `obstacles`) via `dofus.map_data.save()`. Depends on
  `config.json.cell_calibration` being already populated and the
  Postgres DB being reachable (see "Database" below).
- `nav_graph.py` — print the connectivity graph between calibrated
  maps. Use to spot missing return-cell calibrations.
- `walk_to.py <world_x> <world_y>` — one-shot navigator. BFS over the
  calibrated graph (outbound-only edges, broader than `safe_directions`),
  fails upfront with non-zero exit if no path exists -- no clicks are
  issued in that case. On aggro mid-walk: runs the same combat stack
  as `main.py`, re-pathfinds from the post-fight map, resumes. Reuses
  `Orchestrator` for the runtime prompts / proxy / wiring -- accept
  the defaults if you just want to navigate.
- `ignore_challenges_and_trades.py` — optional standalone helper that
  polls for trade/challenge popups and clicks Ignore. Runs in parallel
  to `main.py`.

## One-time setup (Linux)

```
sudo bash proxy/setup-hosts.sh   # /etc/hosts hijack + 127.0.0.2 alias
docker compose up -d db          # Postgres for map_data (see Database)
pip install -r requirements.txt  # picks up psycopg
```

To undo the hosts hijack: `sudo bash proxy/teardown-hosts.sh`.

## Database

Per-map calibration (start cells, NSEW switches, obstacles, gatherable
resources, POIs like zaap/bank) lives in a Postgres DB managed by
`docker-compose.yml`. Schema in `db/schema.sql` is auto-applied on
first container start.

- Bring up:  `docker compose up -d db`  (binds `127.0.0.1:5432` only)
- Connect:   `psql postgresql://auto:auto@127.0.0.1:5432/auto_fighter`
- Override:  set `MAP_DB_URL` env var (consumed by `dofus.map_data`)

`map_data/*.json` files are the **pre-migration snapshot** — kept on
disk for now as a recovery backup. Once the DB has been verified end
to end, delete `map_data/` and remove this paragraph. The runtime
reads/writes only the DB. To (re)import the JSON snapshot:

```
python3 -m scripts.import_map_data_to_db
```

## Running everything together

```bash
# Terminal 1: proxy (run from auto-fighter/)
sudo go run ./proxy/cmd/proxy --events 127.0.0.1:9999 2>&1 | tee /tmp/proxy.log

# Terminal 2: launch Dofus, log in, pick character (proxy must see ASK)

# Terminal 3: ensure DB is up, then calibrate a new map
docker compose up -d db
python3 -u calibrate_map_cells.py <world_x> <world_y> [N]

# Terminal 3: run the fighter
python3 -u main.py
```

Run python with `-u` or stdout buffered through `tee` makes the bot
look frozen.

## Config knobs (config.json)

- `cell_calibration`: pixel-to-cell transform. Don't hand-edit.
- `pass_turn_hotkey`: single key name, default `"e"`.
- `sacrid_dissolution_hotkey`: key for Dissolution slot (e.g. `"2"`).
  **Required** — `main.py` refuses to start if empty. Dissolution is a
  self-cast water AoE: the bot presses the hotkey then clicks own cell.
- `sacrid_dissolution_ap_cost`: AP per Dissolution cast (default 4).
- `sacrid_dissolution_post_walk_settle_sec`: extra sleep ADDED to
  `pending_settle` before the Dissolution hotkey press when we just
  walked (default 0.33). Without it the hotkey lands mid-walk-animation
  and the client drops it -- spell-aim never arms and the follow-up
  click registers as a plain move click.
- `sacrid_bow_*`: legacy bow knobs. **Not wired anymore** -- the bow
  was replaced by Attraction. `fighter/weapon.py` is kept on disk
  (the class is class-agnostic and may be reused later for a different
  character class), but `Orchestrator` no longer constructs it.
- `sacrid_attraction_hotkey`: spell slot for Attraction, default `"1"`.
  Same press-then-click contract as Dissolution. Attraction is the
  ranged fallback when Dissolution can't fire (target not adjacent and
  not reachable this turn) -- it pulls a line-aligned enemy toward us
  for ~6 cells at level 5, setting up next turn's Dissolution. Skipped
  for adjacent enemies (already in melee).
- `sacrid_attraction_ap_cost` / `sacrid_attraction_min_range` /
  `sacrid_attraction_max_range`: AP per cast (default 3) and the
  Po-distance window (default 1..10; the spell's base max is 10 but
  the wielder is responsible for filtering adjacent targets -- the
  picker uses `max(2, min_range)` so dist=1 is always rejected).
  Targeting also requires the target to share an iso axis with us
  (same `u` OR same `v` in `cell_to_uv` terms -- the "cast in a
  straight line" restriction) AND LoS clear (blockers = static
  obstacles + all other live entities). One cast per turn (Retro caps
  Attraction at 1 cast per target per turn anyway).
- `sacrid_attraction_post_walk_settle_sec`: extra sleep ADDED to
  `pending_settle` before the Attraction hotkey press when we just
  walked (default 0.33). Same drop mechanism as Dissolution.
- `sacrid_buff_enabled`: fallback default for the buff toggle if the
  runtime prompt is skipped. `main.py` asks at startup whether to cast
  Bold Punishment, max mob group size (default 8), and min HP
  before engaging (default 500) — the answers override config for the
  session.
- `sacrid_buff_hotkey` / `sacrid_buff_ap_cost` / `sacrid_buff_max_dist`
  / `sacrid_buff_cooldown_turns`: Bold Punishment self-buff slot,
  AP cost, max Po distance at which the buff is worth casting, and the
  in-game cooldown in turns (default 5 — bot recasts as soon as ready).
  **Dissolution-priority gate**: when nearest enemy is adjacent
  (dist==1) AND `my_ap < buff_ap_cost + dissolution_ap_cost`, the buff
  is skipped so the AP feeds Dissolution. Under enemy AP-drain the
  buff would otherwise steal the turn's one possible Dissolution hit.
- `sacrid_vital_hotkey` / `sacrid_vital_ap_cost` /
  `sacrid_vital_cooldown_turns` / `sacrid_vital_post_walk_settle_sec`:
  Vital Punishment self-cast leftover-AP filler. Self-cast (press
  hotkey, click own cell) like Bold Punishment. Defaults: `ctrl+6`,
  3 AP, 4-turn cooldown, 0.33s extra post-walk settle. **Lowest-priority
  AP user**: only fires after buff + Dissolution + Attraction have
  run, so it consumes whatever AP is left over. Useful when an enemy
  debuff drops us below the 4 AP needed for Dissolution but we still
  have 3 for Vital. Cast in BOTH the normal-combat branch and the tofu
  retreat branch (in retreat it goes between the attraction cast and
  the random retreat walk so the self-cast click lands on a stationary
  cell). Vital applies the **Weakened** state to the Sacrieur (server
  packet `GA;108;<me>;<me>,13,2`) which blocks ALL weapons -- this
  mattered when the bow was wired; with Attraction (a spell, not a
  weapon) it does not block our follow-up casts. The old
  `sacrid_vital_weakened_turns` knob and the `weapon.notify_disabled`
  call were removed with the bow.
- `sacrid_swap_hotkey` / `sacrid_swap_ap_cost` / `sacrid_swap_min_ap`:
  Swap-position spell slot (default `"5"`), AP cost (default 2), and
  the AP floor below which swap is never considered (default 6 = swap
  cost + Dissolution cost). Cast on an adjacent enemy after the
  close-on walk: if exactly one enemy is adjacent AND that enemy has
  another enemy adjacent to its own cell, we swap into the target's
  cell so the follow-up Dissolution hits both the swapped target (now
  in our old cell) and the second enemy (already adjacent to our new
  cell). Skipped when 2+ enemies are already adjacent (Dissolution
  already doubles up) or when post-walk AP < min.
- `sacrid_swap_post_walk_settle_sec`: extra sleep ADDED to
  `pending_settle` before the swap hotkey press when we just walked
  (default 0.33). Same drop mechanism as Dissolution.
- `sacrid_cast_wait_sec`: pause after each cast so the proxy `GTM`
  update with the new AP/HP arrives (default 0.8).
- `sacrid_walk_wait_sec`: max wait for `my_cell` to settle after a
  walk click (default 2.0).
- `empty_map_respawn_sec`: cooldown before the navigator will walk
  back into a map it just found empty (default 240).
- `tofu_detect_threshold` / `tofu_detect_required_cycles`: hit-and-run
  detector. At the START of each of our turns (before we move) we
  sample the Po distance to the nearest alive enemy. If the last N
  samples are all `> threshold` AND the sequence is not strictly
  decreasing, the bot flips into "retreat" mode for the rest of the
  fight: if MP+AP allow closing to attack range AND casting
  Dissolution this same turn, do that first (free damage beats
  another retreat cycle). Then walk a random 1..mp_remaining steps
  AWAY from the nearest live enemy. Skips both the Strength
  Punishment buff and the follow-up positioning walk. The retreat
  breaks the kiter's rhythm
  -- they have to spend MP closing on a moving target rather than
  free-shooting us at max range. Sampling has to happen pre-move --
  mid-cycle distance is polluted by our own MP spend and would
  falsely trip the detector every fight. Defaults 4/3.

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
