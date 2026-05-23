"""Enutrof: per-turn brain plus class-specific spell knowledge.

One spell -- Coins Throwing (single-target, 2 AP, range 1..13). The
turn shape:

  if nearest enemy not adjacent and we have MP: walk_toward(nearest)
  while ap >= 2:
      pick castable enemy with fewest casts this turn (ties = nearest)
      cast
      sleep cast_wait_sec  (gives GTM a chance to update alive/hp)

Walk-first runs even when an enemy is already in range: the
calibration conflates LoS-blocking peaks with passable holes (see
project_obstacles_holes_vs_peaks memory), and closing distance cuts
down on casts the server silently rejects through real peaks. The
cast loop still has its own fallback walk in case the first close-in
didn't reach a castable position. AP is tracked locally with a
floor of the snapshot's AP -- so if GTM fires mid-turn and reveals
we've spent more than we thought (rare), we adopt the lower number
and stop early instead of spamming casts the server will reject.

The least-cast-first picker spreads damage across the group; combined
with the per-cast settle that lets GTM updates land between casts,
this means dead enemies drop out of `alive_enemies` before we
double-tap them. If GTM never fires mid-turn we may waste at most
one cast per dead target (the cycle hits every enemy once before
returning to the first), which is the right floor for a simple brain.

Walking helpers (walk_toward, etc.) live in fighter.walking -- shared
with Sacrieur.

Wired via Combat.on_turn_start(enutrof.play_turn) and
Combat.on_fight_engaged(enutrof.on_fight_engaged) in Orchestrator.
"""
import time

from dofus.actions import cast_at_cell, pass_turn
from dofus.cell_grid import cell_distance, line_of_sight
from dofus.map_data import save as save_map_data
from fighter.helpers import alive_enemies, my_fight_cell
from fighter.walking import walk_toward
from utils import CFG


# === Config-driven knobs ===
COINS_HOTKEY = CFG.get("enutrof_coins_hotkey")
COINS_AP_COST = int(CFG.get("enutrof_coins_ap_cost", 2))
COINS_MIN_RANGE = int(CFG.get("enutrof_coins_min_range", 1))
COINS_MAX_RANGE = int(CFG.get("enutrof_coins_max_range", 13))
COINS_POST_WALK_EXTRA_SETTLE_SEC = float(
    CFG.get("enutrof_coins_post_walk_settle_sec", 0.33))
CAST_WAIT_SEC = float(CFG.get("enutrof_cast_wait_sec", 0.8))
PASS_TURN_HOTKEY = CFG.get("pass_turn_hotkey", "e")
PASS_TURN_PRE_DELAY_SEC = float(CFG.get("pass_turn_pre_delay_sec", 1.5))


