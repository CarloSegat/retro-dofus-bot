"""Equipped weapon used during combat -- class-agnostic.

Models a ranged, click-targeted weapon (currently a bow). Any character
class can wield one: the wielder is responsible for

  - calling `on_fight_engaged()` at fight start (resets cooldown / disabled);
  - calling `fire_burst(my_ap, me_cell, current_turn, static_obstacles)`
    at the appropriate moment in their turn;
  - calling `notify_disabled(current_turn, duration_turns, reason)` when a
    class-specific effect makes the weapon unusable. The Weapon itself
    knows nothing about why -- e.g. the Sacrieur calls this after Vital
    Punishment because Vital applies the Weakened state which blocks
    weapons, but a different class might call it for a different reason.

Targeting honours the same `alive_enemies()` rules as the rest of the
fighter (summons de-prioritised when real mobs are alive).
"""
import time

from dofus.actions import cast_at_cell
from dofus.cell_grid import cell_distance, line_of_sight
from fighter.helpers import alive_enemies


class Weapon:
    def __init__(self, state, cal, *,
                 hotkey, ap_cost, min_range, max_range,
                 post_walk_extra_settle_sec, cast_wait_sec):
        self.state = state
        self.cal = cal
        self.hotkey = hotkey
        self.ap_cost = ap_cost
        self.min_range = min_range
        self.max_range = max_range
        self.post_walk_extra_settle_sec = post_walk_extra_settle_sec
        self.cast_wait_sec = cast_wait_sec
        # Disabled-until turn. While `current_turn < _disabled_until_turn`
        # the weapon refuses to fire.
        self._disabled_until_turn = -1

    def on_fight_engaged(self):
        """Reset fight-scoped state. Call from the wielder's
        on_fight_engaged."""
        self._disabled_until_turn = -1

    def notify_disabled(self, current_turn, duration_turns, reason=""):
        """Mark the weapon disabled for `duration_turns` starting now.
        Idempotent: only extends the disabled window, never shortens.
        `reason` is logged for debugging (e.g. "Weakened from Vital
        Punishment")."""
        until = current_turn + duration_turns
        if until > self._disabled_until_turn:
            self._disabled_until_turn = until
            print(f"  [weapon] disabled until turn {until} "
                  f"(from turn {current_turn}, dur={duration_turns}, "
                  f"reason={reason!r})")

    def is_disabled(self, current_turn):
        return current_turn < self._disabled_until_turn

    def pick_target(self, snap, me_cell, current_turn, static_obstacles,
                    debug=False):
        """Nearest alive enemy in [min_range, max_range] with LoS, or
        None. LoS blockers = static_obstacles + all other live entities
        (excluding the target's own cell). Returns None when disabled,
        me_cell unknown, or no eligible target."""
        if self.is_disabled(current_turn):
            if debug:
                turns_left = self._disabled_until_turn - current_turn
                print(f"  weapon: no target -- disabled "
                      f"({turns_left} more turn(s))")
            return None
        if not me_cell:
            if debug:
                print("  weapon: no target -- me_cell unknown")
            return None
        other_alive = {
            e.cell for e in snap.fight_entities.values()
            if e.alive and e.cell > 0 and e.id != snap.my_id
        }
        candidates = []
        rejections = []
        for e in alive_enemies(snap):
            d = cell_distance(me_cell, e.cell)
            if d < self.min_range or d > self.max_range:
                if debug:
                    rejections.append(
                        f"id={e.id} cell={e.cell} out-of-range(dist={d}, "
                        f"need {self.min_range}..{self.max_range})"
                    )
                continue
            blockers = set(static_obstacles) | (other_alive - {e.cell})
            if not line_of_sight(me_cell, e.cell, blockers):
                if debug:
                    mob_blockers = sorted(other_alive - {e.cell})
                    rejections.append(
                        f"id={e.id} cell={e.cell} dist={d} LoS-blocked "
                        f"(other_alive={mob_blockers}, "
                        f"static_obstacles={len(static_obstacles)})"
                    )
                continue
            candidates.append((d, e))
        if not candidates:
            if debug:
                if rejections:
                    print(f"  weapon: no target -- {len(rejections)} "
                          f"enemies considered:")
                    for r in rejections:
                        print(f"    rejected {r}")
                else:
                    print("  weapon: no target -- no alive enemies on the field")
            return None
        candidates.sort(key=lambda t: t[0])
        return candidates[0][1]

    def fire_burst(self, my_ap, me_cell, current_turn, static_obstacles):
        """Fire shots until AP < cost, no eligible target, or combat
        ends. Returns (updated_ap, shots_fired)."""
        shots = 0
        while my_ap >= self.ap_cost:
            snap = self.state.snapshot()
            if not snap.in_combat:
                return my_ap, shots
            target = self.pick_target(snap, me_cell, current_turn,
                                      static_obstacles, debug=(shots == 0))
            if target is None:
                return my_ap, shots
            d = cell_distance(me_cell, target.cell)
            print(f"  weapon: targeting id={target.id} cell={target.cell} "
                  f"dist={d} ap_before={my_ap}")
            self._cast_at(target.cell)
            time.sleep(self.cast_wait_sec)
            my_ap -= self.ap_cost
            shots += 1
        return my_ap, shots

    def _cast_at(self, target_cell):
        print(f"  CAST weapon hotkey={self.hotkey!r} target_cell={target_cell}")
        cast_at_cell(self.hotkey, target_cell, self.cal)
