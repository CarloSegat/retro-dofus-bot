"""Engage the nearest mob, then cast Sacrid Foot on it each turn.

Character: Marx-Rockfeller (Sacrieur), berlinthree Ankama account.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999.
  - config.json has cell_calibration (run calibrate_cells.py).
  - config.sacrid_foot_hotkey is set to whatever 1-9 slot the spell lives
    in on Marx-Rockfeller's spell bar.

Loop:
  Out of fight:
    - As soon as any mob group is visible, click the nearest one (Dofus
      auto-walks to it). Wait for proxy fight_start.
  In fight:
    - run_combat_sacrid: pass placement, then each turn -- walk adjacent
      to the closest alive enemy if needed, cast Sacrid Foot once if
      adjacent and AP >= cost, pass turn.
    - On fight_end (proxy), press Esc, ensure no menu, back to idle.

config.json knobs (Sacrid-specific):
  sacrid_foot_hotkey   : pyautogui key for the Sacrid Foot slot. REQUIRED.
                         No default -- script will refuse to start without
                         it so a placeholder doesn't silently miscast.
  sacrid_foot_ap_cost  : AP per cast. Default 4 (retro Pied du Sacri).
  sacrid_cast_wait_sec : Sleep after the target-click so the GTM AP/HP
                         update can arrive over the proxy. Default 0.8.
  sacrid_walk_wait_sec : Max wait for my_cell to settle after a walk
                         click. Default 2.0.

The strategy is "one cast per turn, then pass": Sacrid Foot has a 1/turn
cap, so even with leftover AP (e.g. 6 AP - 4 cost = 2 left) we don't try
to cast again.

Move mouse to top-left for pyautogui FAILSAFE abort.
"""
import random
import sys
import time

import mss
import pyautogui

from cell_grid import cell_distance, cell_to_xy, neighbors
from dialogs import ensure_safe_to_resume
from fight import pass_turn
from proxy_client import ProxyState
from utils import CFG, make_ctx

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

PROXY_ADDR = "127.0.0.1:9999"

SPELL_HOTKEY = CFG.get("sacrid_foot_hotkey")
FOOT_AP_COST = int(CFG.get("sacrid_foot_ap_cost", 4))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
WALK_WAIT_SEC = float(CFG.get("sacrid_walk_wait_sec", 2.0))

FIGHT_START_TIMEOUT = 15.0
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


def pick_walk_landing(me_cell, target_cell, snap):
    """Pick an edge-neighbor of target_cell to step onto.

    Filters out: cells occupied by any alive fight entity (us included),
    cells that turned out non-adjacent (wrap-off-grid). Picks the one
    closest to us by Po distance (cheapest walk)."""
    occupied = {
        e.cell for e in snap.fight_entities.values() if e.alive and e.cell > 0
    }
    cands = []
    for n in neighbors(target_cell):
        if n in occupied:
            continue
        if cell_distance(n, target_cell) != 1:
            continue  # wrapped off-grid
        cands.append((cell_distance(me_cell, n), n))
    if not cands:
        return None
    cands.sort(key=lambda t: t[0])
    return cands[0][1]


def cast_foot_on(target_cell, cal):
    """Press Sacrid Foot hotkey, then left-click the target cell."""
    x, y = cell_to_screen(target_cell, cal)
    print(f"  CAST Sacrid Foot hotkey={SPELL_HOTKEY!r} target_cell={target_cell} -> ({x},{y})")
    pyautogui.press(SPELL_HOTKEY)
    time.sleep(0.15)
    pyautogui.moveTo(x, y, duration=0.1)
    pyautogui.click()


