"""Sacrieur: per-turn brain plus class-specific spell knowledge.

Owns the walking helpers (used during combat to close on enemies) and
the tofu detector (kiter retreat mode). play_turn(ctx) is the body of
a single combat turn: walk, cast, bow, follow-up, pass. The class
holds fight-scoped state (tofu detector, buff cooldown) that
on_fight_engaged resets at the start of each fight.

Wired via Combat.on_turn_start(sacrieur.play_turn) and
Combat.on_fight_engaged(sacrieur.on_fight_engaged) in Orchestrator.
"""
import random
import time

from dofus.actions import cast_at_cell, pass_turn
from dofus.cell_grid import (
    a_star, cell_distance, cell_to_screen, line_of_sight, neighbors, on_map,
)
from dofus.map_data import save as save_map_data
from mouse_keyboard import click_at
from fighter.helpers import alive_enemies, my_fight_cell, wait_for
from utils import CFG


# === Config-driven knobs ===
DISSOLUTION_HOTKEY = CFG.get("sacrid_dissolution_hotkey")
DISSOLUTION_AP_COST = int(CFG.get("sacrid_dissolution_ap_cost", 4))
BOW_HOTKEY = CFG.get("sacrid_bow_hotkey", "0")
BOW_AP_COST = int(CFG.get("sacrid_bow_ap_cost", 4))
BOW_MIN_RANGE = int(CFG.get("sacrid_bow_min_range", 2))
BOW_MAX_RANGE = int(CFG.get("sacrid_bow_max_range", 6))
BOW_POST_WALK_EXTRA_SETTLE_SEC = float(CFG.get("sacrid_bow_post_walk_settle_sec", 0.33))
BUFF_HOTKEY = CFG.get("sacrid_buff_hotkey", "3")
BUFF_AP_COST = int(CFG.get("sacrid_buff_ap_cost", 3))
BUFF_MAX_DIST = int(CFG.get("sacrid_buff_max_dist", 6))
BUFF_COOLDOWN_TURNS = int(CFG.get("sacrid_buff_cooldown_turns", 5))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
PASS_TURN_HOTKEY = CFG.get("pass_turn_hotkey", "e")
PASS_TURN_PRE_DELAY_SEC = float(CFG.get("pass_turn_pre_delay_sec", 1.5))
WALK_STEP_WAIT_SEC = float(CFG.get("sacrid_walk_step_wait_sec", 1.0))
WALK_STEP_FAST_FAIL_SEC = float(CFG.get("sacrid_walk_step_fast_fail_sec", 0.6))
WALK_MAX_STEPS = int(CFG.get("sacrid_walk_max_steps", 6))
WALK_STEP_SETTLE_SEC = float(CFG.get("sacrid_walk_step_settle_sec", 0.5))
FULL_WALK_SETTLE_FLOOR_SEC = float(CFG.get("full_walk_settle_floor_sec", 1.2))
TOFU_THRESHOLD = int(CFG.get("tofu_detect_threshold", 4))
TOFU_REQUIRED_CYCLES = int(CFG.get("tofu_detect_required_cycles", 3))


class TurnDistanceTracker:
    """Detects hit-and-run "tofu-like" enemies via turn-start distances.

    Called once per our turn-start with the distance to the nearest
    alive enemy BEFORE we move. That snapshot is the cycle's "max" --
    where the enemy ended up after retreating. If the last `required`
    samples are all > `threshold` AND the sequence is not strictly
    decreasing, flip tofu_detected. Sampling mid-cycle would conflate
    enemy approach distance with our own post-move position."""

    def __init__(self, threshold, required_cycles):
        self.threshold = threshold
        self.required = required_cycles
        self.history = []
        self.tofu_detected = False

    def observe_turn_start(self, dist):
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


# === Walking helpers (used by Sacrieur during combat) ===

def _path_repr(path, max_cells=10):
    if path is None:
        return "None"
    if len(path) <= max_cells:
        return str(path)
    return f"{path[:max_cells]}+{len(path) - max_cells}more"


