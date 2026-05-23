# auto-fighter

Proxy-driven Dofus Retro fighter. Split off from the miner project
(`simple-miner-dofus`) and tuned for **Marx-Rockfeller**, a Sacrieur on
the `berlinthree` Ankama account. Reads the game's TCP stream as "eyes"
so the bot engages mobs by *cell id* instead of color-matching the
screen.

Two character profiles are supported, picked at startup via the
"character class" prompt (`sacrieur` / `enutrof`, default `sacrieur`):
- **Sacrieur** (`fighter/sacrieur.py`): the full brain — Dissolution
  AoE, Bold Punishment buff, Vital Punishment filler, Swap, Attraction,
  tofu-retreat mode. The original profile.
- **Enutrof** (`fighter/enutrof.py`): single-spell brain — Coins
  Throwing only. Walks into range/LoS if needed, spams Coins until AP
  runs out, passes. 2 AP per cast, 13 range. No buffs, no retreat
  logic.

Both profiles share the same engager/navigator/regen/inventory pipeline
and the same combat-callback contract (`on_fight_engaged(snap)`,
`play_turn(ctx)`).

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
  detection. `--auto-reuse` skips every interactive prompt and loads
  the settings saved by the previous run -- only used by the
  orchestrator's in-bot restart path (see `scripts/restart_dofus.py`
  below) after `os.execv`; a human at the terminal should never need
  to pass it.
- `calibrate_map_cells.py <world_x> <world_y> [N] [--screen NAME]` —
  per-map calibration. Phase 1: click N (default 2) starting cells.
  Phase 2: click the N/E/S/W switch-map cells (press `n` to skip a
  direction the map lacks). Phase 3: click obstacle cells, Esc when
  done. Upserts a row in the `maps` table and the per-cell child rows
  (`start_cells`, `switch_cells`, `obstacles`) via
  `dofus.map_data.save()`. Depends on the chosen screen's pixel
  calibration in `config.json.cell_calibrations[<name>]` being
  already populated (see `recalibrate_screen.py`) and the Postgres DB
  being reachable (see "Database" below).
- `recalibrate_screen.py <name> [--samples N]` — (re)fit the pixel
  calibration for a named screen. The Dofus window size/position
  depends on the visible environment (host laptop, docker VNC desktop,
  etc.), so each environment needs its own entry under
  `config.json[cell_calibrations][<name>]`. Click N (default 4)
  walkable cells; each click registers (click_xy, walked-to-cell_id)
  via the proxy's `my_cell` update, then `dofus.cell_grid.fit_calibration`
  least-squares-fits origin + cell size. Saves with `fitted_at`
  timestamp; sets `default_screen` if unset.
- `calibrate_play_button.py <name>` — per-screen capture of the
  Ankama Launcher PLAY-button pixel position. Listens for one
  left-click via pynput; saves to
  `config.json[restart_clicks][<name>][play_button]`. Required by
  `restart_dofus.py` (below). Open the launcher and maximize it
  before running, so the calibrated position matches what
  `restart_dofus.py` will see post-restart.
- `calibrate_post_launcher_clicks.py <name>` — captures the two
  in-game click positions used after launcher PLAY: the server
  card on "Choose a server" (`server_first`) and the character
  card on "Choose your character" (`character_first`). Merged
  into the same `config.json[restart_clicks][<name>]` entry that
  `calibrate_play_button.py` writes to. Single click per slot;
  press Enter between slots so navigation clicks (double-clicking
  the server card to advance) don't get captured.
- `scripts/restart_dofus.py [--screen NAME] [--no-character-click]` —
  "nuke and restart" tool for when the Dofus client hangs (UI freeze,
  even manual VNC input is dropped). Kills both the game and launcher
  windows by their X11 window PIDs (SIGKILL), sweeps
  `/tmp/.mount_Retro` for FUSE-mount survivors (the launcher binary
  is named `zaap`, NOT anything matching the AppImage path),
  relaunches the AppImage, waits + maximizes the launcher,
  double-clicks PLAY, waits + double-clicks the server card, waits
  + double-clicks the character card (skip with `--no-character-click`
  when the character was mid-fight — the server auto-enters in that
  case and the extra click would land on the map). All clicks use
  `xdotool windowraise + windowactivate + click` without `--window`
  so they propagate through Electron's nested renderer windows.
  **Two entry points**: the CLI (above, for manual VNC recovery) and
  a callable `restart_dofus_client(screen_name, ...)` consumed by
  `fighter/orchestrator.py`'s `ProgressWatchdog`. When the watchdog
  trips (idle no-progress or in-fight stale-turn), the orchestrator
  calls `restart_dofus_client` and then `os.execv`-s `main.py` with
  `--auto-reuse` so a fresh process resumes from the saved settings
  -- no operator action required.