def run_combat_sacrid(ctx, state, cal):
    """In-fight loop. Terminates when proxy reports in_fight=False.

    Per turn:
      1. Find closest alive enemy from proxy fight_entities.
      2. If not adjacent, click a free neighbor of its cell (Dofus
         auto-paths within MP). Re-read my_cell after.
      3. If adjacent and AP >= FOOT_AP_COST, cast Foot once (hotkey +
         target-click).
      4. pass_turn.

    Placement-phase: wait fight_ready_wait_sec for the placement screen
    to settle, then pass_turn to enter combat."""
    cfg = ctx.cfg
    time.sleep(cfg["fight_ready_wait_sec"])
    print("  READY (pass-turn hotkey)")
    pass_turn(ctx)

    while state.snapshot().in_fight:
        snap = state.snapshot()
        me_cell = my_fight_cell(snap)
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        enemies = alive_enemies(snap)
        if not enemies:
            print("  no alive enemies in snapshot; passing")
            pass_turn(ctx)
            time.sleep(cfg["fight_attack_wait_sec"])
            continue

        target = enemies[0]
        dist = cell_distance(me_cell, target.cell) if me_cell else 99
        print(f"  target id={target.id} cell={target.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap}")

        if dist > 1 and me_cell:
            landing = pick_walk_landing(me_cell, target.cell, snap)
            if landing is None:
                print(f"  no free neighbor of cell={target.cell}; passing")
                pass_turn(ctx)
                time.sleep(cfg["fight_attack_wait_sec"])
                continue
            lx, ly = cell_to_screen(landing, cal)
            print(f"  WALK to neighbor cell={landing} -> ({lx},{ly})")
            ctx.click(lx, ly)
            # Wait for my_cell to settle on the landing tile (or close to
            # it -- Dofus may stop short on MP cap). Give up after
            # WALK_WAIT_SEC and try to cast from wherever we landed.
            settled = wait_for(
                state,
                lambda s: my_fight_cell(s) == landing,
                WALK_WAIT_SEC,
            )
            if not settled:
                print(f"  walk didn't reach landing={landing} in {WALK_WAIT_SEC}s; "
                      f"checking adjacency anyway")
            snap = state.snapshot()
            me_cell = my_fight_cell(snap)
            me = snap.fight_entities.get(snap.my_id)
            my_ap = me.ap if me else 0
            target = snap.fight_entities.get(target.id) or target
            dist = cell_distance(me_cell, target.cell) if me_cell else 99

        if dist == 1 and target.alive and my_ap >= FOOT_AP_COST:
            cast_foot_on(target.cell, cal)
            time.sleep(CAST_WAIT_SEC)
        elif dist == 1 and my_ap < FOOT_AP_COST:
            print(f"  adjacent but ap={my_ap} < cost={FOOT_AP_COST}; passing")
        elif dist > 1:
            print(f"  still not adjacent after walk (dist={dist}); passing")

        if not state.snapshot().in_fight:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(ctx)
        time.sleep(cfg["fight_attack_wait_sec"])


def main():
    if not SPELL_HOTKEY:
        print("config.json is missing 'sacrid_foot_hotkey'. Set it to the key "
              "Sacrid Foot is bound to on Marx-Rockfeller's spell bar (e.g. \"1\").")
        sys.exit(1)

    cal = load_cal()
    print(f"[idle-sacrid] cal: origin=({cal['origin_x']:.1f},{cal['origin_y']:.1f}) "
          f"cell={cal['cell_w']:.2f}x{cal['cell_h']:.2f}")
    print(f"[idle-sacrid] spell_hotkey={SPELL_HOTKEY!r} ap_cost={FOOT_AP_COST}")

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[idle-sacrid] connecting to proxy at {PROXY_ADDR}...")
    if not wait_for(state, lambda s: s.connected and s.my_id != 0, 10.0):
        snap = state.snapshot()
        print(f"[idle-sacrid] proxy not ready: connected={snap.connected} my_id={snap.my_id}")
        sys.exit(1)
    snap = state.snapshot()
    print(f"[idle-sacrid] ready: my_id={snap.my_id} my_cell={snap.my_cell} map={snap.map_id}")

    with mss.mss() as sct:
        ctx = make_ctx(sct)
        last_status_ts = 0.0

        while True:
            snap = state.snapshot()

            if snap.in_fight:
                ents = snap.fight_entities
                others = [e for e in ents.values() if e.id != snap.my_id]
                print(f"[idle-sacrid] in fight (map={snap.map_id}), "
                      f"entities={len(ents)} enemies={len(others)}, running sacrid combat")
                run_combat_sacrid(ctx, state, cal)
                time.sleep(1.0)
                pyautogui.press("esc")
                time.sleep(0.3)
                if not ensure_safe_to_resume(ctx):
                    print("[idle-sacrid] menu still open after Esc -- aborting")
                    sys.exit(1)
                continue

            near = nearest_mob(snap)
            if near is None:
                now = time.time()
                if now - last_status_ts > STATUS_LOG_SEC:
                    print(f"[idle-sacrid] map={snap.map_id} my_cell={snap.my_cell} no mobs visible")
                    last_status_ts = now
                time.sleep(IDLE_POLL_SEC)
                continue
            d, cell, mob = near
            x, y = cell_to_screen(cell, cal)
            print(f"[idle-sacrid] engaging nearest mob: cell={cell} dist={d} "
                  f"group={mob.group_id} members={mob.members} -> screen=({x},{y})")
            ctx.click(x, y)
            if wait_for(state, lambda s: s.in_fight, FIGHT_START_TIMEOUT):
                print(f"[idle-sacrid] fight_start received")
                continue
            others = [(c, m) for c, m in state.snapshot().mobs.items() if c != cell]
            if not others:
                print(f"[idle-sacrid] no other mob groups to try; sleeping 3s")
                time.sleep(3.0)
                continue
            rcell, rmob = random.choice(others)
            rx, ry = cell_to_screen(rcell, cal)
            print(f"[idle-sacrid] nearest didn't engage; trying random mob: cell={rcell} "
                  f"group={rmob.group_id} members={rmob.members} -> screen=({rx},{ry})")
            ctx.click(rx, ry)
            if wait_for(state, lambda s: s.in_fight, FIGHT_START_TIMEOUT):
                print(f"[idle-sacrid] fight_start received")
            else:
                print(f"[idle-sacrid] random click also didn't engage; sleeping 3s")
                time.sleep(3.0)


if __name__ == "__main__":
    main()
