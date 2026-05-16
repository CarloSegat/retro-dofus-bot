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
    - run_combat_sacrid: per turn -- walk adjacent to the closest alive
      enemy if needed, cast Sacrid Foot once if adjacent and AP >= cost,
      pass turn. Terminates when phase leaves "combat" (proxy publishes
      fight_end on the GE xp summary, or any map change).
    - On exit press Esc and confirm no dialog is left covering the game.

config.json knobs (Sacrid-specific):
  sacrid_foot_hotkey   : single-char key for the Sacrid Foot spell slot
                         (e.g. "2"). REQUIRED -- script refuses to start
                         if empty.
  sacrid_foot_ap_cost  : AP per cast. Default 4 (retro Pied du Sacri).
  sacrid_cast_wait_sec : Sleep after the target-click so the GTM AP/HP
                         update can arrive over the proxy. Default 0.8.
  sacrid_walk_wait_sec : Max wait for my_cell to settle after a walk
                         click. Default 2.0.

The strategy is "one cast per turn, then pass": Sacrid Foot has a 1/turn
cap, so even with leftover AP (e.g. 6 AP - 4 cost = 2 left) we don't try
to cast again.

Ctrl+C to abort. All simulated input goes through utils.click / utils.press
(xdotool); no library calls are made from this module.
"""
import sys
import time

import mss

from cell_grid import cell_distance, cell_to_xy, neighbors
from dialogs import ensure_safe_to_resume
from fight import pass_turn
from proxy_client import ProxyState
from utils import CFG, click, make_ctx, press, press_xdotool

PROXY_ADDR = "127.0.0.1:9999"

SPELL_HOTKEY = CFG.get("sacrid_foot_hotkey")
FOOT_AP_COST = int(CFG.get("sacrid_foot_ap_cost", 4))
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


def nearest_mob(snap):
    """(distance, cell, mob) for the closest mob group, or None.

    If `my_cell` isn't known yet (proxy just attached and we haven't walked
    since), distances are meaningless -- return an arbitrary mob with
    distance=-1 so the caller still engages instead of stalling forever."""
    if not snap.mobs:
        return None
    if snap.my_cell == 0:
        cell, mob = next(iter(snap.mobs.items()))
        return (-1, cell, mob)
    items = [(cell_distance(snap.my_cell, c), c, m) for c, m in snap.mobs.items()]
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


def pick_next_step(me_cell, target_cell, snap, recent_failed):
    """Pick the best edge-adjacent neighbor to step into this turn.

    Single-cell move, not a full-MP walk: we step one tile, re-check
    proxy state, and decide again.

    Filters:
      - off-grid wraps (Po distance != 1 from me_cell)
      - alive entity cells (dynamic obstacles -- other mobs, us)
      - recent_failed: cells we already tried and failed *this turn*
        (in-memory only -- no persistence, just so the loop doesn't
        infinite-retry the same cell within a single walk_toward call)

    Picks the neighbor that minimises Po distance to `target_cell`.
    Returns None if no candidate is strictly closer than `me_cell`."""
    occupied = {
        e.cell for e in snap.fight_entities.values() if e.alive and e.cell > 0
    }
    current_dist = cell_distance(me_cell, target_cell)
    cands = []
    for n in neighbors(me_cell):
        if cell_distance(n, me_cell) != 1:
            continue  # off-grid wrap
        if n in occupied or n in recent_failed:
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
    saved order). No-op if no entry for snap.map_id."""
    fsp = CFG.get("fight_start_positions", {}) or {}
    entry = fsp.get(str(snap.map_id))
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


def _wait_movement(state, before, timeout):
    """True iff my_fight_cell moves away from `before` within `timeout`."""
    return wait_for(
        state,
        lambda s, b=before: my_fight_cell(s) != b and my_fight_cell(s) > 0,
        timeout,
    )


def walk_toward(target_cell, state, cal):
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
        step = pick_next_step(me_cell, target_cell, state.snapshot(), recent_failed)
        if step is None:
            print(f"  no walkable neighbor improves dist={dist} "
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
      1. Find closest alive enemy from proxy fight_entities.
      2. If not adjacent, mini-step toward it (1 MP per click).
      3. If adjacent and AP >= FOOT_AP_COST, cast Foot once (hotkey +
         target-click).
      4. pass_turn. Returns immediately; the next iteration's step 0
         re-blocks on our next GTS, so no blind sleep is needed here.

    Terminates when fight_phase leaves "combat" (fight_end via GE, or
    GDM map-change fallback)."""
    my_id = state.snapshot().my_id
    last_turn_n = 0

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

        target = enemies[0]
        dist = cell_distance(me_cell, target.cell) if me_cell else 99
        print(f"  target id={target.id} cell={target.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")

        if dist > 1 and me_cell and my_mp > 0:
            me_cell, my_ap = walk_toward(target.cell, state, cal)
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

        while True:
            snap = state.snapshot()

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
            near = nearest_mob(snap)
            if near is None:
                now = time.time()
                if now - last_status_ts > STATUS_LOG_SEC:
                    print(f"[fighter] phase=idle map={snap.map_id} "
                          f"my_cell={snap.my_cell} no mobs visible")
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
            # No engage. Re-pick nearest from a *fresh* snapshot (the mob we
            # just clicked may have moved during our click; the next-nearest
            # is more useful than a random pick).
            fresh_snap = state.snapshot()
            others = [(c, m) for c, m in fresh_snap.mobs.items() if c != cell]
            if not others:
                print(f"[fighter] no other mob groups to try; sleeping 3s")
                time.sleep(3.0)
                continue
            me = fresh_snap.my_cell
            if me:
                others.sort(key=lambda cm: cell_distance(me, cm[0]))
            acell, amob = others[0]
            d2 = cell_distance(me, acell) if me else -1
            ax, ay = cell_to_screen(acell, cal)
            print(f"[fighter] nearest didn't engage; trying next-nearest mob: "
                  f"cell={acell} dist={d2} group={amob.group_id} "
                  f"members={amob.members} -> screen=({ax},{ay})")
            ctx.click(ax, ay)
            if wait_for(state, lambda s: s.in_fight, ENGAGE_TIMEOUT):
                print(f"[fighter] fight_engage received (phase={state.snapshot().fight_phase})")
            else:
                print(f"[fighter] next-nearest also didn't engage; sleeping 3s")
                time.sleep(3.0)


if __name__ == "__main__":
    main()
