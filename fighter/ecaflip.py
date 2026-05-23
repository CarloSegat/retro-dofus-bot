"""Ecaflip: per-turn brain plus class-specific spell knowledge.

Three spells, in priority order each turn:

  Perception (self-cast damage buff): cast on T1 and every
    PERCEPTION_COOLDOWN_TURNS turns thereafter.
  All or Nothing (self-cast AoE damage): fires when at least
    ALL_OR_NOTHING_TRIGGER_COUNT alive enemies are within
    ALL_OR_NOTHING_TRIGGER_RANGE cells of us; cooldown
    ALL_OR_NOTHING_COOLDOWN_TURNS turns.
  Heads or Tails (single-target, range 1..6): walk into range, then
    spam until AP runs out (least-cast-first picker, ties = nearest).

Kite by default: walk-first only closes in when nothing is already
in range with LoS. After casting at least one HoT we spend the
remaining MP retreating from the nearest live enemy so the next
turn opens at distance again. The HoT loop still has its own inner
fallback walk for the rare case where nothing is castable after
the self-buffs (e.g. opener with all enemies LoS-blocked). AP is
tracked locally with a floor of the snapshot's AP.

The least-cast-first picker spreads damage across the group; combined
with the per-cast settle that lets GTM updates land between casts,
dead enemies drop out of `alive_enemies` before we double-tap them.

Walking helpers live in fighter.walking -- shared with Sacrieur/Enutrof.

Wired via Combat.on_turn_start(ecaflip.play_turn) and
Combat.on_fight_engaged(ecaflip.on_fight_engaged) in Orchestrator.
"""
import time

from dofus.actions import cast_at_cell, pass_turn
from dofus.cell_grid import cell_distance, line_of_sight
from dofus.map_data import save as save_map_data
from fighter.helpers import alive_enemies, my_fight_cell
from fighter.walking import walk_away, walk_toward
from utils import CFG


# === Config-driven knobs ===
HEADS_HOTKEY = CFG.get("ecaflip_heads_or_tails_hotkey", "1")
HEADS_AP_COST = int(CFG.get("ecaflip_heads_or_tails_ap_cost", 3))
HEADS_MIN_RANGE = int(CFG.get("ecaflip_heads_or_tails_min_range", 1))
HEADS_MAX_RANGE = int(CFG.get("ecaflip_heads_or_tails_max_range", 6))
HEADS_POST_WALK_EXTRA_SETTLE_SEC = float(
    CFG.get("ecaflip_heads_or_tails_post_walk_settle_sec", 0.33))
PERCEPTION_HOTKEY = CFG.get("ecaflip_perception_hotkey", "5")
PERCEPTION_AP_COST = int(CFG.get("ecaflip_perception_ap_cost", 2))
PERCEPTION_COOLDOWN_TURNS = int(CFG.get("ecaflip_perception_cooldown_turns", 4))
ALL_OR_NOTHING_HOTKEY = CFG.get("ecaflip_all_or_nothing_hotkey", "6")
ALL_OR_NOTHING_AP_COST = int(CFG.get("ecaflip_all_or_nothing_ap_cost", 5))
ALL_OR_NOTHING_COOLDOWN_TURNS = int(
    CFG.get("ecaflip_all_or_nothing_cooldown_turns", 4))
ALL_OR_NOTHING_TRIGGER_RANGE = int(
    CFG.get("ecaflip_all_or_nothing_trigger_range", 8))
ALL_OR_NOTHING_TRIGGER_COUNT = int(
    CFG.get("ecaflip_all_or_nothing_trigger_count", 2))
CAST_WAIT_SEC = float(CFG.get("ecaflip_cast_wait_sec", 0.8))
PASS_TURN_HOTKEY = CFG.get("pass_turn_hotkey", "e")
PASS_TURN_PRE_DELAY_SEC = float(CFG.get("pass_turn_pre_delay_sec", 1.5))


