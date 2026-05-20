"""DeathWatcher: detect bot death, sit to regen, walk back to area.

Death signal: during a fight, every GTM packet refreshes our row in
`fight_entities`. When we die the row collapses to status=1 (alive=False).
The proxy clears `fight_entities` *before* emitting the `fight_end`
event, so we can't read it then -- instead we listen to the raw event
stream (state.on_event) and remember `last_my_alive_in_fight` after
every snapshot while in combat. When Combat fires on_fight_ended we
check that remembered value.

Recovery on death:
  1. Sit (`/sit`) for POST_DEATH_SIT_SEC seconds. Sitting halves regen
     ms per HP (Dofus retro behaviour) -- aborts early if we get
     aggro'd (rare on a phoenix map but possible).
  2. If a farming area was selected, BFS for the nearest in-area map
     from the respawn map and walk there via navigator.walk_to_world.
     Already on an in-area map -> skip.
     No area / no path -> log and stay put.

The class owns its own state; Orchestrator only calls observe_snapshot
on every proxy event, on_fight_ended on Combat's hook, and try_recover
at the top of _tick_idle.
"""
import time

from dofus.actions import sit
from dofus.map_data import find_path


POST_DEATH_SIT_SEC = 120.0
SIT_POLL_SEC = 1.0


class DeathWatcher:
    """See module docstring."""

    def __init__(self, state, navigator, on_aggro, farming_area_map_ids=None):
        self.state = state
        self.navigator = navigator
        # Combat.run, used if walk-back aggros mid-step. Navigator's
        # walk_to_world re-pathfinds after on_aggro returns.
        self.on_aggro = on_aggro
        self.farming_area_map_ids = (
            None if farming_area_map_ids is None
            else {int(m) for m in farming_area_map_ids}
        )
        # Tracking (written from proxy thread, read from main thread --
        # both are single-word writes, no lock needed in CPython).
        self.last_my_alive_in_fight = True
        # State machine (main thread only).
        self.needs_recovery = False
        self.death_map_id = None

    # --- Hooks ---

    def on_proxy_event(self, ev):
        """Registered on ProxyState.on_event. Refreshes our alive flag
        from the current snapshot. Ignored unless we're in combat (out
        of fight, fight_entities is empty / stale)."""
        snap = self.state.snapshot()
        if not snap.in_combat or not snap.my_id:
            return
        me = snap.fight_entities.get(snap.my_id)
        if me is not None:
            self.last_my_alive_in_fight = me.alive

    def on_fight_ended(self, snap):
        """Registered on Combat.on_fight_ended. Flips needs_recovery if
        our last known in-fight alive flag was False."""
        if not self.last_my_alive_in_fight:
            self.needs_recovery = True
            self.death_map_id = snap.map_id
            print(f"[death] DIED in fight (last fight map_id={snap.map_id}); "
                  f"recovery queued (sit {int(POST_DEATH_SIT_SEC)}s, then walk "
                  f"back to farming area)")
        # Reset for the next fight regardless.
        self.last_my_alive_in_fight = True

    def try_recover(self):
        """Called by Orchestrator at the top of _tick_idle. Returns True
        if recovery ran (caller should treat the tick as consumed),
        False if there's nothing to do."""
        if not self.needs_recovery:
            return False
        self.needs_recovery = False  # consume before running so re-entry is safe
        self._do_recovery()
        return True

    # --- Recovery ---

    def _do_recovery(self):
        snap = self.state.snapshot()
        respawn_map = snap.map_id
        print(f"[death] respawned on map_id={respawn_map} "
              f"(died on map_id={self.death_map_id})")

        # Sit + wait, abortable on aggro.
        print(f"[death] sitting for {int(POST_DEATH_SIT_SEC)}s to regen HP")
        sit()
        deadline = time.time() + POST_DEATH_SIT_SEC
        while time.time() < deadline:
            cur = self.state.snapshot()
            if cur.in_fight:
                print(f"[death] aggro'd while sitting "
                      f"({int(deadline - time.time())}s left); aborting recovery")
                return
            time.sleep(SIT_POLL_SEC)
        print(f"[death] sit done")

        self._walk_back_to_area()

    def _walk_back_to_area(self):
        if self.farming_area_map_ids is None:
            print(f"[death] no farming area selected; staying on respawn map")
            return
        snap = self.state.snapshot()
        if snap.map_id in self.farming_area_map_ids:
            print(f"[death] respawn map_id={snap.map_id} is inside farming "
                  f"area; no walk-back needed")
            return

        entry = self.navigator.map_data.get(snap.map_id)
        if entry is None:
            print(f"[death] respawn map_id={snap.map_id} not calibrated; "
                  f"can't path back to farming area")
            return
        world = entry.get("world")
        if not (isinstance(world, (list, tuple)) and len(world) == 2):
            print(f"[death] respawn map {snap.map_id} has no world coord; "
                  f"can't path back")
            return
        cur_world = (int(world[0]), int(world[1]))

        # Find the nearest in-area map by BFS path length.
        best_target = None
        best_path_len = None
        for area_mid in self.farming_area_map_ids:
            area_entry = self.navigator.map_data.get(area_mid)
            if not area_entry:
                continue
            aw = area_entry.get("world")
            if not (isinstance(aw, (list, tuple)) and len(aw) == 2):
                continue
            target_world = (int(aw[0]), int(aw[1]))
            path = find_path(cur_world, target_world, self.navigator.map_by_world)
            if path is None:
                continue
            if best_path_len is None or len(path) < best_path_len:
                best_path_len = len(path)
                best_target = target_world

        if best_target is None:
            print(f"[death] no calibrated path from respawn world {cur_world} "
                  f"to any farming-area map; staying put (run nav_graph.py to "
                  f"inspect missing edges)")
            return

        print(f"[death] walking back to farming area: {cur_world} -> "
              f"{best_target} ({best_path_len} step(s))")
        ok = self.navigator.walk_to_world(best_target, on_aggro=self.on_aggro)
        if ok:
            print(f"[death] arrived at farming-area entry {best_target}; "
                  f"resuming normal play")
        else:
            print(f"[death] walk-back to {best_target} failed; bot will "
                  f"idle here until next tick decides what to do")
