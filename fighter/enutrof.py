"""Enutrof: per-turn brain plus class-specific spell knowledge.

Strategy is intentionally simple -- one spell, Coins Throwing (long-range
single-target, 2 AP at level ~5, range up to 13). Each turn: pick the
nearest enemy that's in range AND LoS-clear; if no such enemy exists,
walk toward the nearest alive enemy and re-check. LoS blockers are
static obstacles + every other live entity (Dofus entities block LoS).
Cast as many times as AP allows, distributing across castable enemies
so we don't pile multiple hits on a target that may have died on the
first hit -- the proxy only updates `alive`/`hp` on GTM packets which
fire at turn boundaries, so mid-turn there's no kill signal.

Walking helpers (walk_toward, etc.) live in fighter.walking — shared
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
        """Body of one combat turn. Walk into Coins range/LoS if needed,
        then cast until AP exhausted, then pass."""
        snap = ctx.snap
        me_cell = my_fight_cell(snap)
        me = snap.fight_entities.get(snap.my_id)
        my_ap = me.ap if me else 0
        my_mp = me.mp if me else 0

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
        walked = False
        # Casts per target this turn. GTM (which carries alive/hp) only
        # fires at turn boundaries (proxy state.go), so mid-turn we have
        # no signal that a target just died. Distributing casts across
        # enemies (least-hit-this-turn first) minimizes overkill on a
        # mob that died on an earlier cast.
        casts_this_turn: dict[int, int] = {}

        while my_ap >= COINS_AP_COST and self.state.snapshot().in_combat:
            snap = self.state.snapshot()
            enemies = alive_enemies(snap)
            if not enemies:
                break
            me_cell = my_fight_cell(snap) or me_cell
            if not me_cell:
                print(f"  no me_cell; aborting cast loop")
                break
            # Castable = in range AND LoS clear. LoS blockers are static
            # obstacles + every other live entity (Dofus entities block
            # LoS). Pick the castable enemy with the fewest casts this
            # turn; ties broken by Po distance. If no enemy is currently
            # castable, fall back to nearest alive so the walk targets
            # something reasonable.
            other_alive_by_target = {
                t.id: {
                    e.cell for e in snap.fight_entities.values()
                    if e.alive and e.cell > 0
                    and e.id != snap.my_id and e.id != t.id
                }
                for t in enemies
            }

            def _can_cast(e):
                d = cell_distance(me_cell, e.cell)
                if not (COINS_MIN_RANGE <= d <= COINS_MAX_RANGE):
                    return False
                blockers = set(self.static_obstacles) | other_alive_by_target[e.id]
                return line_of_sight(me_cell, e.cell, blockers)

            castable = [e for e in enemies if _can_cast(e)]
            if castable:
                target = min(
                    castable,
                    key=lambda e: (casts_this_turn.get(e.id, 0),
                                   cell_distance(me_cell, e.cell)),
                )
            else:
                target = enemies[0]
            dist = cell_distance(me_cell, target.cell)

            if not castable:
                if walked or mp_remaining == 0:
                    print(f"  no castable enemy (nearest id={target.id} "
                          f"cell={target.cell} dist={dist}); mp_left={mp_remaining} "
                          f"walked_already={walked}; stopping")
                    break
                me_cell_before = me_cell
                print(f"  no castable enemy; walking toward id={target.id} "
                      f"cell={target.cell} dist={dist} mp={mp_remaining}")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    target.cell, self.state, self.cal, self.static_obstacles,
                    mp_override=mp_remaining)
                walked = True
                if me_cell == me_cell_before:
                    print(f"  walk_toward made no progress; stopping")
                    break
                continue

            if pending_settle > 0:
                time.sleep(pending_settle + COINS_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            n_prior = casts_this_turn.get(target.id, 0)
            print(f"  CAST Coins Throwing target id={target.id} "
                  f"cell={target.cell} dist={dist} ap_pre={my_ap} "
                  f"prior_casts_on_target={n_prior}")
            self._cast_coins(target.cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= COINS_AP_COST
            casts_this_turn[target.id] = n_prior + 1

        if not self.state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)

    # --- Spell casts ---

    def _cast_coins(self, target_cell):
        print(f"  CAST Coins Throwing hotkey={COINS_HOTKEY!r} "
              f"target_cell={target_cell}")
        cast_at_cell(COINS_HOTKEY, target_cell, self.cal)

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
