"""Engage the nearest mob, then cast Sacrid Foot on it each turn.

Character: Marx-Rockfeller (Sacrieur), berlinthree Ankama account.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999.
  - config.json has cell_calibration (run calibrate_cells.py).
  - config.sacrid_foot_hotkey is set to whatever 1-9 slot the spell lives
    in on Marx-Rockfeller's spell bar.

Loop is a tri-state mirror of the proxy's fight_phase:

  fight_phase == "idle":
    - As soon as any mob group is visible, click the nearest one (Dofus
      auto-walks into it). The proxy flips to "placement" the moment it
      sees GA;905;<myId>; on the wire, so the wait after the click is
      short (~5s); if it stays idle, the click missed -- fall back to a
      random other mob group, then sleep.

  fight_phase == "placement":
    - Just entered fight. Placement screen is up, 30s placement timer is
      counting. Pause briefly (fight_ready_wait_sec) then press the
      pass-turn hotkey to ready up. Proxy will flip to "combat" the
      moment the bare GS arrives.

  fight_phase == "combat":
    - run_combat_sacrid: per turn -- self-cast Strength Punishment buff
      once per fight on the first eligible turn, then walk adjacent to
      the closest alive enemy if needed, cast Sacrid Foot once if
      adjacent and AP >= cost, pass turn. Terminates when phase leaves
      "combat" (proxy publishes fight_end on the GE xp summary, or any
      map change).
    - On exit press Esc and confirm no dialog is left covering the game.

config.json knobs (Sacrid-specific):
  sacrid_foot_hotkey   : single-char key for the Sacrid Foot spell slot
                         (e.g. "2"). REQUIRED -- script refuses to start
                         if empty.
  sacrid_foot_ap_cost  : AP per Foot cast. Default 4 (retro Pied du Sacri).
  sacrid_buff_hotkey   : single-char key for the Strength Punishment
                         self-buff slot. Default "3".
  sacrid_buff_ap_cost  : AP per buff cast. Default 3. Used to locally
                         decrement AP after the buff so the rest of the
                         turn budgets correctly (GTM only updates AP at
                         turn boundaries).
  sacrid_cast_wait_sec : Sleep after each cast click so the GTM AP/HP
                         update can arrive over the proxy. Default 0.8.
  sacrid_walk_wait_sec : Max wait for my_cell to settle after a walk
                         click. Default 2.0.

The strategy is "one Foot cast per turn, then pass": Sacrid Foot has a
1/turn cap, so even with leftover AP (e.g. 6 AP - 4 cost = 2 left) we
don't try to cast it again. Strength Punishment is cast once on turn 1
(or the first turn AP >= BUFF_AP_COST allows) and never again that fight.

Ctrl+C to abort. All simulated input goes through utils.click / utils.press
(xdotool); no library calls are made from this module.
"""
import sys
import time

import mss

from cell_grid import a_star, cell_distance, cell_to_xy, neighbors
from dialogs import ensure_safe_to_resume
from fight import pass_turn
from map_data import load_all as load_map_data
from proxy_client import ProxyState
from utils import CFG, click, make_ctx, press, press_xdotool

MAP_DATA = load_map_data()  # {map_id: {"world", "map_id", "cells", "obstacles", ...}}

PROXY_ADDR = "127.0.0.1:9999"

SPELL_HOTKEY = CFG.get("sacrid_foot_hotkey")
FOOT_AP_COST = int(CFG.get("sacrid_foot_ap_cost", 4))
# Châtiment Force / Strength Punishment: Sacrieur self-buff, cast once at
# the start of each fight. Self-target -- hotkey + click own cell.
BUFF_HOTKEY = CFG.get("sacrid_buff_hotkey", "3")
BUFF_AP_COST = int(CFG.get("sacrid_buff_ap_cost", 3))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
WALK_WAIT_SEC = float(CFG.get("sacrid_walk_wait_sec", 2.0))
WALK_STEP_WAIT_SEC = float(CFG.get("sacrid_walk_step_wait_sec", 1.0))
WALK_MAX_STEPS = int(CFG.get("sacrid_walk_max_steps", 6))
# Pause between consecutive walk clicks. Dofus retro silently drops a
# click issued while another walk is still animating client-side
# (~300-500 ms per cell). Letting the previous step settle before the
# next click was the root cause of false "obstacle" entries -- the
# server had no problem with the destination cell, the client just
# wasn't listening yet.
WALK_STEP_SETTLE_SEC = float(CFG.get("sacrid_walk_step_settle_sec", 0.5))

