"""Class-agnostic combat-walking primitives.

The fighter classes (Sacrieur, Enutrof, ...) all need the same in-fight
locomotion: walk toward a target cell, walk away from one, peek the
next greedy step. This module owns those building blocks so the per-
class brains can compose them without re-implementing.

Two strategies layered:

- `try_full_walk(target, ...)` — one click that spends all current MP
  toward `target` in a single Dofus pathfind. Fastest happy path; bails
  if the A* path is blocked by dynamic entities mid-way.

- step-by-step (inside `walk_toward` after a full-walk failure, and in
  `walk_away`) — click one neighbour at a time, retry once on
  unresponsive movement, and remember cells that didn't work this turn
  in `recent_failed` so we don't keep banging the same blocked cell.

`pick_next_step` and `pick_retreat_step` are the pure picks (no clicks
issued); they're exported because callers occasionally need to ask
"would there be a step here?" without committing to walking it.

Config knobs (read from `utils.CFG`, default in parens):
  - `sacrid_walk_step_wait_sec`       (1.0) — per-step wait for the
        post-click my_cell update. Keep the `sacrid_` prefix for
        backwards compatibility; the helpers are not Sacrieur-specific.
  - `sacrid_walk_step_fast_fail_sec`  (0.6) — same window when caller
        passes fast_fail=True (Sacrieur's post-Dissolution follow-up
        uses this to skip dead-step latency).
  - `sacrid_walk_max_steps`           (6)   — step-by-step iteration
        cap, a safety net for runaway loops.
  - `sacrid_walk_step_settle_sec`     (0.5) — sleep between successive
        step clicks so animations don't trample each other.
  - `full_walk_settle_floor_sec`      (1.2) — minimum pending_settle
        returned from a successful full walk (multi-step animations
        finish well after the click ack).
"""
import time

from dofus.cell_grid import (
    a_star, cell_distance, cell_to_screen, neighbors, on_map,
)
from mouse_keyboard import click_at
from fighter.helpers import my_fight_cell, wait_for
from utils import CFG


WALK_STEP_WAIT_SEC = float(CFG.get("sacrid_walk_step_wait_sec", 1.0))
WALK_STEP_FAST_FAIL_SEC = float(CFG.get("sacrid_walk_step_fast_fail_sec", 0.6))
WALK_MAX_STEPS = int(CFG.get("sacrid_walk_max_steps", 6))
WALK_STEP_SETTLE_SEC = float(CFG.get("sacrid_walk_step_settle_sec", 0.5))
FULL_WALK_SETTLE_FLOOR_SEC = float(CFG.get("full_walk_settle_floor_sec", 1.2))


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
