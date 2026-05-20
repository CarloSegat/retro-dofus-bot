"""Idle-state: find a valid mob group and click to engage."""
import time

from dofus.actions import click_cell
from dofus.cell_grid import cell_distance
from fighter.helpers import wait_for

ENGAGE_TIMEOUT = 5.0
# Multiplier applied to ENGAGE_TIMEOUT while the character is
# overweight. Walks slow down past the soft pod cap, so a fresh 5s
# wait expires before the sprite finishes the path and we wrongly
# mark the cell a ghost. 3x covers the typical "few hundred pods over
# soft cap" case observed on Marx-Rockfeller; tune up if blocked-cap
# walks still time out.
ENGAGE_TIMEOUT_OVERWEIGHT_MULTIPLIER = 3.0
COMBAT_START_TIMEOUT = 35.0


class Engager:
    """Pre-combat targeting: scan visible mobs, filter ghosts and
    mid-walk groups, return the nearest. Engage by clicking the cell.
    Tracks (cell, group_id) tuples that failed to engage as 'ghosts'
    -- clearing happens on map change, handled by MapNavigator."""

    def __init__(self, state, cal, hp_regen, map_data, inventory,
                 max_group_size=0):
        self.state = state
        self.cal = cal
        self.hp_regen = hp_regen
        self.map_data = map_data
        self.inventory = inventory
        self.max_group_size = max_group_size
        # (cell, group_id) tuples we clicked but failed to engage on
        # the current map. Cleared by MapNavigator on map change.
        self.ghosts = set()
        # Last unix-ts we logged "engage waiting longer due to
        # overweight". Throttled to one line per 30s so the console
        # doesn't drown while we sit a few hundred pods over the cap.
        self._overweight_log_ts = 0.0
        # Same throttle for the rarer "hard cap, refusing to engage"
        # case. Separate ts so an overweight log doesn't suppress a
        # blocked log or vice versa.
        self._blocked_log_ts = 0.0

    def find_target(self, snap):
        """(distance, cell, mob) for the closest valid mob, or None.

        Skips:
          - ghosts (cell, group_id we already clicked and didn't engage)
          - groups larger than max_group_size when > 0
          - mobs whose move_ends_at_ms is in the future (proxy re-keys
            s.mobs to the destination cell the moment GA0;1; arrives,
            but Dofus animates ~steps*400ms; clicking the destination
            during animation registers as a walk, not an engage)

        When my_cell is 0 (proxy just attached) returns the first valid
        candidate with distance=-1 so the caller still tries to engage."""
        now_ms = int(time.time() * 1000)
        candidates = [
            (c, m) for c, m in snap.mobs.items()
            if (c, m.group_id) not in self.ghosts
            and (self.max_group_size <= 0 or len(m.members) <= self.max_group_size)
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

    def all_walking_groups_filtered(self, snap):
        """True iff every visible non-ghost group is mid-walk (and would
        be returned by find_target once their animation ends). Used by
        the orchestrator to decide whether to mark the map empty or
        just wait a tick."""
        now_ms = int(time.time() * 1000)
        walking = [
            (c, m) for c, m in snap.mobs.items()
            if (c, m.group_id) not in self.ghosts
            and (self.max_group_size <= 0 or len(m.members) <= self.max_group_size)
            and m.move_ends_at_ms > now_ms
        ]
        if not walking:
            return None
        wait_ms = max(m.move_ends_at_ms - now_ms for _, m in walking)
        return wait_ms

    def try_engage(self, target):
        """Click the mob, wait for in_fight. Returns True on engage.

        Re-snapshots before clicking (mob group may have wandered).
        Waits for HP threshold via hp_regen. Tries next-nearest if the
        first click ghosts.

        Pod weight affects how long we wait for the engage to register:
          - is_blocked (hard cap): refuse outright, the character
            literally cannot walk; log throttled to once per 30s.
          - is_overweight (soft cap): proceed but wait
            ENGAGE_TIMEOUT * ENGAGE_TIMEOUT_OVERWEIGHT_MULTIPLIER
            instead of ENGAGE_TIMEOUT. The walk is slow, not dead;
            without the extension the click is wrongly ghosted while
            the sprite is still moving."""
        if self.inventory.is_blocked:
            now = time.time()
            if now - self._blocked_log_ts > 30.0:
                print(f"[fighter] skipping engage: {self.inventory.summary()}; "
                      f"hard cap reached, character cannot move")
                self._blocked_log_ts = now
            time.sleep(2.0)
            return False
        timeout = self._engage_timeout_for_logging()
        d, cell, mob = target
        snap = self.state.snapshot()
        # Re-check: the group may have moved/despawned since target pick.
        fresh = snap.mobs.get(cell)
        if fresh is None or fresh.group_id != mob.group_id:
            print(f"[fighter] mob group={mob.group_id} moved/despawned from "
                  f"cell={cell} before click; re-picking next tick")
            return False
        if not self.hp_regen.wait_for_threshold():
            time.sleep(1.0)
            return False
        if self._click_and_wait(cell, mob, d, timeout):
            return True
        # First click ghosted -- try next-nearest from a fresh snapshot.
        self.ghosts.add((cell, mob.group_id))
        print(f"[fighter] click on cell={cell} group={mob.group_id} did not "
              f"engage; marking ghost (total={len(self.ghosts)})")
        alt = self.find_target(self.state.snapshot())
        if alt is None:
            print(f"[fighter] no non-ghost mob groups to try; sleeping 3s")
            time.sleep(3.0)
            return False
        d2, acell, amob = alt
        print(f"[fighter] nearest didn't engage; trying next-nearest mob: "
              f"cell={acell} dist={d2} group={amob.group_id} "
              f"members={amob.members}")
        if self._click_and_wait(acell, amob, d2, timeout):
            return True
        self.ghosts.add((acell, amob.group_id))
        print(f"[fighter] next-nearest also didn't engage; marking ghost "
              f"(total={len(self.ghosts)}); sleeping 3s")
        time.sleep(3.0)
        return False

    def _engage_timeout_for_logging(self):
        """Pick the engage-wait timeout for this try_engage call and log
        if we're using the extended (overweight) one. Throttled to one
        log line per 30s. Returns the timeout in seconds."""
        if not self.inventory.is_overweight:
            return ENGAGE_TIMEOUT
        timeout = ENGAGE_TIMEOUT * ENGAGE_TIMEOUT_OVERWEIGHT_MULTIPLIER
        now = time.time()
        if now - self._overweight_log_ts > 30.0:
            print(f"[fighter] {self.inventory.summary()}: extending engage "
                  f"timeout {ENGAGE_TIMEOUT:.1f}s -> {timeout:.1f}s "
                  f"(walks are slow at overweight)")
            self._overweight_log_ts = now
        return timeout

    def _click_and_wait(self, cell, mob, dist, timeout):
        hp_snap = self.state.snapshot()
        print(f"[fighter] engaging mob: cell={cell} dist={dist} "
              f"group={mob.group_id} members={mob.members} "
              f"hp~{hp_snap.estimated_life()}/{hp_snap.my_life_max} "
              f"engage_timeout={timeout:.1f}s")
        click_cell(cell, self.cal)
        if wait_for(self.state, lambda s: s.in_fight, timeout):
            print(f"[fighter] fight_engage received "
                  f"(phase={self.state.snapshot().fight_phase})")
            return True
        return False

    def place_starting_cells(self, snap):
        """Click the saved starting cells for the current map (one
        click per cell in saved order). No-op if no calibration."""
        entry = self.map_data.get(snap.map_id)
        if not entry:
            return
        cells = entry.get("cells") or []
        if not cells:
            return
        print(f"[fighter] placement: clicking {len(cells)} starting cell(s) "
              f"for map={snap.map_id} world={entry.get('world')}")
        for cell in cells:
            click_cell(cell, self.cal)
            time.sleep(0.3)