# Delay between the proxy receiving GTS<myID> (server-pushed turn-start)
# and the bot firing its first click of the turn. Gives the Dofus client
# time to render the new turn (highlight, AP/MP bars) so spell clicks
# land instead of being eaten by the still-animating end-of-previous-turn.
TURN_START_SETTLE_SEC = float(CFG.get("turn_start_settle_sec", 1.5))
# Upper bound on how long we'll wait for our next GTS. One round is
# dur_ms (29s) * number_of_actors; 90s covers reasonable mob counts plus
# a placement-style stall.
TURN_WAIT_TIMEOUT_SEC = float(CFG.get("turn_wait_timeout_sec", 90.0))

# Time to wait for the proxy to report fight_phase != "idle" after we
# click a mob. GA;905;<myId>; arrives within ~1s of a successful engage
# (the character only has to walk the last tile or two before the engage
# packet fires), so 5s is plenty -- a longer wait was masking the bug
# where the proxy mis-classified GA;905; as fight_end.
ENGAGE_TIMEOUT = 5.0
# How long combat may take to begin after we ready up out of placement.
# Placement timer caps at 30s; we expect GS within a couple seconds of
# our ready press when no other players are involved.
COMBAT_START_TIMEOUT = 35.0
IDLE_POLL_SEC = 0.5
STATUS_LOG_SEC = 5.0


def load_cal():
    cal = CFG.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json. Run calibrate_cells.py.")
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