class Enutrof:
    """Per-turn decision logic for the Enutrof character. One spell
    (Coins Throwing, ranged single-target) cast as many times as AP
    allows. Walks toward the nearest enemy only when not already in
    range with LoS.

    play_turn(ctx) is registered on Combat.on_turn_start.
    on_fight_engaged(snap) is registered on Combat.on_fight_engaged."""

    def __init__(self, state, cal, map_data):
        self.state = state
        self.cal = cal
        self.map_data = map_data
        self.static_obstacles: set[int] = set()

    # --- Combat callbacks ---

    def on_fight_engaged(self, snap):
        """Load static obstacles for the current map and prune any that
        overlap live entities (calibration may have mis-clicked spawn
        cells as obstacles)."""
        map_id = snap.map_id
        self._prune_obstacles_from_entities(map_id, snap)
        self.static_obstacles = set(
            (self.map_data.get(map_id) or {}).get("obstacles") or ()
        )
        if self.static_obstacles:
            print(f"  loaded {len(self.static_obstacles)} static obstacle(s) "
                  f"for map={map_id}")

    def play_turn(self, ctx):
        """Cast Coins until AP runs out, walking between casts if no
        enemy is in range. See module docstring for the picker rule."""
        snap = ctx.snap
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        my_mp = me.mp if me else 0
        me_cell = my_fight_cell(snap)

        enemies = alive_enemies(snap)
        if not enemies:
            print("  no alive enemies in snapshot; passing")
            pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)
            return

        print(f"  my_cell={me_cell} my_ap={my_ap} my_mp={my_mp} "
              f"enemies={len(enemies)} nearest_cell={enemies[0].cell} "
              f"nearest_dist={cell_distance(me_cell, enemies[0].cell) if me_cell else '?'}")

        mp_remaining = my_mp
        pending_settle = 0.0
        casts_this_turn: dict[int, int] = {}

        # Walk-first: close to adjacent whenever we have MP and the
        # nearest enemy isn't already touching us. Coins is fine at range,
        # but the obstacle calibration conflates holes with peaks (see
        # project_obstacles_holes_vs_peaks), so being closer cuts down on
        # casts the server silently rejects through a real peak. Also
        # keeps us harder to kite. walk_toward naturally stops at dist<=1.
        if mp_remaining > 0 and me_cell:
            nearest = enemies[0]
            if cell_distance(me_cell, nearest.cell) > 1:
                print(f"  walk-first toward id={nearest.id} cell={nearest.cell} "
                      f"dist={cell_distance(me_cell, nearest.cell)} mp={mp_remaining}")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    nearest.cell, self.state, self.cal, self.static_obstacles,
                    mp_override=mp_remaining)

        while my_ap >= COINS_AP_COST and self.state.snapshot().in_combat:
            snap = self.state.snapshot()
            # Server-truth AP floor: if GTM fires and says we've spent
            # more than our local count, adopt the lower number. Prevents
            # casting forever when the local AP is wrong.
            me_now = snap.fight_entities.get(snap.my_id)
            if me_now and me_now.ap < my_ap:
                my_ap = me_now.ap
                if my_ap < COINS_AP_COST:
                    break

            me_cell = my_fight_cell(snap) or me_cell
            if not me_cell:
                print(f"  no me_cell; stopping")
                break

            enemies = alive_enemies(snap)
            if not enemies:
                break

            def _can_cast(t):
                d = cell_distance(me_cell, t.cell)
                if not (COINS_MIN_RANGE <= d <= COINS_MAX_RANGE):
                    return False
                # Static obstacles are intentionally NOT in the blocker
                # set: calibration models holes and peaks identically, but
                # only peaks block LoS. Be permissive and let the server
                # reject the rare cast that hits a real peak.
                bl = {
                    e.cell for e in snap.fight_entities.values()
                    if e.alive and e.cell > 0
                    and e.id != snap.my_id and e.id != t.id
                }
                return line_of_sight(me_cell, t.cell, bl)

            castable = [e for e in enemies if _can_cast(e)]

            if not castable:
                if mp_remaining <= 0:
                    print(f"  no castable enemy and mp=0; stopping")
                    break
                target = enemies[0]
                me_cell_before = me_cell
                print(f"  no castable enemy; walking toward id={target.id} "
                      f"cell={target.cell} mp={mp_remaining}")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    target.cell, self.state, self.cal, self.static_obstacles,
                    mp_override=mp_remaining)
                if me_cell == me_cell_before:
                    print(f"  walk made no progress; stopping")
                    break
                continue

            # Least-cast-first; ties broken by Po distance.
            target = min(castable, key=lambda e: (
                casts_this_turn.get(e.id, 0),
                cell_distance(me_cell, e.cell)))

            if pending_settle > 0:
                time.sleep(pending_settle + COINS_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            dist = cell_distance(me_cell, target.cell)
            n_prior = casts_this_turn.get(target.id, 0)
            print(f"  CAST id={target.id} cell={target.cell} dist={dist} "
                  f"ap_pre={my_ap} prior_casts={n_prior}")
            self._cast_coins(target.cell, me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= COINS_AP_COST
            casts_this_turn[target.id] = n_prior + 1

        if not self.state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)

    # --- Spell casts ---

    def _cast_coins(self, target_cell, me_cell):
        print(f"  CAST Coins Throwing hotkey={COINS_HOTKEY!r} "
              f"target_cell={target_cell}")
        cast_at_cell(COINS_HOTKEY, target_cell, self.cal, caster_cell=me_cell)

    # --- Obstacle hygiene (same logic as Sacrieur) ---

    def _prune_obstacles_from_entities(self, map_id, snap):
        """Drop obstacle cells that overlap live entities or saved start
        cells. Calibration sometimes mis-clicks mob spawn cells as
        obstacles; this prunes them when we observe a fight there."""
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
