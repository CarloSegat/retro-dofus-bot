"""Map-to-map navigation: walk through calibrated switch cells.

Owns the empty-map cooldown (don't walk back into a map we just found
empty; gives respawn time). Picks a fresh neighbour, biases away from
immediate backtracking, walks the switch cell click and waits for the
map_id to change. Also resets the Engager's per-map ghost set whenever
a map change is observed.
"""
import random
import time

from dofus.actions import click_cell
from dofus.map_data import (
    DIRECTION_WORLD_DELTA,
    OPPOSITE_DIRECTION,
    find_path,
    safe_directions,
    target_map_id,
)
from fighter.helpers import wait_for
from utils import CFG

EMPTY_MAP_RESPAWN_SEC = float(CFG.get("empty_map_respawn_sec", 240.0))
MAP_CHANGE_TIMEOUT = 20.0
STATUS_LOG_SEC = 5.0


class MapNavigator:
    """Walks between calibrated maps. Stateless across maps except for
    per-session empty-map cooldown and last-direction bias."""

    def __init__(self, state, cal, map_data, map_by_world, engager):
        self.state = state
        self.cal = cal
        self.map_data = map_data
        self.map_by_world = map_by_world
        self.engager = engager
        # Last direction we walked through a switch cell. Used to bias
        # away from immediate backtracking when other options exist.
        self.last_walk_direction = None
        # {map_id: ts when last found empty}. Entries expire after
        # EMPTY_MAP_RESPAWN_SEC and are cleared on successful engage.
        self.recently_empty_maps: dict[int, float] = {}
        # Count of back-to-back failed switch-cell walks. Resets on any
        # successful map change. Stuck warning surfaces calibration drift
        # before Dofus's ~30min inactivity disconnect.
        self.consecutive_walk_failures = 0
        self.last_status_ts = 0.0
        self.STUCK_WARN_THRESHOLD = 5

    def on_map_changed(self, before_map_id, after_map_id):
        """Called by the orchestrator when map_id flips. Resets per-map
        scratch state and gives the new map a moment to populate."""
        if self.engager.ghosts:
            print(f"[fighter] map changed {before_map_id} -> {after_map_id}; "
                  f"clearing {len(self.engager.ghosts)} ghost(s)")
        self.engager.ghosts.clear()
        self.consecutive_walk_failures = 0
        # GDM clears my_cell=0 and mobs={} before the new map's GM|+
        # burst arrives. Without this wait the next find_target sees
        # mobs={}, falsely marks the map empty, and walks straight out
        # -- bouncing several maps before mob data catches up.
        if not wait_for(self.state, lambda s: s.my_cell != 0, 2.0, poll=0.05):
            print(f"[fighter] my_cell didn't populate within 2s of "
                  f"map change to {after_map_id}; proceeding anyway")

    def mark_current_empty(self, snap):
        self.recently_empty_maps[snap.map_id] = time.time()

    def clear_empty_flag(self, map_id):
        self.recently_empty_maps.pop(map_id, None)

    def travel(self, snap, max_group_size):
        """Pick a direction and walk through its switch cell. Returns
        True if we attempted a walk (caller doesn't need to also
        handle anything else this tick), False if the map has no
        navigation options at all.

        Also handles the periodic status log when we're stuck idle.
        `max_group_size` is for the status log only -- the engager
        already filtered."""
        entry = self.map_data.get(snap.map_id) or {}
        switch_cells_map = entry.get("switch_cells") or {}
        self.mark_current_empty(snap)
        safe = safe_directions(entry, self.map_by_world) if switch_cells_map else []
        now = time.time()
        self.recently_empty_maps = {
            mid: ts for mid, ts in self.recently_empty_maps.items()
            if now - ts < EMPTY_MAP_RESPAWN_SEC
        }
        fresh = [
            d for d in safe
            if (tmid := target_map_id(entry, d, self.map_by_world)) is None
            or tmid not in self.recently_empty_maps
        ]
        if not fresh:
            self._maybe_log_no_progress(snap, entry, switch_cells_map, safe,
                                        max_group_size, now)
            time.sleep(0.5)
            return False
        excluded = OPPOSITE_DIRECTION.get(self.last_walk_direction)
        preferred = [d for d in fresh if d != excluded]
        direction = random.choice(preferred or fresh)
        switch_cell = switch_cells_map[direction]
        total = len(snap.mobs)
        reason = (f"{total} group(s) all filtered by max-group-size={max_group_size}"
                  if max_group_size > 0 and total > 0
                  else "no mobs visible")
        skipped = [d for d in safe if d not in fresh]
        skip_note = (f", skipping {skipped} (target map recently empty)"
                     if skipped else "")
        cur_world = entry.get("world")
        cur_world_str = (f"({cur_world[0]},{cur_world[1]})" if cur_world else "?")
        delta = DIRECTION_WORLD_DELTA.get(direction)
        tgt_world_str = (f"({cur_world[0]+delta[0]},{cur_world[1]+delta[1]})"
                         if cur_world and delta else "?")
        tgt_mid = target_map_id(entry, direction, self.map_by_world)
        tgt_mid_str = str(tgt_mid) if tgt_mid is not None else "?"
        print(f"[fighter] phase=idle map={snap.map_id} world={cur_world_str} "
              f"my_cell={snap.my_cell}: {reason}; "
              f"walking {direction} (fresh={fresh}{skip_note}, "
              f"avoid={excluded}) to switch cell={switch_cell}; "
              f"target map={tgt_mid_str} world={tgt_world_str}")
        click_cell(switch_cell, self.cal)
        self.last_walk_direction = direction
        before_map = snap.map_id
        if wait_for(self.state,
                    lambda s, bm=before_map: (s.map_id != bm and s.map_id != 0)
                                              or s.in_fight,
                    MAP_CHANGE_TIMEOUT):
            ns = self.state.snapshot()
            if ns.in_fight:
                print(f"[fighter] aggroed while walking {direction}; "
                      f"phase={ns.fight_phase}")
            else:
                print(f"[fighter] map changed {before_map} -> {ns.map_id} via {direction}")
            self.consecutive_walk_failures = 0
        else:
            self.consecutive_walk_failures += 1
            print(f"[fighter] walk to {direction} switch cell did not change map "
                  f"in {MAP_CHANGE_TIMEOUT}s; will retry next tick "
                  f"(consecutive_failures={self.consecutive_walk_failures})")
            if self.consecutive_walk_failures >= self.STUCK_WARN_THRESHOLD:
                elapsed = self.consecutive_walk_failures * MAP_CHANGE_TIMEOUT
                print(f"[fighter] *** STUCK *** {self.consecutive_walk_failures} "
                      f"consecutive failed walks on map={snap.map_id} "
                      f"world={cur_world_str} (~{elapsed:.0f}s of no real "
                      f"movement). Dofus inactivity disconnect is ~30min. "
                      f"Check: switch_cell calibration, obstacles blocking "
                      f"path, or game-window focus.")
        return True

    def walk_to_world(self, target_world, on_aggro=None):
        """Walk from the current map to the calibrated map at
        `target_world` (a (world_x, world_y) tuple).

        Returns True iff the bot is standing on the target map at the
        end. Returns False if:
          - the current map isn't calibrated (can't anchor pathfinding)
          - no path exists in the calibrated graph (fails *upfront* --
            no clicks issued)
          - a walk step doesn't change the map within MAP_CHANGE_TIMEOUT
          - we aggro AND `on_aggro` is None
          - after running `on_aggro`, we land on an un-calibrated map

        Aggro handling: if `on_aggro` is provided, it's called when a
        switch-cell click triggers a fight (expected to block until
        the fight resolves). After it returns, we re-pathfind from
        wherever we ended up -- the fight or its post-fight cleanup
        may have left us on the same map or a neighbour. `on_aggro`
        is typically `Combat.run`.
        """
        target = (int(target_world[0]), int(target_world[1]))
        while True:
            snap = self.state.snapshot()
            entry = self.map_data.get(snap.map_id)
            if entry is None:
                print(f"[walk_to] current map_id={snap.map_id} is not "
                      f"calibrated; can't anchor pathfinding")
                return False
            world = entry.get("world")
            if not (isinstance(world, (list, tuple)) and len(world) == 2):
                print(f"[walk_to] current map {snap.map_id} has no world "
                      f"field in map_data; aborting")
                return False
            cur = (int(world[0]), int(world[1]))
            if cur == target:
                print(f"[walk_to] arrived at world {target} "
                      f"(map_id={snap.map_id})")
                return True
            path = find_path(cur, target, self.map_by_world)
            if path is None:
                print(f"[walk_to] no calibrated path from world {cur} to "
                      f"{target}; aborting (run nav_graph.py to inspect "
                      f"missing edges)")
                return False
            print(f"[walk_to] world {cur} -> {target}: {len(path)} step(s) "
                  f"via {path}")
            direction = path[0]
            stepped, fought = self._walk_one_step(entry, direction, on_aggro)
            if fought:
                # Re-pathfind from the new map (fight may have ended on
                # a neighbour, or the post-fight cleanup might have left
                # the character somewhere unexpected).
                continue
            if not stepped:
                print(f"[walk_to] step {direction} from world {cur} failed; "
                      f"aborting")
                return False
            # Stepped successfully -- loop and re-pathfind. Re-pathfinding
            # after every step is cheap (BFS over <100 maps) and self-heals
            # if the switch click delivered us to an unexpected map.

    def _walk_one_step(self, entry, direction, on_aggro):
        """Click the switch cell for `direction` on `entry`'s map and
        wait for either a map change or an aggro.

        Returns (stepped, fought) tuple:
          stepped=True if map_id changed without aggro
          fought=True  if we aggroed and `on_aggro` ran (caller should
                       re-pathfind, ignore `stepped`)

        Both False means the click didn't change the map within
        MAP_CHANGE_TIMEOUT (caller should abort).
        """
        switches = entry.get("switch_cells") or {}
        switch_cell = switches.get(direction)
        if switch_cell is None:
            print(f"[walk_to] no {direction} switch on current map "
                  f"({entry.get('map_id')}); aborting")
            return (False, False)
        before_map = entry["map_id"]
        print(f"[walk_to] walking {direction} from map={before_map} via "
              f"switch cell={switch_cell}")
        click_cell(switch_cell, self.cal)
        if not wait_for(
            self.state,
            lambda s, bm=before_map: (s.map_id != bm and s.map_id != 0)
                                     or s.in_fight,
            MAP_CHANGE_TIMEOUT,
        ):
            print(f"[walk_to] walk {direction} did not change map in "
                  f"{MAP_CHANGE_TIMEOUT}s")
            return (False, False)
        post = self.state.snapshot()
        if post.in_fight:
            if on_aggro is None:
                print(f"[walk_to] aggroed while walking {direction}; no "
                      f"fight handler provided, aborting")
                return (False, False)
            print(f"[walk_to] aggroed while walking {direction}; running "
                  f"fight handler, will re-pathfind after")
            on_aggro()
            return (False, True)
        # Map changed cleanly. Wait for my_cell to repopulate so the next
        # iteration sees a real snapshot, mirroring on_map_changed.
        wait_for(self.state, lambda s: s.my_cell != 0, 2.0, poll=0.05)
        new = self.state.snapshot()
        print(f"[walk_to] arrived on map_id={new.map_id}")
        return (True, False)

    def _maybe_log_no_progress(self, snap, entry, switch_cells_map, safe,
                               max_group_size, now):
        """Periodic status log when we're stuck on an empty-but-cooldowned
        map. Keeps the operator informed without spamming."""
        if now - self.last_status_ts <= STATUS_LOG_SEC:
            return
        total = len(snap.mobs)
        cap_note = (f" (filtered by max-group-size={max_group_size};"
                    f" total_visible={total})"
                    if max_group_size > 0 and total > 0 else "")
        if not switch_cells_map:
            nav_note = "no switch_cells calibrated for this map"
        elif not safe:
            nav_note = (f"switch_cells={list(switch_cells_map.keys())} but no "
                        f"return-safe neighbour (target maps un-calibrated or "
                        f"missing return switch)")
        else:
            nav_note = (f"safe={safe} but every target is in cooldown "
                        f"(empty within {int(EMPTY_MAP_RESPAWN_SEC)}s); "
                        f"waiting for respawn")
        cur_world = entry.get("world")
        cur_world_str = (f"({cur_world[0]},{cur_world[1]})" if cur_world else "?")
        print(f"[fighter] phase=idle map={snap.map_id} world={cur_world_str} "
              f"my_cell={snap.my_cell} no mobs visible "
              f"(ghosts={len(self.engager.ghosts)}){cap_note}; {nav_note}")
        self.last_status_ts = now