def pick_next_step(me_cell, target_cell, snap, recent_failed, static_obstacles):
    """One cell to step into toward target. None if no walkable neighbour
    strictly improves Po distance. Two-tier blocked set: A* plans against
    static obstacles only (a mob squatting in the corridor would otherwise
    pin us); the immediate next step is then vetoed if it walks into a
    live entity, falling back to a greedy neighbour pick."""
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

    current_dist = cell_distance(me_cell, target_cell)
    cands = []
    for n in neighbors(me_cell):
        if cell_distance(n, me_cell) != 1:
            continue
        if not on_map(n):
            continue
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
    INCREASES Po distance from `away_from`. No A* (no destination)."""
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


def _wait_movement(state, before, timeout):
    return wait_for(
        state,
        lambda s, b=before: my_fight_cell(s) != b and my_fight_cell(s) > 0,
        timeout,
    )


def walk_away(away_from, state, cal, static_obstacles, max_steps):
    """Step-by-step retreat. Picks neighbours that strictly increase Po
    distance. Stops at max_steps, no MP, no valid neighbour, or movement
    failure. Returns (me_cell, mp_remaining, pending_settle)."""
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
            print(f"  no retreat neighbour from {me_cell} (away from {away_from})")
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


def try_full_walk(target_cell, state, cal, static_obstacles=(), mp_override=None,
                  walk_wait_sec=None):
    """One-click walk that spends all current MP toward target_cell.
    Returns (success, me_cell, mp_remaining, pending_settle_sec)."""
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
    if not path or len(path) < 3:
        print(f"    [full_walk] no usable path (len={len(path) if path else 0}); "
              f"caller falls back to step-by-step")
        return False, me_cell, mp, 0.0

    max_steps = min(mp, len(path) - 2)
    if max_steps <= 0:
        return False, me_cell, mp, 0.0

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

    pending_settle = max(WALK_STEP_SETTLE_SEC * max_steps, FULL_WALK_SETTLE_FLOOR_SEC)
    new_cell = my_fight_cell(state.snapshot()) or me_cell
    mp_used = cell_distance(me_cell, new_cell)
    remaining = mp - mp_used
    print(f"    full walk landed {new_cell} (mp_used={mp_used} "
          f"mp_left~{remaining} pending_settle={pending_settle:.2f}s)")
    return True, new_cell, remaining, pending_settle


def walk_toward(target_cell, state, cal, static_obstacles=(), mp_override=None,
                fast_fail=False):
    """Walk toward target_cell. First tries full-MP single-click via
    try_full_walk; on failure falls through to step-by-step. Returns
    (me_cell, my_ap, mp_remaining, pending_settle_sec). `fast_fail`
    bails on the first failed step (no retry); used for the
    post-Dissolution follow-up walk to skip dead-step latency."""
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
                print(f"    landed {new_cell} (expected {step}; Dofus pathed "
                      f"differently); mp_used={mp_used} mp_left~{estimated_mp}")
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


# === Sacrieur class: spell choices + per-turn brain ===

class Sacrieur:
    """Per-turn decision logic for the Sacrieur character. Owns spells
    (Dissolution self-cast AoE, Strength Punishment self-buff, bow
    ranged) and fight-scoped state (tofu detector, buff cooldown,
    static obstacles for the current map).

    play_turn(ctx) is registered on Combat.on_turn_start.
    on_fight_engaged(snap) is registered on Combat.on_fight_engaged."""

    def __init__(self, state, cal, map_data, buff_enabled=True):
        self.state = state
        self.cal = cal
        self.map_data = map_data
        self.buff_enabled = buff_enabled
        # Fight-scoped state -- reset on on_fight_engaged.
        self.last_buff_turn = -BUFF_COOLDOWN_TURNS
        self.static_obstacles: set[int] = set()
        self.dist_tracker = TurnDistanceTracker(TOFU_THRESHOLD, TOFU_REQUIRED_CYCLES)

    # --- Combat callbacks ---

    def on_fight_engaged(self, snap):
        """Reset fight-scoped state. Loads static obstacles for the
        current map and prunes any that overlap live entities."""
        self.last_buff_turn = -BUFF_COOLDOWN_TURNS
        map_id = snap.map_id
        self._prune_obstacles_from_entities(map_id, snap)
        self.static_obstacles = set(
            (self.map_data.get(map_id) or {}).get("obstacles") or ()
        )
        if self.static_obstacles:
            print(f"  loaded {len(self.static_obstacles)} static obstacle(s) "
                  f"for map={map_id}")
        self.dist_tracker = TurnDistanceTracker(TOFU_THRESHOLD, TOFU_REQUIRED_CYCLES)

    def play_turn(self, ctx):
        """Body of one combat turn. Called via Combat.on_turn_start.
        Calls pass_turn at the end -- Combat doesn't manage AP/MP."""
        new_turn = ctx.turn_n
        snap = ctx.snap
        me_cell = my_fight_cell(snap)
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        my_mp = me.mp if me else 0

        enemies = alive_enemies(snap)
        if not enemies:
            print("  no alive enemies in snapshot; passing")
            pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)
            return

        was_tofu = self.dist_tracker.tofu_detected
        turn_start_dist = (cell_distance(me_cell, enemies[0].cell)
                           if me_cell else None)
        recorded = self.dist_tracker.observe_turn_start(turn_start_dist)
        if recorded is not None:
            print(f"  [tofu-track] turn-start dist={recorded} "
                  f"(history={self.dist_tracker.history[-TOFU_REQUIRED_CYCLES:]})")
        if self.dist_tracker.tofu_detected and not was_tofu:
            print(f"  [tofu] hit-and-run pattern detected: last "
                  f"{TOFU_REQUIRED_CYCLES} turn-start distances all "
                  f"> {TOFU_THRESHOLD} and not strictly decreasing; "
                  f"switching to retreat mode for the rest of this fight")

        if self.dist_tracker.tofu_detected:
            if self._play_tofu_turn(me_cell, my_ap, my_mp, enemies, new_turn):
                return  # handled in tofu branch, turn passed

        # === Normal combat ===

        # Buff with cooldown; distance-gated.
        nearest_dist = (cell_distance(me_cell, enemies[0].cell)
                        if me_cell else 99)
        turns_since_buff = new_turn - self.last_buff_turn
        buff_ready = turns_since_buff >= BUFF_COOLDOWN_TURNS
        if not self.buff_enabled:
            pass
        elif buff_ready and me_cell and my_ap >= BUFF_AP_COST:
            if nearest_dist <= BUFF_MAX_DIST:
                print(f"  buff: nearest_dist={nearest_dist} <= {BUFF_MAX_DIST}, "
                      f"cooldown ready (last_cast_turn={self.last_buff_turn}), casting")
                self._cast_strength_punishment(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= BUFF_AP_COST
                self.last_buff_turn = new_turn
                print(f"  buff cast on turn {new_turn}; ap_left~{my_ap} "
                      f"(next available turn {new_turn + BUFF_COOLDOWN_TURNS})")
            else:
                print(f"  buff: nearest_dist={nearest_dist} > {BUFF_MAX_DIST}, "
                      f"skipping (too far -- buff would expire before we get hit)")
        elif not buff_ready:
            print(f"  buff: on cooldown ({turns_since_buff}/{BUFF_COOLDOWN_TURNS} "
                  f"turns since last cast on turn {self.last_buff_turn})")

        # Close on nearest enemy, then Dissolution if adjacent.
        nearest = enemies[0]
        dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
        print(f"  nearest id={nearest.id} cell={nearest.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")

        mp_remaining = my_mp
        pending_settle = 0.0
        if dist > 1 and me_cell and mp_remaining > 0:
            me_cell, _, mp_remaining, pending_settle = walk_toward(
                nearest.cell, self.state, self.cal, self.static_obstacles)
            enemies = alive_enemies(self.state.snapshot())
            dist = (cell_distance(me_cell, enemies[0].cell)
                    if enemies and me_cell else 99)

        if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
            if pending_settle > 0:
                time.sleep(pending_settle)
                pending_settle = 0.0
            self._cast_dissolution(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= DISSOLUTION_AP_COST
        elif dist == 1 and my_ap < DISSOLUTION_AP_COST:
            print(f"  adjacent but ap={my_ap} < cost={DISSOLUTION_AP_COST}; "
                  f"not casting this turn")
        elif dist > 1:
            print(f"  nothing adjacent (nearest_dist={dist}); not casting this turn")

        # Bow burst with leftover AP. Clears tofu_detected if any hit.
        if self.state.snapshot().in_combat and me_cell and my_ap >= BOW_AP_COST:
            if pending_settle > 0:
                time.sleep(pending_settle + BOW_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            my_ap, bow_shots = self._fire_bow_burst(my_ap, me_cell)
            if bow_shots > 0 and self.dist_tracker.tofu_detected:
                print(f"  [tofu] bow connected ({bow_shots} shot(s)) from "
                      f"normal-combat fall-through; exiting retreat mode")
                self.dist_tracker.tofu_detected = False

        # Follow-up walk: close on a more-distant enemy with leftover MP.
        if self.state.snapshot().in_combat and mp_remaining > 0 and me_cell:
            distant = [e for e in alive_enemies(self.state.snapshot())
                       if cell_distance(me_cell, e.cell) > 1]
            if distant:
                follow = distant[0]
                follow_dist = cell_distance(me_cell, follow.cell)
                print(f"  follow-up walk: closing toward id={follow.id} "
                      f"cell={follow.cell} dist={follow_dist} "
                      f"mp_left={mp_remaining} (fast-fail)")
                me_cell, _, mp_remaining, _ = walk_toward(
                    follow.cell, self.state, self.cal, self.static_obstacles,
                    mp_override=mp_remaining, fast_fail=True)

        if not self.state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)

    def _play_tofu_turn(self, me_cell, my_ap, my_mp, enemies, new_turn):
        """Tofu retreat branch. Returns True if the turn was handled
        (caller returns immediately), False to fall through to normal
        combat (cornered case)."""
        nearest = enemies[0]
        dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
        cells_to_close = max(0, dist - 1)
        can_reach = me_cell is not None and cells_to_close <= my_mp
        can_attack = my_ap >= DISSOLUTION_AP_COST
        will_attack = (dist == 1 or can_reach) and can_attack
        can_retreat = me_cell is not None and pick_retreat_step(
            me_cell, nearest.cell, self.state.snapshot(),
            set(), set(self.static_obstacles)) is not None
        can_bow = (my_ap >= BOW_AP_COST
                   and self._pick_bow_target(self.state.snapshot(), me_cell)
                       is not None)
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
            return False  # fall through

        print(f"  [tofu] nearest id={nearest.id} cell={nearest.cell} "
              f"dist={dist} my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")
        mp_remaining = my_mp
        pending_settle = 0.0
        if can_reach and can_attack:
            if dist > 1:
                print(f"  [tofu] closing to attack (need {cells_to_close} "
                      f"mp, have {my_mp}; ap={my_ap})")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    nearest.cell, self.state, self.cal, self.static_obstacles)
                enemies = alive_enemies(self.state.snapshot())
                dist = (cell_distance(me_cell, enemies[0].cell)
                        if enemies and me_cell else 99)
            if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
                if pending_settle > 0:
                    time.sleep(pending_settle)
                self._cast_dissolution(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= DISSOLUTION_AP_COST
                pending_settle = 0.0
        elif can_walk_to_bow:
            expected_landing = dist - steps_into_bow
            print(f"  [tofu] closing {steps_into_bow} step(s) toward "
                  f"bow range (dist={dist} -> ~{expected_landing}, "
                  f"bow_range={BOW_MIN_RANGE}..{BOW_MAX_RANGE}, "
                  f"mp={my_mp}, ap={my_ap})")
            before_cell = me_cell
            me_cell, _, _, pending_settle = walk_toward(
                nearest.cell, self.state, self.cal, self.static_obstacles,
                mp_override=steps_into_bow)
            steps_used = (cell_distance(before_cell, me_cell)
                          if me_cell and before_cell else 0)
            mp_remaining = max(0, my_mp - steps_used)
        elif dist == 1 and can_attack:
            self._cast_dissolution(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= DISSOLUTION_AP_COST
        elif dist == 1:
            print(f"  [tofu] adjacent but ap={my_ap} < "
                  f"{DISSOLUTION_AP_COST}; not casting")

        if self.state.snapshot().in_combat and me_cell:
            if pending_settle > 0:
                time.sleep(pending_settle + BOW_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            my_ap, bow_shots = self._fire_bow_burst(my_ap, me_cell)
            if bow_shots > 0 and self.dist_tracker.tofu_detected:
                print(f"  [tofu] bow connected ({bow_shots} shot(s)); "
                      f"exiting retreat mode -- we can hit them at "
                      f"range, no need to keep kiting back")
                self.dist_tracker.tofu_detected = False

        if mp_remaining > 0 and me_cell:
            live = alive_enemies(self.state.snapshot())
            if live:
                anchor = live[0].cell
                steps = random.randint(0, mp_remaining)
                print(f"  [tofu] retreating {steps} step(s) away from "
                      f"cell={anchor} (mp_left={mp_remaining})")
                if steps > 0:
                    walk_away(anchor, self.state, self.cal,
                              self.static_obstacles, max_steps=steps)
        if not self.state.snapshot().in_combat:
            return True
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)
        return True

    # --- Spell casts (thin wrappers around dofus.actions.cast_at_cell) ---

    def _cast_dissolution(self, my_cell):
        print(f"  CAST Dissolution hotkey={DISSOLUTION_HOTKEY!r} self_cell={my_cell}")
        cast_at_cell(DISSOLUTION_HOTKEY, my_cell, self.cal)

    def _cast_strength_punishment(self, my_cell):
        print(f"  CAST Strength Punishment hotkey={BUFF_HOTKEY!r} self_cell={my_cell}")
        cast_at_cell(BUFF_HOTKEY, my_cell, self.cal)

    def _cast_bow(self, target_cell):
        print(f"  CAST Bow hotkey={BOW_HOTKEY!r} target_cell={target_cell}")
        cast_at_cell(BOW_HOTKEY, target_cell, self.cal)

    # --- Bow targeting ---

    def _pick_bow_target(self, snap, me_cell, debug=False):
        """Nearest alive enemy in [BOW_MIN_RANGE, BOW_MAX_RANGE] with
        LoS, or None. LoS blockers = static_obstacles + other live
        entities (excluding the target's own cell)."""
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
            blockers = set(self.static_obstacles) | (other_alive - {e.cell})
            if not line_of_sight(me_cell, e.cell, blockers):
                if debug:
                    mob_blockers = sorted(other_alive - {e.cell})
                    rejections.append(
                        f"id={e.id} cell={e.cell} dist={d} LoS-blocked "
                        f"(other_alive={mob_blockers}, "
                        f"static_obstacles={len(self.static_obstacles)})"
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

    def _fire_bow_burst(self, my_ap, me_cell):
        """Fire bow shots until AP < cost, no eligible target, or
        combat ends. Returns (updated_ap, shots_fired)."""
        shots = 0
        while my_ap >= BOW_AP_COST:
            snap = self.state.snapshot()
            if not snap.in_combat:
                return my_ap, shots
            target = self._pick_bow_target(snap, me_cell, debug=(shots == 0))
            if target is None:
                return my_ap, shots
            d = cell_distance(me_cell, target.cell)
            print(f"  bow: targeting id={target.id} cell={target.cell} dist={d} "
                  f"ap_before={my_ap}")
            self._cast_bow(target.cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= BOW_AP_COST
            shots += 1
        return my_ap, shots

    # --- Obstacle hygiene ---

    def _prune_obstacles_from_entities(self, map_id, snap):
        """Drop obstacle cells that overlap live entities or saved
        start cells. Calibration sometimes mis-clicks mob spawn cells
        as obstacles; this prunes them when we observe a fight there."""
        entry = self.map_data.get(map_id)
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