- `calibrate_fight_ui_dismiss.py <name>` — per-screen capture of the
  in-fight turn-order bar `<>` collapse toggle. Listens for one
  left-click via pynput; saves to
  `config.json[fight_ui_dismiss_clicks][<name>][collapse_turn_order]`.
  Must be run during an active fight (the turn-order bar is only
  visible mid-fight). `Orchestrator` clicks this position on every
  `on_fight_engaged` so the bar doesn't overlap clickable map cells.
  Missing entry = one-shot warning + no-op.
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
- `scripts/monitor_connectivity.py` — host-side internet probe. TCP-
  connects to Cloudflare/Google/Quad9 DNS (port 53) every 5s; treats
  "all 3 fail" as DOWN. Logs state transitions + periodic heartbeats
  (UP and DOWN) to `logs/network.log` (daily rotation, 30 days kept).
  Run once per HOST (not per container) -- internet outages affect
  every bot at the same time, so one monitor catches them all. Pair
  with `restart_history.jsonl` and `fighter.log` timestamps to prove
  a bot trip was an external outage (router restart, ISP blip) and
  not a bot bug:
  ```bash
  # start (host shell, not container)
  nohup python3 -u scripts/monitor_connectivity.py >/dev/null 2>&1 &
  # later: see all outages in the last week
  grep -E "DOWN|UP -- " logs/network.log
  # correlate with a watchdog trip at 14:32
  awk '$2 >= "14:25:00" && $2 <= "14:35:00"' logs/network.log
  ```

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

### Farming areas

A **farming area** is a named, strongly-connected subset of calibrated
maps. At startup `main.py` prompts which one to scope navigation to;
`MapNavigator` then refuses to walk to a target outside the area.
Empty selection (`0`) means free-roam.

Tables:
- `farming_areas (area_id, name UNIQUE, created_at)`
- `farming_area_maps (area_id, map_id)` — N:N membership

Strong connectivity (every map in the area reachable from every other
map via in-area `switch_cells` edges) is enforced by `dofus.map_data.
create_farming_area` on insert, not by the DB itself. Forward + backward
BFS from one node; both must reach the whole set.

Manage areas via the CLI:

```
# interactive create (lists calibrated maps, asks name + world coords,
# validates connectivity, writes)
python3 -m scripts.create_farming_area

# list existing
python3 -m scripts.create_farming_area --list

# delete by name
python3 -m scripts.create_farming_area --delete "Tofu Plains"
```

Public Python API in `dofus.map_data`: `list_farming_areas()`,
`get_farming_area(area_id)`, `get_farming_area_by_name(name)`,
`is_strongly_connected(map_ids, map_data, by_world)`,
`create_farming_area(name, map_ids)`, `delete_farming_area(area_id)`.

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

## Diagnosing a fuck-up after the fact

When you come back to a bot that's stuck / logged out / sat in the
character selection screen for hours, both halves write rotating
logs under `<repo>/logs/<instance>/`:

```
logs/
  desktop-1/                   # FIGHTER_INSTANCE
    fighter.log                # live bot log
    fighter.log.YYYY-MM-DD_HH-MM   # rotated chunks (10 min each, 24h)
    proxy.log                  # live proxy log
    proxy.log.YYYY-MM-DD_HH-MM
  desktop-2/
    ...
  host/                        # default instance when run outside docker
    ...
```

The convention is **one container per Dofus character**, so each
`<instance>` subdirectory holds the full story for exactly one
character. The bot prints a `[session] instance=<X> character=<Y>
pid=<P>` banner once per process at startup, so a retrospective
`grep <character-name> logs/*/fighter.log*` lands in the right
subdir without already knowing which container ran them.

