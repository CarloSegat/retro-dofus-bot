"""Proxy-driven Sacrieur fighter (Marx-Rockfeller / berlinthree).

Tri-state loop mirroring the proxy's fight_phase:
  idle      -> engage nearest valid mob, or navigate NSEW if none
  placement -> ready up, place starting cells
  combat    -> run_combat_sacrid (buff -> walk -> Dissolution -> pass)

Strategy: one Dissolution per turn (1/turn cap). Dissolution is a
self-cast water AoE that hits the 4 edge-adjacent cells, so we just
need any live enemy adjacent -- no target lock, re-pick nearest each
turn and re-evaluate after walking. Strength Punishment self-buff is
recast every sacrid_buff_cooldown_turns turns (5 by default -- the
in-game spell cooldown) on any turn the nearest enemy is within
sacrid_buff_max_dist (the buff only triggers on damage taken;
pointless if mobs are too far to hit us before it expires).

Usage:
  python3 -u main.py [--min-hp HP] [--max-group-size N]

  --min-hp:         wait until HP >= this (capped at max) before engaging.
                    If no "As" stats packet has been seen, refuse rather
                    than attack blind.
  --max-group-size: skip mob groups with more than N members. 0 = no cap.

Pre-reqs: proxy running on 127.0.0.1:9999, config.json has
cell_calibration + sacrid_dissolution_hotkey. See CLAUDE.md for config
knobs.
"""
import json
import random
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import mss

from dofus.cell_grid import a_star, cell_distance, cell_to_xy, line_of_sight, neighbors, on_map
from fight import pass_turn
from dofus.map_data import (
    DIRECTION_WORLD_DELTA,
    OPPOSITE_DIRECTION,
    build_world_index,
    load_all as load_map_data,
    safe_directions,
    save as save_map_data,
    target_map_id,
)
from mouse_keyboard import click_at, click_at_focused, press, press_focused, type_text_focused
from dofus.proxy_client import ProxyState
from utils import CFG, make_ctx
from vision import ensure_safe_to_resume

MAP_DATA = load_map_data()  # {map_id: {"world", "map_id", "cells", "obstacles", ...}}
MAP_BY_WORLD = build_world_index(MAP_DATA)  # {(world_x, world_y): entry}

PROXY_ADDR = "127.0.0.1:9999"

DISSOLUTION_HOTKEY = CFG.get("sacrid_dissolution_hotkey")
DISSOLUTION_AP_COST = int(CFG.get("sacrid_dissolution_ap_cost", 4))
# Bow weapon: press hotkey, click an enemy cell within [min, max] Po
# range. Can NOT hit adjacent cells (min_range > 1). Same spell-aim
# click contract as Dissolution -- xdotool spell_click, not pyautogui.
# LoS gated against the same static_obstacles the walker uses (plus
# live entities other than the target).
BOW_HOTKEY = CFG.get("sacrid_bow_hotkey", "0")
BOW_AP_COST = int(CFG.get("sacrid_bow_ap_cost", 4))
BOW_MIN_RANGE = int(CFG.get("sacrid_bow_min_range", 2))
BOW_MAX_RANGE = int(CFG.get("sacrid_bow_max_range", 6))
# Extra settle ADDED to pending_settle before the first bow shot of
# a turn when we just walked. Empirically, the bow hotkey press was
# dropped if it landed before the walk animation fully released --
# the spell-aim mode never armed, and the follow-up click registered
# as a plain move-click. Spells don't need this (Dissolution uses
# pending_settle directly and works); the bow is the symptom that
# pending_settle's floor is a hair too short for weapon-arming.
BOW_POST_WALK_EXTRA_SETTLE_SEC = float(CFG.get("sacrid_bow_post_walk_settle_sec", 0.33))
# Strength Punishment self-buff: hotkey + click own cell.
# Set sacrid_buff_enabled=false to skip the buff entirely (e.g. when
# farming low-level mobs where the AP is better spent on damage).
BUFF_ENABLED = bool(CFG.get("sacrid_buff_enabled", True))
BUFF_HOTKEY = CFG.get("sacrid_buff_hotkey", "3")
BUFF_AP_COST = int(CFG.get("sacrid_buff_ap_cost", 3))
# Skip buff if nearest enemy > this many Po away -- buff triggers on
# damage taken, and would expire before they close the gap.
BUFF_MAX_DIST = int(CFG.get("sacrid_buff_max_dist", 6))
# Strength Punishment in-game cooldown, in our turns. If cast on turn T,
# the next cast is allowed on turn T + BUFF_COOLDOWN_TURNS. We re-cast
# as soon as it's available (assuming the distance gate also passes).
BUFF_COOLDOWN_TURNS = int(CFG.get("sacrid_buff_cooldown_turns", 5))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
WALK_WAIT_SEC = float(CFG.get("sacrid_walk_wait_sec", 2.0))
WALK_STEP_WAIT_SEC = float(CFG.get("sacrid_walk_step_wait_sec", 1.0))
# Shorter per-click movement-wait for the post-Dissolution follow-up
# walk. After an AoE cast the bot is frequently surrounded by surviving
# mobs and the server silently rejects every walk click -- there's no
# negative-ack to short-circuit on, only the absence of a GA;1; packet,
# so the only lever we have is to wait less before declaring "blocked".
# Server confirms a valid walk in <100ms; 0.6s is enough margin without
# burning the usual 1s + retry + 0.5s settle on every dead click.
WALK_STEP_FAST_FAIL_SEC = float(CFG.get("sacrid_walk_step_fast_fail_sec", 0.6))
WALK_MAX_STEPS = int(CFG.get("sacrid_walk_max_steps", 6))
# Hit-and-run detector: enemies (tofus) with high MP rush in, hit, and
# retreat each turn. Chasing them with a 3-MP Sacrieur never closes the
# gap. Sample the distance to the nearest alive enemy at the START of
# each of our turns (before we move). If the last TOFU_REQUIRED_CYCLES
# turn-start distances are all > TOFU_THRESHOLD AND that sequence is
# not strictly decreasing, flip into hold-position mode for the rest
# of the fight: walk only when MP+AP let us reach attack range AND
# cast this turn; skip the follow-up positioning walk entirely.
TOFU_THRESHOLD = int(CFG.get("tofu_detect_threshold", 4))
TOFU_REQUIRED_CYCLES = int(CFG.get("tofu_detect_required_cycles", 3))
# Dofus retro silently drops walk clicks issued mid-animation; settle
# between steps to avoid false "obstacle" entries.
WALK_STEP_SETTLE_SEC = float(CFG.get("sacrid_walk_step_settle_sec", 0.5))
# Floor on the full-walk pending_settle returned to the caller. Per-step
# multiplier under-estimated the animation tail for 2-step walks -- the
# Dissolution click then landed mid-animation and got dropped. 1.2s
# covers ~3 cells of animation at 0.4s/cell.
FULL_WALK_SETTLE_FLOOR_SEC = float(CFG.get("full_walk_settle_floor_sec", 1.2))

# Settle after GTS<myID> so the Dofus client finishes rendering the new
# turn before our first click (else clicks get eaten by the previous
# actor's end-of-turn animation).
TURN_START_SETTLE_SEC = float(CFG.get("turn_start_settle_sec", 1.5))
TURN_WAIT_TIMEOUT_SEC = float(CFG.get("turn_wait_timeout_sec", 90.0))

ENGAGE_TIMEOUT = 5.0
COMBAT_START_TIMEOUT = 35.0
IDLE_POLL_SEC = 0.5
STATUS_LOG_SEC = 5.0
MAP_CHANGE_TIMEOUT = 20.0
# How long a map stays marked "recently empty" after we find no valid
# mob on it. Prevents ping-ponging into a dead-end neighbour whose only
# safe exit leads back to us. Roughly mob respawn time.
EMPTY_MAP_RESPAWN_SEC = float(CFG.get("empty_map_respawn_sec", 240.0))

HP_WAIT_TIMEOUT = 300.0
HP_POLL_SEC = 1.0
HP_LOG_SEC = 10.0

STATS_FILE = Path(__file__).parent / "data" / "stats.json"


def append_fight_stats(mob_size, duration_sec):
    """Append one {mob_size, fight_duration} record to data/stats.json.

    Reads the existing JSON array, appends, rewrites via tmp+rename so a
    crash mid-write can't corrupt the file. Resets to [] if the file is
    missing or unreadable as JSON."""
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if STATS_FILE.exists():
        try:
            with STATS_FILE.open() as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = []
        except (OSError, json.JSONDecodeError):
            records = []
    records.append({"mob_size": mob_size, "fight_duration": round(duration_sec, 2)})
    tmp = STATS_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(records, f, indent=2)
    tmp.replace(STATS_FILE)


def load_cal():
    cal = CFG.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        sys.exit(1)
    return cal


def cell_to_screen(cell, cal):
    return cell_to_xy(cell, cal["origin_x"], cal["origin_y"], cal["cell_w"], cal["cell_h"])


