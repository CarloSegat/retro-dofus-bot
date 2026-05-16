"""Proxy-driven Sacrieur fighter (Marx-Rockfeller / berlinthree).

Tri-state loop mirroring the proxy's fight_phase:
  idle      -> engage nearest valid mob, or navigate NSEW if none
  placement -> ready up, place starting cells
  combat    -> run_combat_sacrid (buff -> walk -> Sacrid Foot -> pass)

Strategy: one Sacrid Foot per turn (it has a 1/turn cap). Strength
Punishment self-buff is cast once per fight on the first turn where the
nearest enemy is within sacrid_buff_max_dist (the buff only triggers on
damage taken; pointless if mobs are too far away to hit us before it
expires).

Usage:
  python3 -u main.py [--min-hp HP] [--max-group-size N]

  --min-hp:         wait until HP >= this (capped at max) before engaging.
                    If no "As" stats packet has been seen, refuse rather
                    than attack blind.
  --max-group-size: skip mob groups with more than N members. 0 = no cap.

Pre-reqs: proxy running on 127.0.0.1:9999, config.json has
cell_calibration + sacrid_foot_hotkey. See CLAUDE.md for config knobs.
"""
import argparse
import random
import sys
import time

import mss

from cell_grid import a_star, cell_distance, cell_to_xy, neighbors
from dialogs import ensure_safe_to_resume
from fight import pass_turn
from map_data import (
    DIRECTION_WORLD_DELTA,
    OPPOSITE_DIRECTION,
    build_world_index,
    load_all as load_map_data,
    safe_directions,
    target_map_id,
)
from proxy_client import ProxyState
from utils import CFG, click, make_ctx, press, press_xdotool, type_xdotool

MAP_DATA = load_map_data()  # {map_id: {"world", "map_id", "cells", "obstacles", ...}}
MAP_BY_WORLD = build_world_index(MAP_DATA)  # {(world_x, world_y): entry}

PROXY_ADDR = "127.0.0.1:9999"

SPELL_HOTKEY = CFG.get("sacrid_foot_hotkey")
FOOT_AP_COST = int(CFG.get("sacrid_foot_ap_cost", 4))
# Strength Punishment self-buff: hotkey + click own cell.
BUFF_HOTKEY = CFG.get("sacrid_buff_hotkey", "3")
BUFF_AP_COST = int(CFG.get("sacrid_buff_ap_cost", 3))
# Skip buff if nearest enemy > this many Po away -- buff triggers on
# damage taken, and would expire before they close the gap.
BUFF_MAX_DIST = int(CFG.get("sacrid_buff_max_dist", 6))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
WALK_WAIT_SEC = float(CFG.get("sacrid_walk_wait_sec", 2.0))
WALK_STEP_WAIT_SEC = float(CFG.get("sacrid_walk_step_wait_sec", 1.0))
WALK_MAX_STEPS = int(CFG.get("sacrid_walk_max_steps", 6))
# Dofus retro silently drops walk clicks issued mid-animation; settle
# between steps to avoid false "obstacle" entries.
WALK_STEP_SETTLE_SEC = float(CFG.get("sacrid_walk_step_settle_sec", 0.5))

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