- **Bot side** (`main.py` / `walk_to.py`):
  `logs/<instance>/fighter.log` is the live file. Older 10-minute
  chunks rotate to `fighter.log.YYYY-MM-DD_HH-MM`, keeping ~24h
  (144 files). Configured in `fighter/logging_setup.py` --
  every `print(...)` is teed through it, prefixed with
  `YYYY-MM-DD HH:MM:SS`. Instance is picked from
  `$FIGHTER_INSTANCE`, falling back to `socket.gethostname()` then
  `host`. Character name comes from `$FIGHTER_CHARACTER` (banner
  only -- not embedded in every line).
- **Proxy side** (`sudo go run ./proxy/cmd/proxy ...`):
  `logs/<instance>/proxy.log` with the same rotation shape.
  `--log-dir`, `--log-interval`, `--log-backups`, `--instance`
  flags override the defaults. Instance falls through to
  `$FIGHTER_INSTANCE` then `os.Hostname()` then `host` -- matching
  the Python side. Also still tees to stderr so `docker logs`
  and interactive runs show the same lines live.

**Important**: the proxy's `--log-dir` defaults to `$PWD/logs`.
Always launch the proxy from the repo root, otherwise its logs
land under `proxy/logs/<instance>/` instead of `logs/<instance>/`.
The desktop container's `start.sh` passes `--log-dir
/workspace/logs --instance "$FIGHTER_INSTANCE"` explicitly so
nothing depends on cwd inside the container.

If you change `FIGHTER_INSTANCE` or `FIGHTER_CHARACTER` in
docker-compose.yml you MUST `docker compose up -d --force-recreate
<service>` -- env-var changes don't apply to a running container.

Diagnostic playbook:

```bash
# 1. Find the instance/character you care about
grep -lE "\[session\] .*character=Marx-Rockfeller" logs/*/fighter.log*

# 2. Newest chunks first within that instance
ls -lt logs/desktop-1/

# 3. Grep both sides for the disconnect markers we emit
grep -nE "DISCONNECT|STALE|client_disconnected|game client disconnected" \
    logs/desktop-1/fighter.log* logs/desktop-1/proxy.log*

# 4. Were there any auto-restarts? (watchdog or stale-turn -> restart_dofus
#    + execv). The history file is append-only and survives 24h log
#    rotation -- safe to use after a long absence.
cat logs/desktop-1/restart_history.jsonl

# 5. For each trip, find the full snapshot block + the post-restart child
grep -nE "watchdog\] === restart_everything|auto-restart child" \
    logs/desktop-1/fighter.log*

# 6. Pull the ~50 lines BEFORE the first DISCONNECT / trip in the chunk
#    you care about -- that's the actual failure context. Don't read
#    the whole 24h.
less logs/desktop-1/fighter.log.2026-05-22_14-30
```

What the markers mean:

- `[proxy-eyes] DISCONNECT: Dofus client closed the upstream session ...`
  -- Python side; fired the instant the Go proxy sees the Dofus<->Ankama
  TCP socket close. This is the most authoritative "I just got logged
  out" signal.
- `[proxy] game client disconnected: ...` -- same event from the Go
  side; pair it with the matching `[proxy-eyes]` line by timestamp.
- `[proxy-eyes] DISCONNECT: connection lost ...` /
  `proxy closed the event socket` -- the bot lost the proxy itself
  (proxy crashed / sudo died / network blip).
- `[proxy-eyes] STALE: no proxy events for 123s ...` -- proxy is up
  and the event hub is connected, but no game traffic for ~2 min.
  Usually the Dofus client is frozen but hasn't dropped its TCP
  session yet (e.g. spinning on a popup, anti-cheat ban screen, OOM).
- `[watchdog] no progress for Xs / Ys; map=N last_engage=...` --
  periodic "still idling" breadcrumb. One per threshold/4 seconds
  while stuck. Survives across rotated chunks so a long flat stretch
  leaves a trail, not a single line.
- `[watchdog] TRIPPED: ...` -- the no-progress threshold was crossed.
  Immediately followed by `=== restart_everything BEGIN ===`, a
  multi-line labeled snapshot dump (connected / my_id / map_id /
  fight_phase / hp / engager-filter state / etc.), a JSONL append
  to `restart_history.jsonl`, then the `=== restart_everything END
  ===` line right before `os.execv` kills this pid.
- `[combat] StaleClientError: no GTS for Ns ...` -- in-fight
  variant: Combat raised, Orchestrator caught, same restart flow
  fires. Look just above for the last successful turn number.
- `[session] auto-restart child (pid=...): ...` -- next pid's
  startup banner; pairs 1:1 with a prior `restart_everything` block
  in the previous pid's tail. Use this to chain "fresh start -> trip
  -> child -> trip -> child" across hours. Immediately followed by a
  triage block: `[session] restart_history.jsonl: N total trip(s), M
  in the last 1h` and the last 5 trips inline -- so an SSH-tail
  shows the thrash story on the first screen.
- `[watchdog] !! restart loop suspected: N trips in last 60 min ...`
  -- soft warning at `LOOP_WARN_TRIPS` trips/hour (default 3). Bot
  keeps restarting but the line is grep-magnetic.
- `[watchdog] !!! RUNAWAY RESTART LOOP: ... refusing to execv yet
  again ... exiting non-zero` -- hard stop at `LOOP_HARDSTOP_TRIPS`
  trips/hour (default 10). Process exits with code 99 after a 60s
  beat. The fix is upstream (calibration, account ban, dead
  launcher); flat-out restarting just burns the AppImage. Look at
  the previous trip blocks in fighter.log and the corresponding
  `restart_history.jsonl` rows to diagnose.

If none of the markers fire but the bot still misbehaves, the failure
is in-game logic (stuck on a popup, navigation dead-end, etc.) and
the relevant evidence is the last `[fighter]` / `[orchestrator]` /
`[combat]` lines before the symptom appeared.

### `logs/<instance>/restart_history.jsonl`

Append-only, one JSON row per watchdog trip. Written from
`Orchestrator._append_restart_history` immediately before `os.execv`.
Unlike `fighter.log` it is NOT rotated, so months of history is
greppable in one place. Each row carries:

  ts, iso, pid, reason, character, instance, screen, map_id, my_id,
  my_cell, fight_phase, my_life, my_life_max, estimated_life,
  last_engage_ts, last_fight_end_ts, last_event_ts

Quick scans:

```bash
# How many restarts in this instance, ever?
wc -l logs/desktop-1/restart_history.jsonl

# Last 5 trips with reason + map_id
tail -5 logs/desktop-1/restart_history.jsonl | \
    jq -r '"\(.iso)  map=\(.map_id)  \(.reason)"'

# Trips since yesterday morning
awk -F'"ts": ' '$2+0 > 1716508800' logs/desktop-1/restart_history.jsonl
```

## Config knobs (config.json)

- `cell_calibrations`: dict of `{<screen_name>: {origin_x, origin_y,
  cell_w, cell_h, residual_px, samples, fitted_at}}`. One entry per
  visible-environment (host vs docker desktop vs ...). Don't
  hand-edit; produced by `recalibrate_screen.py <name>`.
- `default_screen`: which key in `cell_calibrations` to use when
  neither `--screen` nor `FIGHTER_SCREEN` is supplied. The desktop
  container sets `FIGHTER_SCREEN=docker_ubuntu` in `docker-compose.yml`
  so the bot picks the docker fit automatically inside the VNC desktop.
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
- **Escape swap** (no extra knobs -- reuses `sacrid_swap_*`): runs at
  the END of the turn, after Bold + Vital + Attraction have had their
  chance, when `SWAP_AP_COST <= my_ap < DISSOLUTION_AP_COST` (i.e. we
  can't fit another Dissolution but still have 2-3 AP). If at least
  one enemy is adjacent, we pick the adjacent-enemy cell whose iso
  (u, v) L1 distance to the enemy centroid is greatest, and swap into
  it -- trading the leftover AP for a step away from the cluster
  reduces incoming damage next round. Skipped if <2 alive enemies
  (centroid degenerates) or if no adjacent enemy cell strictly beats
  our current distance from the centroid. The `sacrid_swap_min_ap`
  floor (default 6) does NOT gate this -- that floor only applies to
  the setup-swap (which needs to leave AP for Dissolution); the
  escape-swap is explicitly the low-AP branch.
- `sacrid_cast_wait_sec`: pause after each cast so the proxy `GTM`
  update with the new AP/HP arrives (default 0.8).
- `enutrof_coins_hotkey`: spell slot for Coins Throwing, default `"1"`.
  **Required** when running the Enutrof profile — Orchestrator refuses
  to start if empty. Coins Throwing is the only spell the Enutrof brain
  uses: ranged single-target, 2 AP per cast, range 1..13. The bot
  walks toward the nearest enemy only if it isn't already in range
  with LoS, then casts as many times as `my_ap` allows.
- `enutrof_coins_ap_cost`: AP per Coins Throwing cast (default 2).
- `enutrof_coins_min_range` / `enutrof_coins_max_range`: Po-distance
  window for Coins Throwing targeting (defaults 1..13).
- `enutrof_coins_post_walk_settle_sec`: extra sleep ADDED to
  `pending_settle` before the Coins hotkey press when we just walked
  (default 0.33). Same `[[post-walk-hotkey-drop]]` mechanism as the
  Sacrieur spells.
- `enutrof_cast_wait_sec`: pause after each Coins cast for the proxy
  `GTM` update to arrive (default 0.8).
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
- `restart_clicks`: dict of `{<screen_name>: {play_button: [x, y],
  calibrated_at}}`. Pixel position of the Ankama Launcher PLAY button
  used by `restart_dofus.py` (the standalone client-restart tool).
  Captured by `calibrate_play_button.py <name>`; each docker desktop
  / host environment needs its own entry. Missing entry →
  `restart_dofus.py` hard-errors with a pointer to the calibration
  script.
- `fight_ui_dismiss_clicks`: dict of `{<screen_name>:
  {collapse_turn_order: [x, y], calibrated_at}}`. Pixel position of
  the in-fight turn-order bar `<>` collapse toggle. `Orchestrator`
  clicks it on every `on_fight_engaged` so the expanded bar doesn't
  overlap clickable map cells in the top-right. Captured by
  `calibrate_fight_ui_dismiss.py <name>` (run during a fight, so the
  bar is visible). Missing entry → one-shot warning + no-op.
  Assumption: Dofus resets the bar to expanded at the start of each
  fight; if it turns out to be sticky across fights, add a per-session
  guard inside `Orchestrator._dismiss_fight_ui`.
- **In-fight cell click offsets (no config knob)**: a permanent
  bottom-right UI panel covers cells 462 and 463 during fights. Their
  geometric center lands inside the panel, so `dofus.cell_grid.
  cell_to_screen_fight()` applies a hardcoded offset
  (`FIGHT_CELL_CLICK_OFFSETS_PCT`: 462 → 25% of cell_w left, 463 →
  25% of cell_h up) for the fight-only click sites (`cast_at_cell` and
  all of `fighter/walking.py`). Out-of-fight `click_cell` keeps using
  `cell_to_screen` directly — the panel isn't drawn then.
- `turn_wait_timeout_sec`: seconds Combat waits for our next `GTS`
  before declaring the turn stale (default 180). The server's
  enforced per-turn timer is 29s, so 180s covers ~6 stuck enemy
  turns or two badly-frozen single turns. On stale-turn Combat raises
  `StaleClientError` (from `fighter/watchdog.py`); `Orchestrator.
  _tick_combat` catches it and routes to `_restart_everything` --
  same path the idle `ProgressWatchdog` uses. No more `sys.exit(1)`
  pointing the operator at the manual script -- the bot self-heals.
- `watchdog_idle_no_progress_sec`: out-of-fight no-progress threshold
  in seconds (default 300). `ProgressWatchdog` watches the snapshot
  signature `(map_id, last_fight_engage_ts, last_fight_end_ts,
  estimated_life())`. `estimated_life()` ticks continuously during
  sit-regen (server ILS rate), so a bot legitimately healing for 10+
  minutes does NOT false-positive -- the metric resets the timer.
  Trip = kill + relaunch Dofus client + `os.execv` `main.py
  --auto-reuse`. Bump if real "i'm just thinking" plateaus (heavy
  navigation pathing on big graphs?) start tripping.

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

## Working on this repo with Claude

- **Plan files end with an executive summary.** Every plan written
  under `~/.claude/plans/<slug>.md` (from Claude Code's plan mode)
  must close with a `## Executive summary` section: 3-5 bullets that
  name the concrete files / functions changed and the single most
  important caveat. The user reads the bottom first to approve --
  skipping the summary forces them to skim the whole plan.