def wait_for(state, predicate, timeout, poll=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(state.snapshot()):
            return True
        time.sleep(poll)
    return False


def wait_for_my_turn(state, my_id, last_turn_n, timeout):
    """Block until the proxy reports GTS<my_id> with turn_number > last_turn_n.

    Returns the new turn_number on success, or 0 if combat ended or we
    timed out. Polls at 50ms so the post-GTS settle delay is the dominant
    contributor to "time until first click" -- not our polling cadence."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        snap = state.snapshot()
        if not snap.in_combat:
            return 0
        if snap.turn_actor == my_id and snap.turn_number > last_turn_n:
            return snap.turn_number
        time.sleep(0.05)
    return 0


class TurnDistanceTracker:
    """Detects hit-and-run "tofu-like" enemies via turn-start distances.

    Called once per our turn-start with the distance to the nearest
    alive enemy BEFORE we move. That snapshot is the cycle's "max" --
    where the enemy ended up after retreating. If the last `required`
    samples are all > `threshold` AND the sequence is not strictly
    decreasing, flip tofu_detected (kiter -- we aren't closing on
    them). The flag never clears -- caller builds a fresh tracker per
    fight. Sampling mid-cycle (during enemy turns or right after our
    pass) would conflate enemy approach distance with our own post-
    move position and corrupt the signal.
    """

    def __init__(self, threshold, required_cycles):
        self.threshold = threshold
        self.required = required_cycles
        self.history = []
        self.tofu_detected = False

    def observe_turn_start(self, dist):
        """Record start-of-our-turn distance to nearest enemy. May flip
        tofu_detected. Returns the recorded distance (or None if invalid)."""
        if dist is None or dist <= 0:
            return None
        self.history.append(dist)
        if self.tofu_detected:
            return dist
        if len(self.history) >= self.required:
            recent = self.history[-self.required:]
            all_high = all(d > self.threshold for d in recent)
            strictly_decreasing = all(
                recent[i + 1] < recent[i] for i in range(len(recent) - 1)
            )
            if all_high and not strictly_decreasing:
                self.tofu_detected = True
        return dist


def make_exchange_dismiss_callback():
    """Returns a proxy on_event callback that schedules an Esc 1s after
    any exchange_open event.

    The bot sometimes click-engages a mob whose cell coincides with (or
    is masked by) a player in merchant mode -- the click registers as
    "open shop" instead of "engage", and the shop window blocks every
    follow-up action until dismissed. We can't tell from the click
    alone whether the shop opened, but the proxy sees ECK<kind>|<id>
    on the wire. 1 second is enough for the shop UI to fully render
    before Esc reaches it. Esc is sent via xdotool to the Dofus window,
    not pyautogui, so EscStop's pynput listener won't see it (XSendEvent
    sets send_event; pynput filters those)."""
    def cb(ev):
        if ev.get("type") != "exchange_open":
            return
        kind = ev.get("kind")
        target = ev.get("target")
        def fire():
            print(f"[fighter] exchange_open (kind={kind} target={target}) -> Esc")
            try:
                press_focused("Escape")
            except Exception as e:
                print(f"[fighter] exchange Esc failed: {e}")
        threading.Timer(1.0, fire).start()
    return cb


def sit_for_regen(state):
    """Send /sit via chat and mark ProxyState.sitting=True.

    Pre-condition: we are currently STANDING. /sit is a toggle so the
    caller must know this -- combat auto-stands, so by the time
    wait_for_hp invokes us we're known standing. Cleared automatically
    on fight_engage."""
    print("[fighter] /sit to regen faster")
    press_focused("Return")
    time.sleep(0.3)
    type_text_focused("/sit")
    time.sleep(0.3)
    press_focused("Return")
    state.set_sitting(True)


def wait_for_hp(state, min_hp):
    """Block until estimated HP >= min(min_hp, my_life_max). Returns False
    on timeout or if we enter a fight mid-wait.

    HP estimate = anchor (last As packet) + elapsed/regen_ms (last ILS
    packet). Server only emits As on stat changes, so without
    extrapolation we'd freeze at post-fight HP forever.

    Sit-once: /sit fires the first iteration below threshold and stays
    on until fight_engage stands us back up. Refusing to engage when
    my_life_max == 0 (no As ever seen) is intentional -- engaging blind
    is the bug this function prevents."""
    deadline = time.time() + HP_WAIT_TIMEOUT
    announced = False
    last_log = 0.0
    sat_down = False
    while time.time() < deadline:
        snap = state.snapshot()
        if snap.in_fight:
            print(f"[fighter] entered fight while waiting for HP; aborting wait")
            return False
        if snap.my_life_max > 0:
            cap = min(min_hp, snap.my_life_max)
            est = snap.estimated_life()
            eff = snap.effective_regen_ms()
            rate_str = (f"regen={eff}ms/hp (sitting, raw={snap.my_life_regen_ms})"
                        if snap.sitting else f"regen={eff}ms/hp")
            if est >= cap:
                if announced:
                    print(f"[fighter] HP ~{est}/{snap.my_life_max} >= {cap}; engaging "
                          f"(anchor={snap.my_life}, {rate_str})")
                return True
            # Below threshold -> sit for regen. One-shot: don't re-sit
            # on every iteration.
            if not sat_down and not snap.sitting:
                sit_for_regen(state)
                sat_down = True
                continue
            if not announced:
                print(f"[fighter] HP ~{est}/{snap.my_life_max} below threshold {cap}; "
                      f"waiting (anchor={snap.my_life}, {rate_str})")
                announced = True
                last_log = time.time()
            elif time.time() - last_log >= HP_LOG_SEC:
                print(f"[fighter] still regenerating: HP ~{est}/{snap.my_life_max} (need {cap})")
                last_log = time.time()
        else:
            if not announced or time.time() - last_log >= HP_LOG_SEC:
                print(f"[fighter] no HP info from proxy yet (no 'As' packet seen); "
                      f"holding engage. Complete any stat change to populate it.")
                announced = True
                last_log = time.time()
        time.sleep(HP_POLL_SEC)
    snap = state.snapshot()
    print(f"[fighter] HP wait timed out after {HP_WAIT_TIMEOUT}s at "
          f"~{snap.estimated_life()}/{snap.my_life_max}; refusing to engage blind")
    return False


def nearest_mob(snap, ghosts=(), max_group_size=0):
    """(distance, cell, mob) for the closest valid mob group, or None.

    `ghosts`: (cell, group_id) tuples already tried and failed on this
    map -- skipped. Catches groups despawned by other players, missed
    GM|-, etc; the proxy itself drops engaged groups from s.mobs.

    `max_group_size > 0`: skip groups with > N members.

    Mobs whose `move_ends_at_ms` is in the future are skipped: the proxy
    re-keys s.mobs to the destination cell the instant it sees GA0;1;
    but Dofus needs ~steps*400ms to animate the sprite there. A click
    on the destination during the animation registers as a walk, not
    an engage. Wait one IDLE_POLL tick and re-pick.

    When my_cell is 0 (proxy just attached) we return an arbitrary mob
    with distance=-1 so the caller still tries to engage."""
    ghost_set = set(ghosts)
    now_ms = int(time.time() * 1000)
    candidates = [
        (c, m) for c, m in snap.mobs.items()
        if (c, m.group_id) not in ghost_set
        and (max_group_size <= 0 or len(m.members) <= max_group_size)
        and m.move_ends_at_ms <= now_ms
    ]
    if not candidates:
        return None
    if snap.my_cell == 0:
        cell, mob = candidates[0]
        return (-1, cell, mob)
    items = [(cell_distance(snap.my_cell, c), c, m) for c, m in candidates]
    items.sort(key=lambda t: t[0])
    return items[0]


def my_fight_cell(snap):
    """Cell of our own character. In fight, the proxy's `my_cell` does NOT
    update from GTM (only from GA;1; movement), so prefer fight_entities."""
    me = snap.fight_entities.get(snap.my_id) if snap.my_id else None
    if me and me.cell > 0:
        return me.cell
    return snap.my_cell


def alive_enemies(snap):
    """Alive enemies in current fight, sorted by Po distance from us."""
    me_cell = my_fight_cell(snap)
    enemies = [
        e for e in snap.fight_entities.values()
        if e.alive and e.id != snap.my_id and e.cell > 0
    ]
    enemies.sort(key=lambda e: cell_distance(me_cell, e.cell) if me_cell else 0)
    return enemies


def _path_repr(path, max_cells=10):
    """Compact log-friendly repr of an A* path. Truncates very long paths."""
    if path is None:
        return "None"
    if len(path) <= max_cells:
        return str(path)
    return f"{path[:max_cells]}+{len(path) - max_cells}more"


def pick_next_step(me_cell, target_cell, snap, recent_failed, static_obstacles):
    """Pick one cell to step into toward `target_cell`. Returns None if
    no walkable neighbour strictly improves Po distance.

    Single-cell-at-a-time: plan with A*, take one step, re-check proxy
    state, re-plan. Two-tier blocked set is the key trick -- dynamic
    obstacles (live entities) are *not* in the A* plan (one mob squatting
    in the optimal corridor would pin us) but we still veto the
    immediate next step if it walks into a live mob, falling back to a
    greedy neighbour pick."""
    static = set(static_obstacles)
    rf = set(recent_failed)
    dynamic = {
        e.cell for e in snap.fight_entities.values()
        if e.alive and e.cell > 0 and e.cell != target_cell
    }

    plan_blocked = (static | rf) - {target_cell}
    path = a_star(me_cell, target_cell, blocked=plan_blocked)
    if path and len(path) >= 2 and path[1] not in dynamic:
        print(f"    [pick_step] src=astar me={me_cell} target={target_cell} "
              f"path={_path_repr(path)} pick={path[1]} "
              f"static={len(static)} rf={sorted(rf) or '[]'} "
              f"dyn={sorted(dynamic) or '[]'}")
        return path[1]

    astar_note = (f"path={_path_repr(path)} but path[1]={path[1]} blocked by dynamic"
                  if path and len(path) >= 2 else f"path={_path_repr(path)}")

    # Fallback: walk one tile toward target, dodging live entities. Matches
    # the pre-A* greedy behaviour so a mid-corridor mob doesn't pin us.
    current_dist = cell_distance(me_cell, target_cell)
    cands = []
    for n in neighbors(me_cell):
        if cell_distance(n, me_cell) != 1:
            continue  # off-grid wrap
        if not on_map(n):
            continue  # off the playable diamond
        if n in static or n in dynamic or n in rf:
            continue
        d = cell_distance(n, target_cell)
        if d >= current_dist:
            continue
        cands.append((d, n))
    if not cands:
        print(f"    [pick_step] src=NONE me={me_cell} target={target_cell} "
              f"({astar_note}) no greedy candidate "
              f"(current_dist={current_dist} static={len(static)} "
              f"rf={sorted(rf) or '[]'} dyn={sorted(dynamic) or '[]'})")
        return None
    cands.sort()
    print(f"    [pick_step] src=greedy me={me_cell} target={target_cell} "
          f"({astar_note}) pick={cands[0][1]} "
          f"(current_dist={current_dist} candidates={cands})")
    return cands[0][1]


def pick_retreat_step(me_cell, away_from, snap, recent_failed, static_obstacles):
    """Greedy mirror of pick_next_step: pick a neighbor that strictly
    INCREASES Po distance from `away_from`. Returns None if no walkable
    neighbour does. No A* -- we don't have a destination cell, just a
    direction to flee."""
    static = set(static_obstacles)
    rf = set(recent_failed)
    dynamic = {
        e.cell for e in snap.fight_entities.values()
        if e.alive and e.cell > 0 and e.cell != me_cell
    }
    current_dist = cell_distance(me_cell, away_from)
    cands = []
    for n in neighbors(me_cell):
        if cell_distance(n, me_cell) != 1:
            continue
        if not on_map(n):
            continue
        if n in static or n in dynamic or n in rf:
            continue
        d = cell_distance(n, away_from)
        if d <= current_dist:
            continue
        cands.append((-d, n))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


def walk_away(away_from, state, cal, static_obstacles, max_steps):
    """Step-by-step retreat from `away_from` (a cell, e.g. the nearest
    enemy). Picks neighbours that strictly increase Po distance. Stops
    at max_steps, no MP, no valid neighbour, or movement failure.
    Returns (me_cell, mp_remaining, pending_settle)."""
    initial = state.snapshot()
    me0 = initial.fight_entities.get(initial.my_id)
    estimated_mp = me0.mp if me0 else 0
    me_cell = my_fight_cell(initial)
    recent_failed = set()
    steps_taken = 0
    moved_any = False

    while steps_taken < max_steps and estimated_mp > 0 and me_cell:
        step = pick_retreat_step(me_cell, away_from, state.snapshot(),
                                 recent_failed, set(static_obstacles))
        if step is None:
            print(f"  no retreat neighbour from {me_cell} "
                  f"(away from {away_from})")
            break
        if steps_taken > 0:
            time.sleep(WALK_STEP_SETTLE_SEC)
        sx, sy = cell_to_screen(step, cal)
        print(f"  RETREAT {steps_taken + 1}/{max_steps} from {me_cell} -> "
              f"cell={step} ({sx},{sy}) [mp_left~{estimated_mp}]")
        before = me_cell
        click_at(sx, sy)
        moved = _wait_movement(state, before, WALK_STEP_WAIT_SEC)
        if not moved:
            print(f"    no movement from {before} -> cell={step} ({sx},{sy}); "
                  f"settling {WALK_STEP_SETTLE_SEC}s and retrying")
            time.sleep(WALK_STEP_SETTLE_SEC)
            before = my_fight_cell(state.snapshot()) or before
            click_at(sx, sy)
            moved = _wait_movement(state, before, WALK_STEP_WAIT_SEC)
        steps_taken += 1
        if moved:
            moved_any = True
            new_cell = my_fight_cell(state.snapshot())
            mp_used = cell_distance(before, new_cell) if new_cell else 1
            estimated_mp -= mp_used
            print(f"    landed {new_cell}; mp_used={mp_used} mp_left~{estimated_mp}")
            me_cell = new_cell
            continue
        print(f"  retreat step from {before} -> cell={step} ({sx},{sy}) "
              f"failed twice; excluding for this turn")
        recent_failed.add(step)

    pending = WALK_STEP_SETTLE_SEC if moved_any else 0.0
    return me_cell, max(estimated_mp, 0), pending


def place_starting_cells(snap, cal):
    """Click the saved starting cells for this map (one click per cell in
    saved order). No-op if no entry for snap.map_id in MAP_DATA."""
    entry = MAP_DATA.get(snap.map_id)
    if not entry:
        return
    cells = entry.get("cells") or []
    if not cells:
        return
    print(f"[fighter] placement: clicking {len(cells)} starting cell(s) "
          f"for map={snap.map_id} world={entry.get('world')}")
    for cell in cells:
        x, y = cell_to_screen(cell, cal)
        print(f"  start_click cell={cell} -> ({x},{y})")
        click_at(x, y)
        time.sleep(0.3)


def prune_obstacles_from_entities(map_id, snap):
    """Drop any obstacle cell now occupied by a live entity (us or enemies),
    plus any saved player start cell. Calibration sometimes mis-clicks
    mob spawn cells as obstacles, which then makes A* return None when
    we land on one (the start-in-blocked guard) or forces multi-cell
    detours around cells nothing actually blocks.

    No-op if nothing changed. Mutates the in-memory MAP_DATA entry and
    persists to the JSON file."""
    entry = MAP_DATA.get(map_id)
    if not entry:
        return
    obstacles = entry.get("obstacles") or []
    if not obstacles:
        return
    occupied = {e.cell for e in snap.fight_entities.values()
                if e.alive and e.cell > 0}
    occupied |= {c for c in (entry.get("cells") or []) if c > 0}
    if not occupied:
        return
    drop = [c for c in obstacles if c in occupied]
    if not drop:
        return
    new_obs = [c for c in obstacles if c not in occupied]
    entry["obstacles"] = new_obs
    save_map_data(entry)
    print(f"  pruned {len(drop)} mis-calibrated obstacle(s) for map={map_id} "
          f"(occupied by start/entity): {sorted(drop)}")


def cast_dissolution(my_cell, cal):
    """Press Dissolution hotkey, then click own cell (self-cast AoE that
    damages the 4 edge-adjacent cells).

    Uses `spell_click` (xdotool) rather than the plain pyautogui click:
    in spell-aim mode pyautogui's click is silently dropped sometimes,
    leaving the spell armed without firing -- bot then passes the turn
    with full AP. See CLAUDE.md."""
    x, y = cell_to_screen(my_cell, cal)
    print(f"  CAST Dissolution hotkey={DISSOLUTION_HOTKEY!r} self_cell={my_cell} -> ({x},{y})")
    press(DISSOLUTION_HOTKEY)
    time.sleep(0.4)  # let Dofus enter spell-aim mode and show the reticle
    click_at_focused(x, y)


def cast_strength_punishment(my_cell, cal):
    """Press Strength Punishment hotkey, then click own cell (self-buff).

    Same spell-aim-mode click rule as `cast_dissolution`."""
    x, y = cell_to_screen(my_cell, cal)
    print(f"  CAST Strength Punishment hotkey={BUFF_HOTKEY!r} self_cell={my_cell} -> ({x},{y})")
    press(BUFF_HOTKEY)
    time.sleep(0.4)
    click_at_focused(x, y)


def cast_bow(target_cell, cal):
    """Press bow hotkey, then click an enemy cell. Same spell-aim click
    contract as the Sacrieur spells -- xdotool, not pyautogui."""
    x, y = cell_to_screen(target_cell, cal)
    print(f"  CAST Bow hotkey={BOW_HOTKEY!r} target_cell={target_cell} -> ({x},{y})")
    press(BOW_HOTKEY)
    time.sleep(0.4)
    click_at_focused(x, y)


def pick_bow_target(snap, me_cell, static_obstacles, debug=False):
    """Nearest alive enemy in [BOW_MIN_RANGE, BOW_MAX_RANGE] with LoS, or None.

    LoS blockers = static_obstacles (same set the walker avoids) plus
    every other live entity. The target's own cell is excluded from
    blockers -- you can shoot a mob standing on a square.

    When `debug=True` and no candidate is found, prints one line per
    rejected enemy explaining why (out-of-range / LoS blocked) so the
    "bow silently did nothing" case is diagnosable from the trace."""
    if not me_cell:
        if debug:
            print("  bow: no target -- me_cell unknown")
        return None
    other_alive = {
        e.cell for e in snap.fight_entities.values()
        if e.alive and e.cell > 0 and e.id != snap.my_id
    }
    candidates = []
    rejections = []
    for e in snap.fight_entities.values():
        if not e.alive or e.id == snap.my_id or e.cell <= 0:
            continue
        d = cell_distance(me_cell, e.cell)
        if d < BOW_MIN_RANGE or d > BOW_MAX_RANGE:
            if debug:
                rejections.append(
                    f"id={e.id} cell={e.cell} out-of-range(dist={d}, "
                    f"need {BOW_MIN_RANGE}..{BOW_MAX_RANGE})"
                )
            continue
        blockers = set(static_obstacles) | (other_alive - {e.cell})
        if not line_of_sight(me_cell, e.cell, blockers):
            if debug:
                mob_blockers = sorted(other_alive - {e.cell})
                rejections.append(
                    f"id={e.id} cell={e.cell} dist={d} LoS-blocked "
                    f"(other_alive={mob_blockers}, "
                    f"static_obstacles={len(static_obstacles)})"
                )
            continue
        candidates.append((d, e))
    if not candidates:
        if debug:
            if rejections:
                print(f"  bow: no target -- {len(rejections)} enemies considered:")
                for r in rejections:
                    print(f"    rejected {r}")
            else:
                print("  bow: no target -- no alive enemies on the field")
        return None
    candidates.sort(key=lambda t: t[0])
    return candidates[0][1]


def fire_bow_burst(state, cal, my_ap, me_cell, static_obstacles):
    """Fire bow shots until AP < cost, no eligible target, or combat ends.

    Re-pulls the snapshot before each pick so mobs killed by prior
    shots in this burst drop out. Returns (updated_ap, shots_fired)."""
    shots = 0
    while my_ap >= BOW_AP_COST:
        snap = state.snapshot()
        if not snap.in_combat:
            return my_ap, shots
        # debug=True only on the first iteration -- after a successful
        # shot the candidate disappears as expected (mob died or AP ran
        # out), no need to spam rejections every burst.
        target = pick_bow_target(snap, me_cell, static_obstacles,
                                 debug=(shots == 0))
        if target is None:
            return my_ap, shots
        d = cell_distance(me_cell, target.cell)
        print(f"  bow: targeting id={target.id} cell={target.cell} dist={d} "
              f"ap_before={my_ap}")
        cast_bow(target.cell, cal)
        time.sleep(CAST_WAIT_SEC)
        my_ap -= BOW_AP_COST
        shots += 1
    return my_ap, shots


def _wait_movement(state, before, timeout):
    """True iff my_fight_cell moves away from `before` within `timeout`."""
    return wait_for(
        state,
        lambda s, b=before: my_fight_cell(s) != b and my_fight_cell(s) > 0,
        timeout,
    )


def try_full_walk(target_cell, state, cal, static_obstacles=(), mp_override=None,
                  walk_wait_sec=None):
    """One-click walk that spends all current MP toward `target_cell`.

    Returns (success, me_cell, mp_remaining, pending_settle_sec). On
    failure (no path, or Dofus didn't budge), me_cell/mp_remaining are
    the pre-click values so the caller can drop straight into step-by-step
    and pending_settle_sec is 0.

    pending_settle_sec is the animation budget the *caller* should sleep
    before issuing the next click (typically a spell cast). Skip it when
    the next action is the pass-turn hotkey -- key events don't care
    about walk animation overlap, only clicks do.

    Plans with STATIC obstacles only (no live entities) -- matches
    pick_next_step's reasoning: one mob sitting on the corridor would
    null the A* path and pin us forever. We then walk the destination
    back along the path past any live entity, so the chosen cell is
    walkable now. Dofus's own pathing routes around the remaining
    mobs; if it can't (no path within MP), no movement fires and we
    fall through to step-by-step.

    Destination is path[min(MP, dist-1)] -- one short of the target so
    we land adjacent for the spell cast, not on the target itself.

    `mp_override`: pass when you've already walked this turn -- the
    proxy doesn't refresh MP in fight_entities until the next GTM at
    turn start, so a state-read mid-turn returns stale MP.

    `walk_wait_sec`: override the post-click movement-wait timeout.
    Default WALK_STEP_WAIT_SEC; lower it for fast-fail walks (e.g.
    post-Dissolution follow-up) where a missing GA;1; should be treated
    as "blocked" sooner.
    """
    walk_wait = walk_wait_sec if walk_wait_sec is not None else WALK_STEP_WAIT_SEC
    snap = state.snapshot()
    me = snap.fight_entities.get(snap.my_id)
    mp = mp_override if mp_override is not None else (me.mp if me else 0)
    me_cell = my_fight_cell(snap)
    if mp <= 0 or not me_cell:
        return False, me_cell, mp, 0.0

    obs_set = set(static_obstacles)
    path = a_star(me_cell, target_cell, blocked=obs_set - {target_cell})
    print(f"  [full_walk] me={me_cell} target={target_cell} mp={mp} "
          f"static_obstacles={len(obs_set)} path={_path_repr(path)}")
    # path[0] is me_cell, path[-1] is target. We need at least one cell
    # between them to walk to (path[-2] = adjacent to target).
    if not path or len(path) < 3:
        print(f"    [full_walk] no usable path (len={len(path) if path else 0}); "
              f"caller falls back to step-by-step")
        return False, me_cell, mp, 0.0

    max_steps = min(mp, len(path) - 2)
    if max_steps <= 0:
        return False, me_cell, mp, 0.0

    # Pull the destination back past any live entity (other than the
    # target itself) so we click a walkable cell. With many mobs this
    # may shrink the walk, but anything beats clicking onto a mob.
    dynamic = {
        e.cell for e in snap.fight_entities.values()
        if e.alive and e.cell > 0 and e.cell != target_cell
    }
    pulled = 0
    while max_steps > 0 and path[max_steps] in dynamic:
        max_steps -= 1
        pulled += 1
    if pulled:
        print(f"    [full_walk] pulled dest back {pulled} step(s) past "
              f"dynamic entities={sorted(dynamic)}")
    if max_steps <= 0:
        print(f"    [full_walk] entire path blocked by dynamic entities; "
              f"caller falls back to step-by-step")
        return False, me_cell, mp, 0.0

    dest_cell = path[max_steps]
    # Belt-and-braces: A* should never put a static obstacle on the path,
    # but if it did (bug, stale data, target itself an obstacle), flag it
    # in the log -- the click will fail and we want it obvious why.
    if dest_cell in obs_set:
        print(f"    [full_walk] WARNING dest_cell={dest_cell} is in static "
              f"obstacles ({sorted(obs_set & set(path))} appear on path); "
              f"clicking anyway, expect Dofus to reject")
    sx, sy = cell_to_screen(dest_cell, cal)
    print(f"  FULL WALK from {me_cell} -> cell={dest_cell} ({sx},{sy}) "
          f"[mp={mp} planned_steps={max_steps} target={target_cell}]")
    click_at(sx, sy)

    moved = _wait_movement(state, me_cell, walk_wait)
    if not moved:
        print(f"    full walk from {me_cell} -> cell={dest_cell} ({sx},{sy}) "
              f"produced no movement in {walk_wait}s; falling back to step-by-step")
        return False, me_cell, mp, 0.0

    # Per-planned-step animation budget the caller should sleep before
    # the next click (typically the Dissolution cast). The proxy updates
    # my_cell from the path packet immediately but the client animates
    # ~300-500ms per cell, so a click landing mid-animation gets dropped.
    # Floor at FULL_WALK_SETTLE_FLOOR_SEC: short walks (2 cells * 0.3 =
    # 0.6s) weren't waiting long enough -- Dissolution casts dropped
    # because the spell-aim click landed while the walk animation was
    # still finishing. 1.2s covers ~3 cells of animation at 0.4s each.
    pending_settle = max(WALK_STEP_SETTLE_SEC * max_steps, FULL_WALK_SETTLE_FLOOR_SEC)
    new_cell = my_fight_cell(state.snapshot()) or me_cell
    mp_used = cell_distance(me_cell, new_cell)
    remaining = mp - mp_used
    print(f"    full walk landed {new_cell} "
          f"(mp_used={mp_used} mp_left~{remaining} "
          f"pending_settle={pending_settle:.2f}s)")
    return True, new_cell, remaining, pending_settle


def walk_toward(target_cell, state, cal, static_obstacles=(), mp_override=None,
                fast_fail=False):
    """Walk toward `target_cell`. Returns
    (me_cell, my_ap, mp_remaining, pending_settle_sec).

    pending_settle_sec is the animation budget the caller should sleep
    before the next click (e.g. Dissolution cast). Skip it when the
    next action is a key event like the pass-turn hotkey.

    First tries a single-click full-MP walk via try_full_walk. On
    failure (no path, dynamic obstacle, Dofus refused), falls through to
    the original step-by-step click loop, which is more resilient to
    transient blockers (greedy neighbour picks, per-cell retries, etc).

    MP is read ONCE from GTM at entry and decremented locally per step
    in the fallback -- GTM only refreshes at turn boundaries. Pass
    `mp_override` for a second call in the same turn (the proxy doesn't
    re-emit GTM after our movement; state-read would be stale).

    Step-by-step fallback termination: distance hits 1, MP runs out, no
    neighbour improves distance, or WALK_MAX_STEPS is reached.

    `fast_fail`: when True, use WALK_STEP_FAST_FAIL_SEC as the per-click
    movement-wait timeout and bail on the first failed step -- skipping
    the usual single retry and the per-cell-exclusion fallback. Intended
    for the post-Dissolution follow-up walk: the bot is often surrounded
    by surviving mobs and the server silently rejects every walk click,
    and spending the usual 1s + 0.5s + 1s per dead step (~2.5s) on each
    of several cells is 5-10s of pure latency before we finally pass."""
    walk_wait = WALK_STEP_FAST_FAIL_SEC if fast_fail else WALK_STEP_WAIT_SEC
    _entry_snap = state.snapshot()
    _me_entry = my_fight_cell(_entry_snap)
    print(f"  [walk_toward] entry me={_me_entry} target={target_cell} "
          f"mp_override={mp_override} fast_fail={fast_fail} "
          f"static_obstacles={len(static_obstacles)} "
          f"map_id={_entry_snap.map_id}")
    full_ok, new_cell, mp_remaining, pending_settle = try_full_walk(
        target_cell, state, cal, static_obstacles,
        mp_override=mp_override, walk_wait_sec=walk_wait)
    if full_ok:
        snap = state.snapshot()
        me_cell = my_fight_cell(snap) or new_cell
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        return me_cell, my_ap, mp_remaining, pending_settle

    recent_failed = set()

    initial = state.snapshot()
    if mp_override is not None:
        estimated_mp = mp_override
    else:
        me0 = initial.fight_entities.get(initial.my_id)
        estimated_mp = me0.mp if me0 else 0
    me_cell = my_fight_cell(initial)
    steps_taken = 0
    moved_any = False

    while steps_taken < WALK_MAX_STEPS and estimated_mp > 0 and me_cell:
        dist = cell_distance(me_cell, target_cell)
        if dist <= 1:
            break
        step = pick_next_step(me_cell, target_cell, state.snapshot(),
                              recent_failed, static_obstacles)
        if step is None:
            print(f"  no A* path to target_cell={target_cell} from "
                  f"me_cell={me_cell} dist={dist} "
                  f"(failed_this_turn={len(recent_failed)})")
            break

        # Settle before each step *except the first* so consecutive walk
        # clicks don't get dropped mid-animation. After the last step we
        # don't sleep -- caller decides whether the next action needs it.
        if steps_taken > 0:
            time.sleep(WALK_STEP_SETTLE_SEC)

        sx, sy = cell_to_screen(step, cal)
        print(f"  STEP {steps_taken + 1}/{WALK_MAX_STEPS} from {me_cell} -> cell={step} "
              f"({sx},{sy}) [mp_left~{estimated_mp} dist={dist}]")
        before = me_cell
        click_at(sx, sy)
        moved = _wait_movement(state, before, walk_wait)
        if not moved and not fast_fail:
            print(f"    no movement from {before} -> cell={step} ({sx},{sy}); "
                  f"settling {WALK_STEP_SETTLE_SEC}s and retrying")
            time.sleep(WALK_STEP_SETTLE_SEC)
            before = my_fight_cell(state.snapshot()) or before
            click_at(sx, sy)
            moved = _wait_movement(state, before, walk_wait)

        steps_taken += 1

        if moved:
            moved_any = True
            new_cell = my_fight_cell(state.snapshot())
            mp_used = cell_distance(before, new_cell) if new_cell else 1
            estimated_mp -= mp_used
            if new_cell != step:
                print(f"    landed {new_cell} (expected {step}; Dofus pathed differently); "
                      f"mp_used={mp_used} mp_left~{estimated_mp}")
            else:
                print(f"    landed {new_cell}; mp_used={mp_used} mp_left~{estimated_mp}")
            me_cell = new_cell
            continue

        if fast_fail:
            print(f"  fast-fail: step from {before} -> cell={step} ({sx},{sy}) "
                  f"didn't move in {walk_wait}s; assuming we're blocked, bailing")
            break
        print(f"  step from {before} -> cell={step} ({sx},{sy}) "
              f"failed twice; excluding for the rest of this turn")
        recent_failed.add(step)

    snap = state.snapshot()
    me_cell = my_fight_cell(snap) or me_cell
    me = snap.fight_entities.get(snap.my_id)
    my_ap = me.ap if me else 0
    pending = WALK_STEP_SETTLE_SEC if moved_any else 0.0
    return me_cell, my_ap, max(estimated_mp, 0), pending


def run_combat_sacrid(ctx, state, cal):
    """Combat-phase loop. Caller guarantees fight_phase == "combat".

    Per turn: wait for GTS<myID>, settle, buff-if-eligible, walk toward
    the nearest live enemy, cast Dissolution if anything is adjacent
    (re-checked after the walk -- a different mob may have closed the
    gap), fire the bow at any enemy in [BOW_MIN_RANGE, BOW_MAX_RANGE]
    Po with LoS for whatever AP remains, then spend any leftover MP
    closing distance to the new nearest so we're better positioned
    next turn, pass. No target lock: Dissolution is self-cast AoE so
    the "which enemy" question never matters -- we just want any enemy
    in our 4 edge-adjacent cells; the bow always re-picks the nearest
    valid LoS target.

    Once TurnDistanceTracker flips tofu_detected (chasing is hopeless),
    switches to retreat mode: if we CAN close and cast this turn
    (cells_to_close <= my_mp AND my_ap >= DISSOLUTION_AP_COST), do
    that first -- free damage beats another retreat cycle. Then fire
    bow shots until AP runs out (kiters are usually within bow range
    after their hit-and-run move), then walk_away a random
    1..mp_remaining steps from the nearest live enemy. Skips buff and
    the post-Dissolution follow-up walk. The retreat breaks the
    kiter's rhythm -- they have to spend MP closing on a moving target
    instead of free-shooting us at max range.

    Exits when phase leaves combat (GE or GDM map-change)."""
    my_id = state.snapshot().my_id
    last_turn_n = 0
    # Turn number on which Strength Punishment was last cast this fight.
    # Initialised so the cooldown check passes on turn 1 (no prior cast).
    last_buff_turn = -BUFF_COOLDOWN_TURNS
    map_id = state.snapshot().map_id
    prune_obstacles_from_entities(map_id, state.snapshot())
    static_obstacles = set((MAP_DATA.get(map_id) or {}).get("obstacles") or ())
    if static_obstacles:
        print(f"  loaded {len(static_obstacles)} static obstacle(s) "
              f"for map={map_id}")
    dist_tracker = TurnDistanceTracker(TOFU_THRESHOLD, TOFU_REQUIRED_CYCLES)

    while state.snapshot().in_combat:
        new_turn = wait_for_my_turn(
            state, my_id, last_turn_n, TURN_WAIT_TIMEOUT_SEC)
        if new_turn == 0:
            # Combat ended or we timed out waiting. Outer loop will
            # re-check in_combat and either exit cleanly or retry.
            return
        print(f"  TURN {new_turn} start (actor={my_id}); settling "
              f"{TURN_START_SETTLE_SEC}s before acting")
        last_turn_n = new_turn
        time.sleep(TURN_START_SETTLE_SEC)

        snap = state.snapshot()
        me_cell = my_fight_cell(snap)
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        my_mp = me.mp if me else 0

        enemies = alive_enemies(snap)
        if not enemies:
            print("  no alive enemies in snapshot; passing")
            pass_turn(ctx)
            continue

        # Sample turn-start distance for tofu detection. Must happen
        # before any walk -- after we move, the distance reflects our
        # own MP spend, not the enemy's retreat.
        was_tofu = dist_tracker.tofu_detected
        turn_start_dist = cell_distance(me_cell, enemies[0].cell) if me_cell else None
        recorded = dist_tracker.observe_turn_start(turn_start_dist)
        if recorded is not None:
            print(f"  [tofu-track] turn-start dist={recorded} "
                  f"(history={dist_tracker.history[-TOFU_REQUIRED_CYCLES:]})")
        if dist_tracker.tofu_detected and not was_tofu:
            print(f"  [tofu] hit-and-run pattern detected: last "
                  f"{TOFU_REQUIRED_CYCLES} turn-start distances all "
                  f"> {TOFU_THRESHOLD} and not strictly decreasing; "
                  f"switching to retreat mode for the rest of this fight")

        # Tofu mode: don't chase. If they ended adjacent (their MP ran out
        # next to us), punish with Dissolution. Then walk AWAY a random
        # number of steps so they can't anchor their retreat off our cell
        # -- forces them to spend MP closing again instead of free-shooting
        # us at max range. Skip buff (wasted on cooldown turns we get
        # hit at most once) and follow-up walk (closing toward enemy
        # defeats the retreat).
        if dist_tracker.tofu_detected:
            nearest = enemies[0]
            dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
            cells_to_close = max(0, dist - 1)
            can_reach = me_cell is not None and cells_to_close <= my_mp
            can_attack = my_ap >= DISSOLUTION_AP_COST
            will_attack = (dist == 1 or can_reach) and can_attack
            # Cornered = no neighbour strictly increases dist from enemy.
            # Combined with no attack opportunity this turn, the tofu block
            # would just pass forever (the kiter stays out of range and we
            # stand still until dead -- happened on cell 15 at top of
            # diamond for 20+ turns). Fall back to normal combat: walking
            # toward the enemy at least sets up a future cast and breaks
            # the deadlock.
            can_retreat = me_cell is not None and pick_retreat_step(
                me_cell, nearest.cell, state.snapshot(),
                set(), set(static_obstacles)) is not None
            # A bow shot is also "an attack opportunity" -- if we can
            # hit something at range without chasing, prefer that over
            # falling through to normal combat (which would chase a
            # kiter we already know we can't catch).
            can_bow = (my_ap >= BOW_AP_COST
                       and pick_bow_target(snap, me_cell, static_obstacles) is not None)
            # Close as much as possible before shooting -- land at
            # BOW_MIN_RANGE (or as close as MP allows, never below MIN
            # since bow can't fire adjacent and dist=1 is the
            # Dissolution branch above). Trades retreat MP for a tighter
            # shot, which the user prefers: free damage from close range
            # beats kiting at max range. Guard with `landing_dist <=
            # BOW_MAX_RANGE` so we don't burn retreat MP closing when
            # we still can't shoot afterwards (e.g. dist=10, mp=3).
            steps_into_bow = (min(my_mp, dist - BOW_MIN_RANGE)
                              if me_cell is not None and dist > BOW_MIN_RANGE
                              else 0)
            landing_dist = dist - steps_into_bow if steps_into_bow > 0 else dist
            can_walk_to_bow = (my_ap >= BOW_AP_COST
                               and steps_into_bow > 0
                               and landing_dist <= BOW_MAX_RANGE
                               and not (can_reach and can_attack))
            if not will_attack and not can_retreat and not can_bow and not can_walk_to_bow:
                print(f"  [tofu] cornered at {me_cell}: no retreat step "
                      f"from id={nearest.id} cell={nearest.cell} dist={dist} "
                      f"and can't close+cast (mp={my_mp} ap={my_ap}) "
                      f"and no bow target in range with LoS; "
                      f"falling back to normal combat this turn")
                # Don't `continue`; fall through to the buff + walk_toward
                # + Dissolution block below.
            else:
                print(f"  [tofu] nearest id={nearest.id} cell={nearest.cell} "
                      f"dist={dist} my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")
                mp_remaining = my_mp
                pending_settle = 0.0
                if can_reach and can_attack:
                    # We CAN close and cast this turn -- do it before retreating.
                    # Free damage is always better than another retreat cycle.
                    if dist > 1:
                        print(f"  [tofu] closing to attack (need {cells_to_close} "
                              f"mp, have {my_mp}; ap={my_ap})")
                        me_cell, _, mp_remaining, pending_settle = walk_toward(
                            nearest.cell, state, cal, static_obstacles)
                        enemies = alive_enemies(state.snapshot())
                        dist = (cell_distance(me_cell, enemies[0].cell)
                                if enemies and me_cell else 99)
                    if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
                        if pending_settle > 0:
                            time.sleep(pending_settle)
                        cast_dissolution(me_cell, cal)
                        time.sleep(CAST_WAIT_SEC)
                        my_ap -= DISSOLUTION_AP_COST
                        pending_settle = 0.0
                elif can_walk_to_bow:
                    # Walk as close as we can while staying at >=
                    # BOW_MIN_RANGE (bow can't fire adjacent). Cap
                    # mp_override at steps_into_bow so walk_toward
                    # stops one short of melee. After this, fire_bow_burst
                    # below shoots, then any leftover MP retreats.
                    expected_landing = dist - steps_into_bow
                    print(f"  [tofu] closing {steps_into_bow} step(s) toward "
                          f"bow range (dist={dist} -> ~{expected_landing}, "
                          f"bow_range={BOW_MIN_RANGE}..{BOW_MAX_RANGE}, "
                          f"mp={my_mp}, ap={my_ap})")
                    before_cell = me_cell
                    me_cell, _, _, pending_settle = walk_toward(
                        nearest.cell, state, cal, static_obstacles,
                        mp_override=steps_into_bow)
                    steps_used = (cell_distance(before_cell, me_cell)
                                  if me_cell and before_cell else 0)
                    mp_remaining = max(0, my_mp - steps_used)
                elif dist == 1 and can_attack:
                    # Already adjacent (their MP ran out next to us) -- punish.
                    cast_dissolution(me_cell, cal)
                    time.sleep(CAST_WAIT_SEC)
                    my_ap -= DISSOLUTION_AP_COST
                elif dist == 1:
                    print(f"  [tofu] adjacent but ap={my_ap} < "
                          f"{DISSOLUTION_AP_COST}; not casting")
                # Bow whatever AP is left at any enemy in [min,max] Po
                # with LoS. Picks off kiters we can't (or won't) chase
                # AND adds free damage on turns Dissolution already fired.
                # A successful shot clears tofu_detected: if we can hit
                # them at range, the kiting pattern is no longer blocking
                # us and the next turn should resume normal combat.
                if state.snapshot().in_combat and me_cell:
                    if pending_settle > 0:
                        time.sleep(pending_settle + BOW_POST_WALK_EXTRA_SETTLE_SEC)
                        pending_settle = 0.0
                    my_ap, bow_shots = fire_bow_burst(
                        state, cal, my_ap, me_cell, static_obstacles)
                    if bow_shots > 0 and dist_tracker.tofu_detected:
                        print(f"  [tofu] bow connected ({bow_shots} shot(s)); "
                              f"exiting retreat mode -- we can hit them at "
                              f"range, no need to keep kiting back")
                        dist_tracker.tofu_detected = False
                # Retreat with leftover MP. Re-pick nearest in case the cast
                # killed one and a different enemy is now closest.
                if mp_remaining > 0 and me_cell:
                    live = alive_enemies(state.snapshot())
                    if live:
                        anchor = live[0].cell
                        steps = random.randint(0, mp_remaining)
                        print(f"  [tofu] retreating {steps} step(s) away from "
                              f"cell={anchor} (mp_left={mp_remaining})")
                        if steps > 0:
                            walk_away(anchor, state, cal, static_obstacles,
                                      max_steps=steps)
                if not state.snapshot().in_combat:
                    return
                print("  PASS (pass-turn hotkey)")
                pass_turn(ctx)
                continue

        # Buff with cooldown: cast on turn T, recast at turn T + cooldown.
        # Distance-gated: skip if nearest enemy > BUFF_MAX_DIST (buff
        # triggers on damage taken, would expire before they close).
        # Re-checked every turn so all gates are re-evaluated.
        nearest_dist = (cell_distance(me_cell, enemies[0].cell)
                        if me_cell else 99)
        turns_since_buff = new_turn - last_buff_turn
        buff_ready = turns_since_buff >= BUFF_COOLDOWN_TURNS
        if not BUFF_ENABLED:
            pass  # sacrid_buff_enabled=false: skip Strength Punishment entirely
        elif buff_ready and me_cell and my_ap >= BUFF_AP_COST:
            if nearest_dist <= BUFF_MAX_DIST:
                print(f"  buff: nearest_dist={nearest_dist} <= {BUFF_MAX_DIST}, "
                      f"cooldown ready (last_cast_turn={last_buff_turn}), casting")
                cast_strength_punishment(me_cell, cal)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= BUFF_AP_COST
                last_buff_turn = new_turn
                print(f"  buff cast on turn {new_turn}; ap_left~{my_ap} "
                      f"(next available turn {new_turn + BUFF_COOLDOWN_TURNS})")
            else:
                print(f"  buff: nearest_dist={nearest_dist} > {BUFF_MAX_DIST}, "
                      f"skipping (too far -- buff would expire before we get hit)")
        elif not buff_ready:
            print(f"  buff: on cooldown ({turns_since_buff}/{BUFF_COOLDOWN_TURNS} "
                  f"turns since last cast on turn {last_buff_turn})")

        # No target lock: nearest live enemy is the walk anchor. After
        # walking we re-pull alive_enemies so a mob that closed the gap
        # mid-turn counts toward the adjacency check.
        nearest = enemies[0]
        dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
        print(f"  nearest id={nearest.id} cell={nearest.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")

        mp_remaining = my_mp
        pending_settle = 0.0
        if dist > 1 and me_cell and mp_remaining > 0:
            # Discard walk_toward's my_ap return -- it reads GTM which
            # can't see our intra-turn buff cast. Keep locally-tracked AP.
            me_cell, _, mp_remaining, pending_settle = walk_toward(
                nearest.cell, state, cal, static_obstacles)
            enemies = alive_enemies(state.snapshot())
            dist = (cell_distance(me_cell, enemies[0].cell)
                    if enemies and me_cell else 99)

        if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
            # Let walk animation finish before clicking -- mid-animation
            # spell clicks get silently dropped.
            if pending_settle > 0:
                time.sleep(pending_settle)
                pending_settle = 0.0
            cast_dissolution(me_cell, cal)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= DISSOLUTION_AP_COST
        elif dist == 1 and my_ap < DISSOLUTION_AP_COST:
            print(f"  adjacent but ap={my_ap} < cost={DISSOLUTION_AP_COST}; "
                  f"not casting this turn")
        elif dist > 1:
            print(f"  nothing adjacent (nearest_dist={dist}); not casting this turn")

        # Bow with leftover AP. Hits anything at range [min,max] with
        # LoS -- the fallback when we couldn't (or didn't) reach
        # adjacency, plus a bonus hit when Dissolution already fired
        # but more AP is on the table. Reachable from tofu mode via the
        # cornered fall-through (line above the tofu else): if bow lands
        # here, clear tofu_detected for the same reason as in the tofu
        # branch -- ranged damage means the kiting is no longer a wall.
        if state.snapshot().in_combat and me_cell and my_ap >= BOW_AP_COST:
            if pending_settle > 0:
                time.sleep(pending_settle + BOW_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            my_ap, bow_shots = fire_bow_burst(
                state, cal, my_ap, me_cell, static_obstacles)
            if bow_shots > 0 and dist_tracker.tofu_detected:
                print(f"  [tofu] bow connected ({bow_shots} shot(s)) from "
                      f"normal-combat fall-through; exiting retreat mode")
                dist_tracker.tofu_detected = False

        # Follow-up walk: spend leftover MP closing toward a non-adjacent
        # enemy so we're better positioned next turn. Pass mp_remaining
        # via override -- the proxy doesn't refresh GTM mid-turn so
        # state.mp is still showing turn-start value.
        #
        # Skip currently-adjacent enemies: GTM only fires at turn
        # boundaries, so anything we just hit with Dissolution still
        # shows alive=True at dist=1 until next turn. Either they died
        # (we need to reposition) or they survived (we're still in AoE
        # range for next turn either way) -- walking toward a more
        # distant target wins both cases. Picking just the absolute
        # nearest would let a stale just-killed adjacent entry block
        # any walk and force an immediate pass.
        if state.snapshot().in_combat and mp_remaining > 0 and me_cell:
            distant = [e for e in alive_enemies(state.snapshot())
                       if cell_distance(me_cell, e.cell) > 1]
            if distant:
                follow = distant[0]
                follow_dist = cell_distance(me_cell, follow.cell)
                print(f"  follow-up walk: closing toward id={follow.id} "
                      f"cell={follow.cell} dist={follow_dist} "
                      f"mp_left={mp_remaining} (fast-fail)")
                me_cell, _, mp_remaining, _ = walk_toward(
                    follow.cell, state, cal, static_obstacles,
                    mp_override=mp_remaining, fast_fail=True)

        if not state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(ctx)


def _prompt_int(label, default):
    while True:
        raw = input(f"{label} [{default}]: ").strip()
        if not raw:
            return default
        try:
            return int(raw)
        except ValueError:
            print(f"  not a number: {raw!r}")


def _prompt_yn(label, default=True):
    suffix = "Y/n" if default else "y/N"
    while True:
        raw = input(f"{label} [{suffix}]: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print(f"  answer y or n (got {raw!r})")


def prompt_runtime_settings():
    """Interactive prompts for per-run settings. Overrides the config
    defaults at startup so the operator doesn't have to hand-edit
    config.json between farming sessions."""
    print("[fighter] runtime settings (press enter for default):")
    buff = _prompt_yn("  cast Strength Punishment buff?", default=True)
    max_group = _prompt_int("  max mob group size", default=8)
    min_hp = _prompt_int("  min HP before engaging", default=500)
    return SimpleNamespace(
        buff_enabled=buff,
        max_group_size=max_group,
        min_hp=min_hp,
    )


def main():
    if not DISSOLUTION_HOTKEY:
        print("config.json is missing 'sacrid_dissolution_hotkey'. Set it to the "
              "key Dissolution is bound to on Marx-Rockfeller's spell bar (e.g. \"2\").")
        sys.exit(1)

    args = prompt_runtime_settings()
    # Override the module-level BUFF_ENABLED so run_combat_sacrid (which
    # reads it as a module global) picks up the operator's choice.
    globals()["BUFF_ENABLED"] = args.buff_enabled

    cal = load_cal()
    print(f"[fighter] cal: origin=({cal['origin_x']:.1f},{cal['origin_y']:.1f}) "
          f"cell={cal['cell_w']:.2f}x{cal['cell_h']:.2f}")
    print(f"[fighter] dissolution_hotkey={DISSOLUTION_HOTKEY!r} ap_cost={DISSOLUTION_AP_COST}")
    print(f"[fighter] buff: {'enabled' if args.buff_enabled else 'DISABLED (skipping Strength Punishment)'}")
    print(f"[fighter] min-hp threshold: wait until >= {args.min_hp} HP before engaging")
    if args.max_group_size > 0:
        print(f"[fighter] max-group-size: skip mob groups with > {args.max_group_size} members")
    else:
        print(f"[fighter] max-group-size: no cap (engaging any group size)")

    with_switches = sum(1 for d in MAP_DATA.values() if d.get("switch_cells"))
    with_safe = sum(1 for d in MAP_DATA.values()
                    if safe_directions(d, MAP_BY_WORLD))
    print(f"[fighter] navigation: {len(MAP_DATA)} map(s) calibrated, "
          f"{with_switches} have switch_cells, "
          f"{with_safe} have >= 1 return-safe neighbour")

    state = ProxyState(PROXY_ADDR)
    state.on_event(make_exchange_dismiss_callback())
    state.start()
    print(f"[fighter] connecting to proxy at {PROXY_ADDR}...")
    if not wait_for(state, lambda s: s.connected and s.my_id != 0, 10.0):
        snap = state.snapshot()
        print(f"[fighter] proxy not ready: connected={snap.connected} my_id={snap.my_id}")
        sys.exit(1)
    snap = state.snapshot()
    print(f"[fighter] ready: my_id={snap.my_id} my_cell={snap.my_cell} map={snap.map_id}")

    with mss.mss() as sct:
        ctx = make_ctx(sct)
        last_status_ts = 0.0
        placed_for_engage_ts = 0.0
        # (cell, group_id) tuples we clicked but failed to engage on
        # this map. Cleared on map change. Catches anything the proxy's
        # s.mobs-drop doesn't (missed GM|-, third-party engages, etc).
        ghosts = set()
        last_map_id = snap.map_id
        # Last direction we walked out a switch cell -- used to bias
        # away from immediate backtracking when alternatives exist.
        last_walk_direction = None
        # {map_id: ts when last found empty}. Filtered against when
        # picking a navigation target; entries expire after
        # EMPTY_MAP_RESPAWN_SEC and are cleared on successful engage.
        recently_empty_maps: dict[int, float] = {}
        # Count of back-to-back failed switch-cell walks. Resets on any
        # successful map change. Dofus disconnects after ~30min idle, so
        # if we burn many MAP_CHANGE_TIMEOUT cycles in a row the clicks
        # aren't producing real movement -- calibration drift, blocked
        # path, or off-map switch_cell. Print a loud warning so the
        # operator notices before the inactivity kick.
        consecutive_walk_failures = 0
        STUCK_WARN_THRESHOLD = 5

        while True:
            snap = state.snapshot()
            if snap.map_id != last_map_id:
                if ghosts:
                    print(f"[fighter] map changed {last_map_id} -> {snap.map_id}; "
                          f"clearing {len(ghosts)} ghost(s)")
                ghosts.clear()
                last_map_id = snap.map_id
                consecutive_walk_failures = 0
                # GDM clears my_cell=0 and mobs={} before the new map's
                # GM|+ entity burst arrives. Without this wait, the next
                # nearest_mob() sees mobs={}, falsely flags the map empty,
                # and walks straight out -- bouncing through several maps
                # before mob data catches up. my_cell repopulating from the
                # GM|+ burst (which carries the mobs too) is the reliable
                # "map has settled" signal.
                if not wait_for(state, lambda s: s.my_cell != 0, 2.0, poll=0.05):
                    print(f"[fighter] my_cell didn't populate within 2s of "
                          f"map change to {snap.map_id}; proceeding anyway")
                snap = state.snapshot()

            if snap.in_combat:
                ents = snap.fight_entities
                others = [e for e in ents.values() if e.id != snap.my_id]
                fight_mob_size = len(others)
                fight_start_ts = snap.last_fight_engage_ts
                print(f"[fighter] phase=combat (map={snap.map_id}) "
                      f"entities={len(ents)} enemies={len(others)}, running sacrid combat")
                run_combat_sacrid(ctx, state, cal)
                # Record stats only on a real fight end -- in_combat=False
                # AND last_fight_end_ts is from THIS fight (> engage_ts).
                # Guards against double-counting if run_combat_sacrid
                # returned due to its internal turn-wait timeout.
                end_snap = state.snapshot()
                if (not end_snap.in_combat
                        and fight_mob_size > 0
                        and fight_start_ts > 0
                        and end_snap.last_fight_end_ts > fight_start_ts):
                    duration = end_snap.last_fight_end_ts - fight_start_ts
                    append_fight_stats(fight_mob_size, duration)
                    print(f"[fighter] stats: mob_size={fight_mob_size} "
                          f"fight_duration={duration:.2f}s -> {STATS_FILE}")
                # Enter dismisses any level-up popup, 1s gap lets the
                # XP summary take focus, then Esc closes it. The 4s
                # pre-wait gives both popups time to render.
                time.sleep(4.0)
                press_focused("Return")
                time.sleep(1.0)
                press_focused("Escape")
                time.sleep(0.3)
                if not ensure_safe_to_resume(ctx):
                    print("[fighter] menu still open after Esc -- aborting")
                    sys.exit(1)
                continue

            if snap.in_placement:
                # Ready up so we don't burn the full 30s placement timer.
                print(f"[fighter] phase=placement (map={snap.map_id}); readying up")
                time.sleep(ctx.cfg["fight_ready_wait_sec"])
                if snap.last_fight_engage_ts != placed_for_engage_ts:
                    place_starting_cells(snap, cal)
                    placed_for_engage_ts = snap.last_fight_engage_ts
                pass_turn(ctx)
                if not wait_for(state, lambda s: s.in_combat or not s.in_fight,
                                COMBAT_START_TIMEOUT):
                    print(f"[fighter] still in placement after {COMBAT_START_TIMEOUT}s; "
                          f"will press ready again next iteration")
                continue

            # phase == idle
            near = nearest_mob(snap, ghosts, args.max_group_size)
            if near is None:
                # nearest_mob filters mid-walk mobs (move_ends_at_ms in
                # the future). If all visible non-ghost mobs are walking,
                # don't mark the map empty -- they'll be click-targetable
                # in another ~400-800ms. Just sleep and re-pick.
                now_ms = int(time.time() * 1000)
                walking = [
                    (c, m) for c, m in snap.mobs.items()
                    if (c, m.group_id) not in ghosts
                    and (args.max_group_size <= 0 or len(m.members) <= args.max_group_size)
                    and m.move_ends_at_ms > now_ms
                ]
                if walking:
                    wait_ms = max(m.move_ends_at_ms - now_ms for _, m in walking)
                    time.sleep(min(wait_ms / 1000.0 + 0.05, IDLE_POLL_SEC))
                    continue
                # No valid mob (none visible or all filtered). Mark this
                # map empty, then try to walk to a calibrated neighbour
                # whose target isn't itself in cooldown.
                entry = MAP_DATA.get(snap.map_id) or {}
                switch_cells_map = entry.get("switch_cells") or {}
                recently_empty_maps[snap.map_id] = time.time()
                safe = safe_directions(entry, MAP_BY_WORLD) if switch_cells_map else []
                now = time.time()
                recently_empty_maps = {
                    mid: ts for mid, ts in recently_empty_maps.items()
                    if now - ts < EMPTY_MAP_RESPAWN_SEC
                }
                fresh = [
                    d for d in safe
                    if (tmid := target_map_id(entry, d, MAP_BY_WORLD)) is None
                    or tmid not in recently_empty_maps
                ]
                if fresh:
                    excluded = OPPOSITE_DIRECTION.get(last_walk_direction)
                    preferred = [d for d in fresh if d != excluded]
                    direction = random.choice(preferred or fresh)
                    switch_cell = switch_cells_map[direction]
                    x, y = cell_to_screen(switch_cell, cal)
                    total = len(snap.mobs)
                    reason = (f"{total} group(s) all filtered by "
                              f"max-group-size={args.max_group_size}"
                              if args.max_group_size > 0 and total > 0
                              else "no mobs visible")
                    skipped = [d for d in safe if d not in fresh]
                    skip_note = (f", skipping {skipped} (target map recently empty)"
                                 if skipped else "")
                    cur_world = entry.get("world")
                    cur_world_str = (f"({cur_world[0]},{cur_world[1]})"
                                     if cur_world else "?")
                    delta = DIRECTION_WORLD_DELTA.get(direction)
                    if cur_world and delta:
                        tgt_world_str = f"({cur_world[0]+delta[0]},{cur_world[1]+delta[1]})"
                    else:
                        tgt_world_str = "?"
                    tgt_mid = target_map_id(entry, direction, MAP_BY_WORLD)
                    tgt_mid_str = str(tgt_mid) if tgt_mid is not None else "?"
                    print(f"[fighter] phase=idle map={snap.map_id} world={cur_world_str} "
                          f"my_cell={snap.my_cell}: {reason}; "
                          f"walking {direction} (fresh={fresh}{skip_note}, "
                          f"avoid={excluded}) to switch cell={switch_cell} "
                          f"-> screen=({x},{y}); target map={tgt_mid_str} world={tgt_world_str}")
                    ctx.click_at(x, y)
                    last_walk_direction = direction
                    before_map = snap.map_id
                    if wait_for(state,
                                lambda s, bm=before_map: (s.map_id != bm and s.map_id != 0)
                                                          or s.in_fight,
                                MAP_CHANGE_TIMEOUT):
                        ns = state.snapshot()
                        if ns.in_fight:
                            print(f"[fighter] aggroed while walking {direction}; "
                                  f"phase={ns.fight_phase}")
                        else:
                            print(f"[fighter] map changed {before_map} -> {ns.map_id} "
                                  f"via {direction}")
                        consecutive_walk_failures = 0
                    else:
                        consecutive_walk_failures += 1
                        print(f"[fighter] walk to {direction} switch cell did not "
                              f"change map in {MAP_CHANGE_TIMEOUT}s; will retry "
                              f"next tick (consecutive_failures={consecutive_walk_failures})")
                        if consecutive_walk_failures >= STUCK_WARN_THRESHOLD:
                            elapsed = consecutive_walk_failures * MAP_CHANGE_TIMEOUT
                            print(f"[fighter] *** STUCK *** {consecutive_walk_failures} "
                                  f"consecutive failed walks on map={snap.map_id} "
                                  f"world={cur_world_str} (~{elapsed:.0f}s of no real "
                                  f"movement). Dofus inactivity disconnect is ~30min. "
                                  f"Check: switch_cell calibration, obstacles blocking "
                                  f"path, or game-window focus.")
                    continue
                if now - last_status_ts > STATUS_LOG_SEC:
                    total = len(snap.mobs)
                    cap_note = (f" (filtered by max-group-size={args.max_group_size};"
                                f" total_visible={total})"
                                if args.max_group_size > 0 and total > 0 else "")
                    if not switch_cells_map:
                        nav_note = "no switch_cells calibrated for this map"
                    elif not safe:
                        nav_note = (f"switch_cells={list(switch_cells_map.keys())} but "
                                    f"no return-safe neighbour (target maps un-calibrated "
                                    f"or missing return switch)")
                    else:
                        nav_note = (f"safe={safe} but every target is in cooldown "
                                    f"(empty within {int(EMPTY_MAP_RESPAWN_SEC)}s); "
                                    f"waiting for respawn")
                    cur_world = entry.get("world")
                    cur_world_str = (f"({cur_world[0]},{cur_world[1]})"
                                     if cur_world else "?")
                    print(f"[fighter] phase=idle map={snap.map_id} world={cur_world_str} "
                          f"my_cell={snap.my_cell} no mobs visible "
                          f"(ghosts={len(ghosts)}){cap_note}; {nav_note}")
                    last_status_ts = now
                time.sleep(IDLE_POLL_SEC)
                continue
            d, cell, mob = near
            # Map has a valid mob -- clear the empty flag.
            recently_empty_maps.pop(snap.map_id, None)
            # Re-snapshot before clicking: mobs wander and `snap` is up
            # to IDLE_POLL_SEC stale. Bail if the group moved.
            fresh = state.snapshot().mobs.get(cell)
            if fresh is None or fresh.group_id != mob.group_id:
                print(f"[fighter] mob group={mob.group_id} moved/despawned "
                      f"from cell={cell} before click; re-picking next tick")
                continue
            if not wait_for_hp(state, args.min_hp):
                # Either aggroed (next tick's in_fight branch handles it)
                # or timed out / refused to engage blind. Don't click.
                time.sleep(1.0)
                continue
            x, y = cell_to_screen(cell, cal)
            hp_snap = state.snapshot()
            print(f"[fighter] engaging nearest mob: cell={cell} dist={d} "
                  f"group={mob.group_id} members={mob.members} "
                  f"hp~{hp_snap.estimated_life()}/{hp_snap.my_life_max} -> screen=({x},{y})")
            ctx.click_at(x, y)
            if wait_for(state, lambda s: s.in_fight, ENGAGE_TIMEOUT):
                print(f"[fighter] fight_engage received (phase={state.snapshot().fight_phase})")
                continue
            # No engage -> this (cell, group_id) is a ghost; never click it
            # again until the map changes.
            ghosts.add((cell, mob.group_id))
            print(f"[fighter] click on cell={cell} group={mob.group_id} did not "
                  f"engage; marking ghost (total={len(ghosts)})")
            # Re-pick nearest from a *fresh* snapshot, excluding ghosts.
            fresh_snap = state.snapshot()
            alt = nearest_mob(fresh_snap, ghosts, args.max_group_size)
            if alt is None:
                print(f"[fighter] no non-ghost mob groups to try; sleeping 3s")
                time.sleep(3.0)
                continue
            d2, acell, amob = alt
            ax, ay = cell_to_screen(acell, cal)
            print(f"[fighter] nearest didn't engage; trying next-nearest mob: "
                  f"cell={acell} dist={d2} group={amob.group_id} "
                  f"members={amob.members} -> screen=({ax},{ay})")
            ctx.click_at(ax, ay)
            if wait_for(state, lambda s: s.in_fight, ENGAGE_TIMEOUT):
                print(f"[fighter] fight_engage received (phase={state.snapshot().fight_phase})")
            else:
                ghosts.add((acell, amob.group_id))
                print(f"[fighter] next-nearest also didn't engage; marking ghost "
                      f"(total={len(ghosts)}); sleeping 3s")
                time.sleep(3.0)


if __name__ == "__main__":
    main()