class Ecaflip:
    """Per-turn decision logic for the Ecaflip character.

    Spells:
      - Perception (self-buff +damage): T1 force-cast, then every
        PERCEPTION_COOLDOWN_TURNS turns.
      - All or Nothing (self-cast AoE damage): fires when at least
        ALL_OR_NOTHING_TRIGGER_COUNT alive enemies are within
        ALL_OR_NOTHING_TRIGGER_RANGE cells of us; cooldown
        ALL_OR_NOTHING_COOLDOWN_TURNS.
      - Heads or Tails (single-target ranged): spammed until AP runs out.

    play_turn(ctx) is registered on Combat.on_turn_start.
    on_fight_engaged(snap) is registered on Combat.on_fight_engaged."""

    def __init__(self, state, cal, map_data, aon_enabled=True):
        self.state = state
        self.cal = cal
        self.map_data = map_data
        # When False, the All or Nothing cast block is skipped entirely
        # (cooldown bookkeeping included). Heads or Tails + Perception
        # behave normally. Useful when the spell is too AP-hungry for
        # short-AP runs.
        self.aon_enabled = aon_enabled
        self.static_obstacles: set[int] = set()
        # Fight-scoped state -- reset on on_fight_engaged.
        self.last_perception_turn = -PERCEPTION_COOLDOWN_TURNS
        self.last_aon_turn = -ALL_OR_NOTHING_COOLDOWN_TURNS
        self.is_first_turn = True

    # --- Combat callbacks ---

    def on_fight_engaged(self, snap):
        """Reset fight-scoped state. Load static obstacles for the
        current map and prune any that overlap live entities."""
        map_id = snap.map_id
        self._prune_obstacles_from_entities(map_id, snap)
        self.static_obstacles = set(
            (self.map_data.get(map_id) or {}).get("obstacles") or ()
        )
        if self.static_obstacles:
            print(f"  loaded {len(self.static_obstacles)} static obstacle(s) "
                  f"for map={map_id}")
        self.last_perception_turn = -PERCEPTION_COOLDOWN_TURNS
        self.last_aon_turn = -ALL_OR_NOTHING_COOLDOWN_TURNS
        self.is_first_turn = True

    def play_turn(self, ctx):
        """Cast Perception (T1 / every N turns) and All or Nothing (when
        a cluster forms around us), then spam Heads or Tails. Skips
        walking in when something is already in range/LoS; after at
        least one HoT lands, retreats from the nearest live enemy with
        whatever MP is left. See module docstring."""
        new_turn = ctx.turn_n
        is_first_turn = self.is_first_turn
        self.is_first_turn = False
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

        # Perception: T1 force-cast, then every PERCEPTION_COOLDOWN_TURNS turns.
        # Self-cast, fires before any walking so the click lands on a
        # stationary cell.
        turns_since_perception = new_turn - self.last_perception_turn
        perception_ready = (is_first_turn
                            or turns_since_perception >= PERCEPTION_COOLDOWN_TURNS)
        if perception_ready and me_cell and my_ap >= PERCEPTION_AP_COST:
            tag = "T1 force-cast" if is_first_turn else (
                f"cooldown ready (last_cast_turn={self.last_perception_turn})")
            print(f"  perception: {tag}, casting")
            self._cast_perception(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= PERCEPTION_AP_COST
            self.last_perception_turn = new_turn
            print(f"  perception cast on turn {new_turn}; ap_left~{my_ap} "
                  f"(next available turn {new_turn + PERCEPTION_COOLDOWN_TURNS})")
        elif not perception_ready:
            print(f"  perception: on cooldown ({turns_since_perception}/"
                  f"{PERCEPTION_COOLDOWN_TURNS} turns since last cast on turn "
                  f"{self.last_perception_turn})")

        # All or Nothing: self-cast AoE. Fire when a cluster of >=N enemies
        # is within trigger-range of us. Cooldown-gated. Skipped entirely
        # when aon_enabled is False (runtime prompt).
        if self.aon_enabled:
            turns_since_aon = new_turn - self.last_aon_turn
            aon_ready = turns_since_aon >= ALL_OR_NOTHING_COOLDOWN_TURNS
            nearby = []
            if me_cell:
                nearby = [e for e in enemies
                          if cell_distance(me_cell, e.cell) <= ALL_OR_NOTHING_TRIGGER_RANGE]
            if (aon_ready and len(nearby) >= ALL_OR_NOTHING_TRIGGER_COUNT
                    and me_cell and my_ap >= ALL_OR_NOTHING_AP_COST):
                print(f"  all-or-nothing: {len(nearby)} enemies within "
                      f"{ALL_OR_NOTHING_TRIGGER_RANGE} cells, casting")
                self._cast_all_or_nothing(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= ALL_OR_NOTHING_AP_COST
                self.last_aon_turn = new_turn
                print(f"  all-or-nothing cast on turn {new_turn}; ap_left~{my_ap} "
                      f"(next available turn {new_turn + ALL_OR_NOTHING_COOLDOWN_TURNS})")
            elif (not aon_ready
                  and len(nearby) >= ALL_OR_NOTHING_TRIGGER_COUNT):
                print(f"  all-or-nothing: cluster of {len(nearby)} present but on "
                      f"cooldown ({turns_since_aon}/{ALL_OR_NOTHING_COOLDOWN_TURNS})")

        mp_remaining = my_mp
        pending_settle = 0.0
        casts_this_turn: dict[int, int] = {}

        # Walk-first ONLY when nothing is castable from where we stand --
        # if anything is already in range with LoS we skip closing in and
        # let the HoT loop fire from the current cell, then retreat.
        if mp_remaining > 0 and me_cell:
            already_castable = self._castable_enemies(me_cell, snap, enemies)
            if already_castable:
                print(f"  walk-first skipped: {len(already_castable)} enemy/ies "
                      f"already castable from {me_cell}")
            else:
                nearest = enemies[0]
                if cell_distance(me_cell, nearest.cell) > 1:
                    print(f"  walk-first toward id={nearest.id} cell={nearest.cell} "
                          f"dist={cell_distance(me_cell, nearest.cell)} mp={mp_remaining}")
                    me_cell, _, mp_remaining, pending_settle = walk_toward(
                        nearest.cell, self.state, self.cal, self.static_obstacles,
                        mp_override=mp_remaining)

        while my_ap >= HEADS_AP_COST and self.state.snapshot().in_combat:
            snap = self.state.snapshot()
            me_now = snap.fight_entities.get(snap.my_id)
            if me_now and me_now.ap < my_ap:
                my_ap = me_now.ap
                if my_ap < HEADS_AP_COST:
                    break

            me_cell = my_fight_cell(snap) or me_cell
            if not me_cell:
                print(f"  no me_cell; stopping")
                break

            enemies = alive_enemies(snap)
            if not enemies:
                break

            castable = self._castable_enemies(me_cell, snap, enemies)

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
                time.sleep(pending_settle + HEADS_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            dist = cell_distance(me_cell, target.cell)
            n_prior = casts_this_turn.get(target.id, 0)
            print(f"  CAST id={target.id} cell={target.cell} dist={dist} "
                  f"ap_pre={my_ap} prior_casts={n_prior}")
            self._cast_heads(target.cell, me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= HEADS_AP_COST
            casts_this_turn[target.id] = n_prior + 1

        # Kite: after landing at least one HoT, spend leftover MP walking
        # away from the nearest live enemy so they have to close the gap
        # again before they can melee us.
        if (any(casts_this_turn.values()) and mp_remaining > 0
                and self.state.snapshot().in_combat):
            snap_now = self.state.snapshot()
            enemies_now = alive_enemies(snap_now)
            me_cell_now = my_fight_cell(snap_now) or me_cell
            if enemies_now and me_cell_now:
                nearest = enemies_now[0]
                print(f"  retreat after casts from id={nearest.id} "
                      f"cell={nearest.cell} dist={cell_distance(me_cell_now, nearest.cell)} "
                      f"mp_left~{mp_remaining}")
                walk_away(nearest.cell, self.state, self.cal,
                          self.static_obstacles, max_steps=mp_remaining)

        if not self.state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)

    # --- Targeting ---

    @staticmethod
    def _castable_enemies(me_cell, snap, enemies):
        """Enemies within HoT range AND LoS from me_cell. LoS blockers =
        every other live entity; statics are dropped from the blocker
        set, same hole/peak workaround as Enutrof
        ([[obstacles-holes-vs-peaks]])."""
        out = []
        for t in enemies:
            d = cell_distance(me_cell, t.cell)
            if not (HEADS_MIN_RANGE <= d <= HEADS_MAX_RANGE):
                continue
            bl = {
                e.cell for e in snap.fight_entities.values()
                if e.alive and e.cell > 0
                and e.id != snap.my_id and e.id != t.id
            }
            if line_of_sight(me_cell, t.cell, bl):
                out.append(t)
        return out

    # --- Spell casts ---

    def _cast_heads(self, target_cell, me_cell):
        print(f"  CAST Heads or Tails hotkey={HEADS_HOTKEY!r} "
              f"target_cell={target_cell}")
        cast_at_cell(HEADS_HOTKEY, target_cell, self.cal, caster_cell=me_cell)

    def _cast_perception(self, my_cell):
        print(f"  CAST Perception hotkey={PERCEPTION_HOTKEY!r} "
              f"self_cell={my_cell}")
        cast_at_cell(PERCEPTION_HOTKEY, my_cell, self.cal)

    def _cast_all_or_nothing(self, my_cell):
        print(f"  CAST All or Nothing hotkey={ALL_OR_NOTHING_HOTKEY!r} "
              f"self_cell={my_cell}")
        cast_at_cell(ALL_OR_NOTHING_HOTKEY, my_cell, self.cal)

    # --- Obstacle hygiene ---

    def _prune_obstacles_from_entities(self, map_id, snap):
        """Drop obstacle cells that overlap live entities or saved start
        cells."""
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
