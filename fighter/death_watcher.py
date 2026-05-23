"""DeathWatcher: detect bot death, sit to regen, walk back to area.

Death signal in Dofus Retro
---------------------------
The reliable signal is the post-fight auto-teleport to the phoenix
respawn map. The server's death sequence is:

  1. Final combat actions.
  2. GE<xp>;... -- XP-summary packet, which the proxy interprets as
     `fight_end` (and synchronously clears `fight_entities`).
  3. GDM|<map_id> -- teleport to phoenix.

No GTM between (1) and (2) ever flags our own row as dead in the
`<id>;1` short form (verified May 2026: 16 in-fight observations of
our own row, 0 with `alive=False`, after a confirmed kill -- see
memory `project_retro_death_no_alive_packet`). Detection therefore
watches for the map_id jump in (3) within
POST_FIGHT_DEATH_WINDOW_SEC of `fight_end`.

The `dead_observations` counter is kept purely as observability so
a future proxy change that exposes a death event surfaces in the
log immediately.

Recovery on death
-----------------
  1. Sit (`/sit`) for POST_DEATH_SIT_SEC seconds. Sitting halves
     regen ms per HP -- abortable on aggro (rare on a phoenix map
     but possible).
  2. If a farming area was selected, BFS for the nearest in-area
     map from the respawn map and walk there via
     `navigator.walk_to_world`.

Threading
---------
  on_proxy_event       : proxy thread
  on_fight_engaged     : main thread (Combat.run callback)
  on_fight_ended       : main thread (Combat.run callback)
  try_recover          : main thread (Orchestrator._tick_idle)

No explicit lock: every cross-thread write is a single-word
assignment (CPython atomic), the proxy thread only ever flips
`needs_recovery=True` and ends the watch; partial reads of the
two-field watch state would self-heal on the next event tick.
"""
import time
from enum import Enum

from dofus.actions import sit
from dofus.map_data import find_path


POST_DEATH_SIT_SEC = 5.0
SIT_POLL_SEC = 1.0
# How long after fight_end we keep watching for a teleport-away. The
# auto-teleport to phoenix lands within ~2s in practice; 6s leaves
# headroom for slow GDM packets without false-positiving a voluntary
# walk (which the bot never issues post-fight anyway).
POST_FIGHT_DEATH_WINDOW_SEC = 6.0


class _WatchState(Enum):
    """Internal phase of the DeathWatcher state machine.

    Transitions:
      IDLE             --on_fight_engaged-->  IN_FIGHT
      IN_FIGHT         --on_fight_ended-->    POST_FIGHT_WATCH
      POST_FIGHT_WATCH --teleport detected--> IDLE  (needs_recovery=True)
      POST_FIGHT_WATCH --window expired-->    IDLE
      POST_FIGHT_WATCH --on_fight_engaged-->  IN_FIGHT  (next fight)
    """
    IDLE = "idle"
    IN_FIGHT = "in_fight"
    POST_FIGHT_WATCH = "post_fight_watch"


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

        # State machine.
        self._phase = _WatchState.IDLE
        self._engage_map_id = None
        self._watch_started_at = None

        # Observability counters, reset per fight. `dead_observations`
        # is structurally always 0 in Retro -- kept so a future proxy
        # change that surfaces a death packet shows up immediately in
        # the [death] on_fight_ended log line.
        self.my_row_observations = 0
        self.dead_observations = 0

        # Outputs read by Orchestrator.
        self.needs_recovery = False
        self.death_map_id = None

    # --- Hooks ---

    def on_fight_engaged(self, snap):
        """Combat.on_fight_engaged hook. Enter IN_FIGHT, capturing the
        engage map (the death-detection reference point)."""
        print(f"[death] on_fight_engaged: my_id={snap.my_id} "
              f"map_id={snap.map_id} fight_entities="
              f"{len(snap.fight_entities or {})}")
        self._phase = _WatchState.IN_FIGHT
        self._engage_map_id = snap.map_id
        self._watch_started_at = None
        self.my_row_observations = 0
        self.dead_observations = 0

    def on_fight_ended(self, snap):
        """Combat.on_fight_ended hook. Arm the post-fight teleport
        watch -- the actual death decision is made by on_proxy_event
        when the next GDM lands (or doesn't, within the window)."""
        me_present = snap.my_id in (snap.fight_entities or {})
        print(f"[death] on_fight_ended: my_row_observations="
              f"{self.my_row_observations} dead_observations="
              f"{self.dead_observations} map_id={snap.map_id} "
              f"my_id={snap.my_id} my_row_in_fe={me_present} "
              f"engage_map_id={self._engage_map_id}")
        self._phase = _WatchState.POST_FIGHT_WATCH
        self._watch_started_at = time.time()
        print(f"[death] arming post-fight death watch "
              f"(window={POST_FIGHT_DEATH_WINDOW_SEC}s, "
              f"engage_map_id={self._engage_map_id})")

    def on_proxy_event(self, ev):
        """Dispatches on `_phase`.
          IN_FIGHT          -> increment observability counters
          POST_FIGHT_WATCH  -> watch for the teleport (or timeout)
          IDLE              -> ignored
        """
        if self._phase is _WatchState.IN_FIGHT:
            self._observe_in_fight()
        elif self._phase is _WatchState.POST_FIGHT_WATCH:
            self._observe_post_fight()

    def try_recover(self):
        """Orchestrator calls this at the top of `_tick_idle`. Returns
        True if recovery ran (caller treats the tick as consumed)."""
        if not self.needs_recovery:
            return False
        self.needs_recovery = False  # consume before running so re-entry is safe
        self._do_recovery()
        return True

    # --- Per-state observers ---

    def _observe_in_fight(self):
        """Counters only. The `alive=False` signal never fires for our
        own row in Retro; tracked purely so a regression / proxy
        change surfaces immediately (dead_observations > 0 would
        be visible diagnostic evidence that detection has another
        usable signal)."""
        snap = self.state.snapshot()
        if not snap.my_id:
            return
        me = snap.fight_entities.get(snap.my_id)
        if me is None:
            return
        self.my_row_observations += 1
        if not me.alive:
            self.dead_observations += 1

    def _observe_post_fight(self):
        """While in the post-fight watch window:
          - map_id != engage_map_id  -> phoenix teleport -> DEATH
          - window elapses           -> no teleport -> ALIVE
        Either outcome ends the watch."""
        elapsed = time.time() - self._watch_started_at
        if elapsed > POST_FIGHT_DEATH_WINDOW_SEC:
            snap = self.state.snapshot()
            print(f"[death] post-fight death watch expired after "
                  f"{elapsed:.1f}s with no teleport "
                  f"(stayed on map_id={snap.map_id}); diagnosing as ALIVE")
            self._end_watch()
            return
        snap = self.state.snapshot()
        if snap.in_fight or snap.map_id == 0:
            return  # new fight starting (on_fight_engaged will reset) or transition snapshot
        if snap.map_id != self._engage_map_id:
            print(f"[death] post-fight teleport detected: "
                  f"engage_map={self._engage_map_id} -> "
                  f"current_map={snap.map_id} within {elapsed:.2f}s of "
                  f"fight_end -- diagnosing as DEATH")
            self.needs_recovery = True
            self.death_map_id = self._engage_map_id
            self._end_watch()

    def _end_watch(self):
        self._phase = _WatchState.IDLE
        self._watch_started_at = None

    # --- Recovery ---

    def _do_recovery(self):
        snap = self.state.snapshot()
        respawn_map = snap.map_id
        print(f"[death] respawned on map_id={respawn_map} "
              f"(died on map_id={self.death_map_id})")

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