def nearest_mob(snap, ghosts=()):
    """(distance, cell, mob) for the closest mob group, or None.

    `ghosts` is an iterable of (cell, group_id) tuples we've already tried
    and failed to engage on the current map; entries matching any tuple
    are skipped. The proxy fix (handleEngage now removes the engaged group
    from s.mobs) handles the common case; ghosts catches everything else
    (groups despawned by other players, missed GM|-, etc.).

    If `my_cell` isn't known yet (proxy just attached and we haven't walked
    since), distances are meaningless -- return an arbitrary mob with
    distance=-1 so the caller still engages instead of stalling forever."""
    ghost_set = set(ghosts)
    candidates = [
        (c, m) for c, m in snap.mobs.items()
        if (c, m.group_id) not in ghost_set
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
    """Pick the next cell to step into, toward `target_cell`.

    Single-cell move, not a full-MP walk: we plan with A*, take one step,
    re-check proxy state, and re-plan next iteration.

    Two-tier blocked set: dynamic obstacles (other live entities) are
    transient -- they move on their own turn -- so we do NOT include
    them in the A* plan, else one mob squatting on the optimal corridor
    pins us in place for the whole fight. Instead we plan against the
    static map and only veto the **immediate** next step if it would
    walk into a live mob, falling back to a greedy neighbor pick in
    that case.

    Blocked sets:
      - A* plan:       static_obstacles + recent_failed (minus target).
      - Greedy fallback / next-step veto:
                       static + dynamic (other live entities) + recent_failed.

    Returns the cell to step into, or None if no walkable neighbor
    strictly improves Po distance to `target_cell`."""
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

    MP tracking: the proxy only writes fight_entities[my].mp from GTM
    packets, which fire at turn boundaries -- not after each mid-turn
    walk. Reading mp from the snapshot every iteration gives the
    turn-start value forever, so the loop has no idea when we've spent
    all our MP. We read mp ONCE at entry and decrement locally by
    cell_distance(before, after) per step.

    Animation timing: Dofus retro silently drops a walk click issued
    while another walk is still animating client-side. Sleep
    WALK_STEP_SETTLE_SEC after each successful step before the next
    click. On click failure, settle and retry the SAME cell once. If
    the retry also fails, give up on that cell for the rest of this
    turn (in-memory only -- no persistence).

    Termination:
      - Po distance to target_cell reaches 1 (caller will cast),
      - estimated MP reaches 0,
      - no neighbor strictly improves Po distance,
      - WALK_MAX_STEPS safety cap reached."""
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

    Per turn:
      0. Block until proxy reports GTS<myID> (server-pushed turn-start),
         then sleep TURN_START_SETTLE_SEC so the Dofus client has finished
         rendering the new turn (highlight, AP/MP bars). Acting earlier
         risks the spell hotkey or click landing while the previous
         actor's end-of-turn animation still has input focus.
      1. First eligible turn only: self-cast Strength Punishment (buff)
         once per fight, then locally decrement AP by BUFF_AP_COST so the
         rest of the turn budgets correctly. GTM only refreshes AP at
         turn boundaries, so we can't re-read it mid-turn.
      2. Find closest alive enemy from proxy fight_entities.
      3. If not adjacent, mini-step toward it (1 MP per click).
      4. If adjacent and AP >= FOOT_AP_COST, cast Foot once (hotkey +
         target-click).
      5. pass_turn. Returns immediately; the next iteration's step 0
         re-blocks on our next GTS, so no blind sleep is needed here.

    Terminates when fight_phase leaves "combat" (fight_end via GE, or
    GDM map-change fallback)."""
    my_id = state.snapshot().my_id
    last_turn_n = 0
    buff_cast = False
    # Static obstacles are calibrated per-map and don't move; load once.
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

        if not buff_cast and me_cell and my_ap >= BUFF_AP_COST:
            cast_strength_punishment(me_cell, cal)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= BUFF_AP_COST
            buff_cast = True
            print(f"  buff cast; ap_left~{my_ap}")

        enemies = alive_enemies(snap)
        if not enemies:
            print("  no alive enemies in snapshot; passing")
            pass_turn(ctx)
            continue

        target = enemies[0]
        dist = cell_distance(me_cell, target.cell) if me_cell else 99
        print(f"  target id={target.id} cell={target.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")

        if dist > 1 and me_cell and my_mp > 0:
            # walk_toward returns (me_cell, my_ap) but walking doesn't
            # consume AP -- and its AP value comes from the turn-bounded
            # GTM, which can't see our intra-turn buff cast. Keep our
            # locally-tracked my_ap instead.
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
    if not SPELL_HOTKEY:
        print("config.json is missing 'sacrid_foot_hotkey'. Set it to the key "
              "Sacrid Foot is bound to on Marx-Rockfeller's spell bar (e.g. \"1\").")
        sys.exit(1)

    cal = load_cal()
    print(f"[fighter] cal: origin=({cal['origin_x']:.1f},{cal['origin_y']:.1f}) "
          f"cell={cal['cell_w']:.2f}x{cal['cell_h']:.2f}")
    print(f"[fighter] spell_hotkey={SPELL_HOTKEY!r} ap_cost={FOOT_AP_COST}")

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
        # Local "ghost" set: (cell, group_id) tuples we've clicked but
        # failed to engage on the current map. Cleared on map_id change.
        # The proxy preemptively drops engaged groups from s.mobs, but
        # this catches everything that slips past (missed GM|-, other
        # players engaging a group whose despawn we missed, etc.).
        ghosts = set()
        last_map_id = snap.map_id

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
                # Wait for the XP-summary popup to actually finish
                # rendering before we Esc it; 1s was too short.
                time.sleep(4.0)
                press_xdotool("Escape")
                time.sleep(0.3)
                if not ensure_safe_to_resume(ctx):
                    print("[fighter] menu still open after Esc -- aborting")
                    sys.exit(1)
                continue

            if snap.in_placement:
                # Placement screen up after GA;905; engage. Ready up so we
                # don't burn the full 30s placement timer; the proxy will
                # flip phase to "combat" the moment bare GS arrives.
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
            near = nearest_mob(snap, ghosts)
            if near is None:
                now = time.time()
                if now - last_status_ts > STATUS_LOG_SEC:
                    print(f"[fighter] phase=idle map={snap.map_id} "
                          f"my_cell={snap.my_cell} no mobs visible "
                          f"(ghosts={len(ghosts)})")
                    last_status_ts = now
                time.sleep(IDLE_POLL_SEC)
                continue
            d, cell, mob = near
            # Re-snapshot right before clicking: mobs wander every few seconds
            # and our `snap` is up to IDLE_POLL_SEC stale. The proxy keeps
            # s.mobs current via GA0;1; (re-keyed by group_id) and GM|-
            # (despawn), so we just verify the group we picked is still at
            # the picked cell -- otherwise we'd click an empty tile and burn
            # ENGAGE_TIMEOUT before falling back.
            fresh = state.snapshot().mobs.get(cell)
            if fresh is None or fresh.group_id != mob.group_id:
                print(f"[fighter] mob group={mob.group_id} moved/despawned "
                      f"from cell={cell} before click; re-picking next tick")
                continue
            x, y = cell_to_screen(cell, cal)
            print(f"[fighter] engaging nearest mob: cell={cell} dist={d} "
                  f"group={mob.group_id} members={mob.members} -> screen=({x},{y})")
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
            alt = nearest_mob(fresh_snap, ghosts)
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