def sit_for_regen(state):
    """Send /sit via chat and mark ProxyState.sitting=True.

    Pre-condition: we are currently STANDING. /sit is a toggle so the
    caller must know this -- combat auto-stands, so by the time
    wait_for_hp invokes us we're known standing. Cleared automatically
    on fight_engage."""
    print("[fighter] /sit to regen faster")
    press_xdotool("Return")
    time.sleep(0.3)
    type_xdotool("/sit")
    time.sleep(0.3)
    press_xdotool("Return")
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

    When my_cell is 0 (proxy just attached) we return an arbitrary mob
    with distance=-1 so the caller still tries to engage."""
    ghost_set = set(ghosts)
    candidates = [
        (c, m) for c, m in snap.mobs.items()
        if (c, m.group_id) not in ghost_set
        and (max_group_size <= 0 or len(m.members) <= max_group_size)
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
        return path[1]

    # Fallback: walk one tile toward target, dodging live entities. Matches
    # the pre-A* greedy behaviour so a mid-corridor mob doesn't pin us.
    current_dist = cell_distance(me_cell, target_cell)
    cands = []
    for n in neighbors(me_cell):
        if cell_distance(n, me_cell) != 1:
            continue  # off-grid wrap
        if n in static or n in dynamic or n in rf:
            continue
        d = cell_distance(n, target_cell)
        if d >= current_dist:
            continue
        cands.append((d, n))
    if not cands:
        return None
    cands.sort()
    return cands[0][1]


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
        click(x, y)
        time.sleep(0.3)


def cast_foot_on(target_cell, cal):
    """Press Sacrid Foot hotkey, then click the target cell."""
    x, y = cell_to_screen(target_cell, cal)
    print(f"  CAST Sacrid Foot hotkey={SPELL_HOTKEY!r} target_cell={target_cell} -> ({x},{y})")
    press(SPELL_HOTKEY)
    time.sleep(0.4)  # let Dofus enter spell-aim mode and show the reticle
    click(x, y)


def cast_strength_punishment(my_cell, cal):
    """Press Strength Punishment hotkey, then click own cell (self-buff)."""
    x, y = cell_to_screen(my_cell, cal)
    print(f"  CAST Strength Punishment hotkey={BUFF_HOTKEY!r} self_cell={my_cell} -> ({x},{y})")
    press(BUFF_HOTKEY)
    time.sleep(0.4)
    click(x, y)


def _wait_movement(state, before, timeout):
    """True iff my_fight_cell moves away from `before` within `timeout`."""
    return wait_for(
        state,
        lambda s, b=before: my_fight_cell(s) != b and my_fight_cell(s) > 0,
        timeout,
    )


def walk_toward(target_cell, state, cal, static_obstacles=()):
    """Step one cell at a time toward `target_cell`. Returns (me_cell, my_ap).

    MP is read ONCE from GTM at entry and decremented locally per step --
    GTM only refreshes at turn boundaries, so polling mid-turn returns
    the stale turn-start value forever.

    On click failure, settle and retry the same cell once; if that also
    fails, exclude the cell for the rest of this turn (in-memory only).

    Terminates when: distance to target hits 1, MP runs out, no
    neighbour improves distance, or WALK_MAX_STEPS is reached."""
    recent_failed = set()

    initial = state.snapshot()
    me0 = initial.fight_entities.get(initial.my_id)
    estimated_mp = me0.mp if me0 else 0
    me_cell = my_fight_cell(initial)
    steps_taken = 0

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

        sx, sy = cell_to_screen(step, cal)
        print(f"  STEP {steps_taken + 1}/{WALK_MAX_STEPS} from {me_cell} -> cell={step} "
              f"({sx},{sy}) [mp_left~{estimated_mp} dist={dist}]")
        before = me_cell
        click(sx, sy)
        moved = _wait_movement(state, before, WALK_STEP_WAIT_SEC)
        if not moved:
            print(f"    no movement; settling {WALK_STEP_SETTLE_SEC}s and retrying {step}")
            time.sleep(WALK_STEP_SETTLE_SEC)
            before = my_fight_cell(state.snapshot()) or before
            click(sx, sy)
            moved = _wait_movement(state, before, WALK_STEP_WAIT_SEC)

        steps_taken += 1

        if moved:
            new_cell = my_fight_cell(state.snapshot())
            mp_used = cell_distance(before, new_cell) if new_cell else 1
            estimated_mp -= mp_used
            if new_cell != step:
                print(f"    landed {new_cell} (expected {step}; Dofus pathed differently); "
                      f"mp_used={mp_used} mp_left~{estimated_mp}")
            else:
                print(f"    landed {new_cell}; mp_used={mp_used} mp_left~{estimated_mp}")
            me_cell = new_cell
            time.sleep(WALK_STEP_SETTLE_SEC)
            continue

        print(f"  step to {step} failed twice; excluding for the rest of this turn")
        recent_failed.add(step)

    snap = state.snapshot()
    me_cell = my_fight_cell(snap) or me_cell
    me = snap.fight_entities.get(snap.my_id)
    my_ap = me.ap if me else 0
    return me_cell, my_ap


def run_combat_sacrid(ctx, state, cal):
    """Combat-phase loop. Caller guarantees fight_phase == "combat".

    Per turn: wait for GTS<myID>, settle, buff-if-eligible, walk toward
    the *locked* target (we commit to one mob until it dies to avoid
    flip-flopping mid-fight), cast Sacrid Foot if adjacent with AP,
    pass. Exits when phase leaves combat (GE or GDM map-change)."""
    my_id = state.snapshot().my_id
    last_turn_n = 0
    buff_cast = False
    # Locked target: id preferred, cell as fallback for id-less entities.
    locked_target_id = 0
    locked_target_cell = 0
    map_id = state.snapshot().map_id
    static_obstacles = set((MAP_DATA.get(map_id) or {}).get("obstacles") or ())
    if static_obstacles:
        print(f"  loaded {len(static_obstacles)} static obstacle(s) "
              f"for map={map_id}")

    while state.snapshot().in_combat:
        new_turn = wait_for_my_turn(state, my_id, last_turn_n, TURN_WAIT_TIMEOUT_SEC)
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

        # Buff once per fight, gated by distance: skip if nearest enemy
        # > BUFF_MAX_DIST (buff triggers on damage taken, would expire
        # before they close). Re-checked each turn until they're in range.
        nearest_dist = (cell_distance(me_cell, enemies[0].cell)
                        if me_cell else 99)
        if not buff_cast and me_cell and my_ap >= BUFF_AP_COST:
            if nearest_dist <= BUFF_MAX_DIST:
                print(f"  buff: nearest_dist={nearest_dist} <= {BUFF_MAX_DIST}, casting")
                cast_strength_punishment(me_cell, cal)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= BUFF_AP_COST
                buff_cast = True
                print(f"  buff cast; ap_left~{my_ap}")
            else:
                print(f"  buff: nearest_dist={nearest_dist} > {BUFF_MAX_DIST}, "
                      f"skipping (too far -- buff would expire before we get hit)")

        # Re-use previous target if still alive (match by id, fall back
        # to cell). Re-pick only when locked target dies/despawns.
        target = None
        if locked_target_id:
            t = snap.fight_entities.get(locked_target_id)
            if t and t.alive and t.cell > 0:
                target = t
        if target is None and locked_target_cell:
            for e in enemies:
                if e.cell == locked_target_cell:
                    target = e
                    break

        if target is None:
            target = enemies[0]
            locked_target_id = target.id
            locked_target_cell = target.cell
            print(f"  locked new target id={target.id} cell={target.cell}")
        else:
            # Refresh cell anchor in case the mob walked between turns.
            locked_target_cell = target.cell

        dist = cell_distance(me_cell, target.cell) if me_cell else 99
        print(f"  target id={target.id} cell={target.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp} (locked)")

        if dist > 1 and me_cell and my_mp > 0:
            # Discard walk_toward's my_ap return -- it reads GTM which
            # can't see our intra-turn buff cast. Keep locally-tracked AP.
            me_cell, _ = walk_toward(target.cell, state, cal, static_obstacles)
            target = state.snapshot().fight_entities.get(target.id) or target
            dist = cell_distance(me_cell, target.cell) if me_cell else 99

        if dist == 1 and target.alive and my_ap >= FOOT_AP_COST:
            cast_foot_on(target.cell, cal)
            time.sleep(CAST_WAIT_SEC)
        elif dist == 1 and my_ap < FOOT_AP_COST:
            print(f"  adjacent but ap={my_ap} < cost={FOOT_AP_COST}; passing")
        elif dist > 1:
            print(f"  still not adjacent after walk (dist={dist}); passing")

        if not state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(ctx)


def main():
    parser = argparse.ArgumentParser(description="Proxy-driven Sacrieur auto-fighter")
    parser.add_argument("--min-hp", type=int, default=100,
                        help="wait until current HP >= this before engaging "
                             "(default 100; capped at max HP)")
    parser.add_argument("--max-group-size", type=int, default=0,
                        help="skip mob groups with more than N members "
                             "(default 0 = no cap). E.g. 3 ignores groups of 4+.")
    args = parser.parse_args()

    if not SPELL_HOTKEY:
        print("config.json is missing 'sacrid_foot_hotkey'. Set it to the key "
              "Sacrid Foot is bound to on Marx-Rockfeller's spell bar (e.g. \"1\").")
        sys.exit(1)

    cal = load_cal()
    print(f"[fighter] cal: origin=({cal['origin_x']:.1f},{cal['origin_y']:.1f}) "
          f"cell={cal['cell_w']:.2f}x{cal['cell_h']:.2f}")
    print(f"[fighter] spell_hotkey={SPELL_HOTKEY!r} ap_cost={FOOT_AP_COST}")
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

        while True:
            snap = state.snapshot()
            if snap.map_id != last_map_id:
                if ghosts:
                    print(f"[fighter] map changed {last_map_id} -> {snap.map_id}; "
                          f"clearing {len(ghosts)} ghost(s)")
                ghosts.clear()
                last_map_id = snap.map_id

            if snap.in_combat:
                ents = snap.fight_entities
                others = [e for e in ents.values() if e.id != snap.my_id]
                print(f"[fighter] phase=combat (map={snap.map_id}) "
                      f"entities={len(ents)} enemies={len(others)}, running sacrid combat")
                run_combat_sacrid(ctx, state, cal)
                # Enter dismisses any level-up popup, 1s gap lets the
                # XP summary take focus, then Esc closes it. The 4s
                # pre-wait gives both popups time to render.
                time.sleep(4.0)
                press_xdotool("Return")
                time.sleep(1.0)
                press_xdotool("Escape")
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
                    print(f"[fighter] phase=idle map={snap.map_id}: {reason}; "
                          f"walking {direction} (fresh={fresh}{skip_note}, "
                          f"avoid={excluded}) to switch cell={switch_cell} "
                          f"-> ({x},{y})")
                    ctx.click(x, y)
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
                    else:
                        print(f"[fighter] walk to {direction} switch cell did not "
                              f"change map in {MAP_CHANGE_TIMEOUT}s; will retry "
                              f"next tick")
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
                    print(f"[fighter] phase=idle map={snap.map_id} "
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
            ctx.click(x, y)
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
            ctx.click(ax, ay)
            if wait_for(state, lambda s: s.in_fight, ENGAGE_TIMEOUT):
                print(f"[fighter] fight_engage received (phase={state.snapshot().fight_phase})")
            else:
                ghosts.add((acell, amob.group_id))
                print(f"[fighter] next-nearest also didn't engage; marking ghost "
                      f"(total={len(ghosts)}); sleeping 3s")
                time.sleep(3.0)


if __name__ == "__main__":
    main()
