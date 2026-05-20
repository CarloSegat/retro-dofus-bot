"""Sacrieur: per-turn brain plus class-specific spell knowledge.

Owns the tofu detector (kiter retreat mode). play_turn(ctx) is the
body of a single combat turn: walk, cast, attract, follow-up, pass.
The class holds fight-scoped state (tofu detector, buff cooldown) that
on_fight_engaged resets at the start of each fight.

Walking primitives (walk_toward, walk_away, try_full_walk, pick_next_step,
pick_retreat_step) live in fighter.walking — class-agnostic, shared
with Enutrof.

Wired via Combat.on_turn_start(sacrieur.play_turn) and
Combat.on_fight_engaged(sacrieur.on_fight_engaged) in Orchestrator.
"""
import random
import time

from dofus.actions import cast_at_cell, pass_turn
from dofus.cell_grid import cell_distance, cell_to_uv, line_of_sight
from dofus.map_data import save as save_map_data
from fighter.helpers import alive_enemies, my_fight_cell, wait_for
from fighter.walking import pick_retreat_step, walk_away, walk_toward
from utils import CFG


# === Config-driven knobs ===
DISSOLUTION_HOTKEY = CFG.get("sacrid_dissolution_hotkey")
DISSOLUTION_AP_COST = int(CFG.get("sacrid_dissolution_ap_cost", 4))
DISSOLUTION_POST_WALK_EXTRA_SETTLE_SEC = float(CFG.get("sacrid_dissolution_post_walk_settle_sec", 0.33))
BUFF_HOTKEY = CFG.get("sacrid_buff_hotkey", "3")
BUFF_AP_COST = int(CFG.get("sacrid_buff_ap_cost", 3))
BUFF_MAX_DIST = int(CFG.get("sacrid_buff_max_dist", 6))
BUFF_COOLDOWN_TURNS = int(CFG.get("sacrid_buff_cooldown_turns", 5))
VITAL_HOTKEY = CFG.get("sacrid_vital_hotkey", "ctrl+6")
VITAL_AP_COST = int(CFG.get("sacrid_vital_ap_cost", 3))
VITAL_COOLDOWN_TURNS = int(CFG.get("sacrid_vital_cooldown_turns", 4))
VITAL_POST_WALK_EXTRA_SETTLE_SEC = float(CFG.get("sacrid_vital_post_walk_settle_sec", 0.33))
ATTRACTION_HOTKEY = CFG.get("sacrid_attraction_hotkey", "1")
ATTRACTION_AP_COST = int(CFG.get("sacrid_attraction_ap_cost", 3))
ATTRACTION_MIN_RANGE = int(CFG.get("sacrid_attraction_min_range", 1))
ATTRACTION_MAX_RANGE = int(CFG.get("sacrid_attraction_max_range", 10))
ATTRACTION_POST_WALK_EXTRA_SETTLE_SEC = float(
    CFG.get("sacrid_attraction_post_walk_settle_sec", 0.33))
SWAP_HOTKEY = CFG.get("sacrid_swap_hotkey", "5")
SWAP_AP_COST = int(CFG.get("sacrid_swap_ap_cost", 2))
SWAP_MIN_AP = int(CFG.get("sacrid_swap_min_ap", 6))
SWAP_POST_WALK_EXTRA_SETTLE_SEC = float(CFG.get("sacrid_swap_post_walk_settle_sec", 0.33))
CAST_WAIT_SEC = float(CFG.get("sacrid_cast_wait_sec", 0.8))
PASS_TURN_HOTKEY = CFG.get("pass_turn_hotkey", "e")
PASS_TURN_PRE_DELAY_SEC = float(CFG.get("pass_turn_pre_delay_sec", 1.5))
TOFU_THRESHOLD = int(CFG.get("tofu_detect_threshold", 4))
TOFU_REQUIRED_CYCLES = int(CFG.get("tofu_detect_required_cycles", 3))


class TurnDistanceTracker:
    """Detects hit-and-run "tofu-like" enemies via turn-start distances.

    Called once per our turn-start with the distance to the nearest
    alive enemy BEFORE we move. That snapshot is the cycle's "max" --
    where the enemy ended up after retreating. If the last `required`
    samples are all > `threshold` AND the sequence is not strictly
    decreasing, flip tofu_detected. Sampling mid-cycle would conflate
    enemy approach distance with our own post-move position."""

    def __init__(self, threshold, required_cycles):
        self.threshold = threshold
        self.required = required_cycles
        self.history = []
        self.tofu_detected = False

    def observe_turn_start(self, dist):
        if dist is None or dist <= 0:
            return None
        self.history.append(dist)
        if self.tofu_detected:
            return dist
        if len(self.history) >= self.required:
            recent = self.history[-self.required:]
            all_high = all(d > self.threshold for d in recent)
            strictly_decreasing = all(
                recent[i + 1] < recent[i] for i in range(len(recent) - 1)
            )
            if all_high and not strictly_decreasing:
                self.tofu_detected = True
        return dist


# === Sacrieur class: spell choices + per-turn brain ===

class Sacrieur:
    """Per-turn decision logic for the Sacrieur character. Owns spells
    (Dissolution self-cast AoE, Bold Punishment self-buff, Vital
    Punishment self-cast, Swap, Attraction ranged pull) and
    fight-scoped state (tofu detector, buff cooldown, static obstacles
    for the current map).

    play_turn(ctx) is registered on Combat.on_turn_start.
    on_fight_engaged(snap) is registered on Combat.on_fight_engaged."""

    def __init__(self, state, cal, map_data, buff_enabled=True):
        self.state = state
        self.cal = cal
        self.map_data = map_data
        self.buff_enabled = buff_enabled
        # Fight-scoped state -- reset on on_fight_engaged.
        self.last_buff_turn = -BUFF_COOLDOWN_TURNS
        self.last_vital_turn = -VITAL_COOLDOWN_TURNS
        self.is_first_turn = True
        self.static_obstacles: set[int] = set()
        self.dist_tracker = TurnDistanceTracker(TOFU_THRESHOLD, TOFU_REQUIRED_CYCLES)

    # --- Combat callbacks ---

    def on_fight_engaged(self, snap):
        """Reset fight-scoped state. Loads static obstacles for the
        current map and prunes any that overlap live entities."""
        self.last_buff_turn = -BUFF_COOLDOWN_TURNS
        self.last_vital_turn = -VITAL_COOLDOWN_TURNS
        self.is_first_turn = True
        map_id = snap.map_id
        self._prune_obstacles_from_entities(map_id, snap)
        self.static_obstacles = set(
            (self.map_data.get(map_id) or {}).get("obstacles") or ()
        )
        if self.static_obstacles:
            print(f"  loaded {len(self.static_obstacles)} static obstacle(s) "
                  f"for map={map_id}")
        self.dist_tracker = TurnDistanceTracker(TOFU_THRESHOLD, TOFU_REQUIRED_CYCLES)

    def play_turn(self, ctx):
        """Body of one combat turn. Called via Combat.on_turn_start.
        Calls pass_turn at the end -- Combat doesn't manage AP/MP.

        First-turn special-casing: force Bold + Vital Punishment, skip
        Attraction. The buff distance/AP gates exist to avoid wasting
        AP mid-fight, but on T1 we definitely want both punishments
        stacked before anything else (no contest for AP yet, and a fresh
        buff is most valuable at the start)."""
        new_turn = ctx.turn_n
        is_first_turn = self.is_first_turn
        self.is_first_turn = False
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

        was_tofu = self.dist_tracker.tofu_detected
        turn_start_dist = (cell_distance(me_cell, enemies[0].cell)
                           if me_cell else None)
        recorded = self.dist_tracker.observe_turn_start(turn_start_dist)
        if recorded is not None:
            print(f"  [tofu-track] turn-start dist={recorded} "
                  f"(history={self.dist_tracker.history[-TOFU_REQUIRED_CYCLES:]})")
        if self.dist_tracker.tofu_detected and not was_tofu:
            print(f"  [tofu] hit-and-run pattern detected: last "
                  f"{TOFU_REQUIRED_CYCLES} turn-start distances all "
                  f"> {TOFU_THRESHOLD} and not strictly decreasing; "
                  f"switching to retreat mode for the rest of this fight")

        if self.dist_tracker.tofu_detected:
            if self._play_tofu_turn(me_cell, my_ap, my_mp, enemies, new_turn):
                return  # handled in tofu branch, turn passed

        # === Normal combat ===

        # Buff with cooldown; distance-gated.
        nearest_dist = (cell_distance(me_cell, enemies[0].cell)
                        if me_cell else 99)
        turns_since_buff = new_turn - self.last_buff_turn
        buff_ready = turns_since_buff >= BUFF_COOLDOWN_TURNS
        if not self.buff_enabled:
            pass
        elif buff_ready and me_cell and my_ap >= BUFF_AP_COST:
            if is_first_turn:
                # T1: cast unconditionally -- distance and "preserve AP
                # for Dissolution" gates don't apply when the fight has
                # just started and there's no contest for AP yet.
                print(f"  buff: T1 force-cast (bypassing distance + AP gates)")
                self._cast_bold_punishment(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= BUFF_AP_COST
                self.last_buff_turn = new_turn
                print(f"  buff cast on turn {new_turn}; ap_left~{my_ap}")
            else:
                # If a Dissolution is firing this turn (enemy already adjacent),
                # don't burn AP on the buff unless we'd still have enough left
                # to also cast Dissolution. Otherwise the buff steals the hit
                # -- exactly what happens under enemy AP-drain.
                needed = BUFF_AP_COST + DISSOLUTION_AP_COST
                if nearest_dist == 1 and my_ap < needed:
                    print(f"  buff: skip (adjacent enemy, ap={my_ap} < "
                          f"buff+dissolution={needed}; preserving AP for Dissolution)")
                elif nearest_dist <= BUFF_MAX_DIST:
                    print(f"  buff: nearest_dist={nearest_dist} <= {BUFF_MAX_DIST}, "
                          f"cooldown ready (last_cast_turn={self.last_buff_turn}), casting")
                    self._cast_bold_punishment(me_cell)
                    time.sleep(CAST_WAIT_SEC)
                    my_ap -= BUFF_AP_COST
                    self.last_buff_turn = new_turn
                    print(f"  buff cast on turn {new_turn}; ap_left~{my_ap} "
                          f"(next available turn {new_turn + BUFF_COOLDOWN_TURNS})")
                else:
                    print(f"  buff: nearest_dist={nearest_dist} > {BUFF_MAX_DIST}, "
                          f"skipping (too far -- buff would expire before we get hit)")
        elif not buff_ready:
            print(f"  buff: on cooldown ({turns_since_buff}/{BUFF_COOLDOWN_TURNS} "
                  f"turns since last cast on turn {self.last_buff_turn})")

        # T1 Vital: cast right after Bold so both punishments land before
        # anything else competes for AP. With my_ap=6 (buff+vital=6) we
        # spend the whole turn on buffs; with more AP, Dissolution still
        # gets its turn below. Cast before walking so the self-click
        # lands on a stationary cell.
        if (is_first_turn and self.state.snapshot().in_combat and me_cell
                and my_ap >= VITAL_AP_COST):
            print(f"  vital: T1 force-cast (priority on punishments)")
            self._cast_vital_punishment(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= VITAL_AP_COST
            self.last_vital_turn = new_turn
            print(f"  vital cast on turn {new_turn}; ap_left~{my_ap}")

        # Close on nearest enemy, then Dissolution if adjacent.
        nearest = enemies[0]
        dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
        print(f"  nearest id={nearest.id} cell={nearest.cell} dist={dist} "
              f"my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")

        mp_remaining = my_mp
        pending_settle = 0.0
        if dist > 1 and me_cell and mp_remaining > 0:
            me_cell, _, mp_remaining, pending_settle = walk_toward(
                nearest.cell, self.state, self.cal, self.static_obstacles)
            enemies = alive_enemies(self.state.snapshot())
            dist = (cell_distance(me_cell, enemies[0].cell)
                    if enemies and me_cell else 99)

        # Pre-Dissolution swap setup: if exactly one enemy is adjacent and
        # that enemy has another enemy adjacent to it, swap with it so the
        # follow-up Dissolution hits both enemies. Skipped if 2+ already
        # adjacent (Dissolution already double-hits) or ap<SWAP_MIN_AP
        # (after swap we wouldn't have enough left for Dissolution too).
        if dist == 1:
            swap_target = self._pick_swap_target(me_cell, my_ap)
            if swap_target is not None:
                if pending_settle > 0:
                    time.sleep(pending_settle + SWAP_POST_WALK_EXTRA_SETTLE_SEC)
                    pending_settle = 0.0
                old_cell = me_cell
                self._cast_swap(swap_target.cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= SWAP_AP_COST
                me_cell = self._me_cell_after_swap(old_cell, swap_target.cell)
                print(f"  swap landed: me_cell {old_cell} -> {me_cell} "
                      f"(ap_left~{my_ap})")

        if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
            if pending_settle > 0:
                time.sleep(pending_settle + DISSOLUTION_POST_WALK_EXTRA_SETTLE_SEC)
                pending_settle = 0.0
            self._cast_dissolution(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= DISSOLUTION_AP_COST
        elif dist == 1 and my_ap < DISSOLUTION_AP_COST:
            print(f"  adjacent but ap={my_ap} < cost={DISSOLUTION_AP_COST}; "
                  f"not casting this turn")
        elif dist > 1:
            print(f"  nothing adjacent (nearest_dist={dist}); not casting this turn")

        # Attraction: pull the nearest line-aligned enemy with LoS. Replaces
        # the old bow burst. One cast per turn (Retro caps Attraction at 1
        # cast per target per turn, and a single pull is usually enough to
        # bring the enemy adjacent for next turn's Dissolution). Clears
        # tofu_detected on a successful pull -- once they're glued to us
        # the kite is broken. Skipped on T1: the punishments + Dissolution
        # are higher priority before AP gets drained.
        if (not is_first_turn and self.state.snapshot().in_combat and me_cell
                and my_ap >= ATTRACTION_AP_COST):
            target = self._pick_attraction_target(me_cell, my_ap)
            if target is not None:
                if pending_settle > 0:
                    time.sleep(pending_settle + ATTRACTION_POST_WALK_EXTRA_SETTLE_SEC)
                    pending_settle = 0.0
                self._cast_attraction(target.cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= ATTRACTION_AP_COST
                if self.dist_tracker.tofu_detected:
                    print(f"  [tofu] attraction pulled id={target.id}; "
                          f"exiting retreat mode")
                    self.dist_tracker.tofu_detected = False

        # Follow-up walk: close on a more-distant enemy with leftover MP.
        if self.state.snapshot().in_combat and mp_remaining > 0 and me_cell:
            distant = [e for e in alive_enemies(self.state.snapshot())
                       if cell_distance(me_cell, e.cell) > 1]
            if distant:
                follow = distant[0]
                follow_dist = cell_distance(me_cell, follow.cell)
                print(f"  follow-up walk: closing toward id={follow.id} "
                      f"cell={follow.cell} dist={follow_dist} "
                      f"mp_left={mp_remaining} (fast-fail)")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    follow.cell, self.state, self.cal, self.static_obstacles,
                    mp_override=mp_remaining, fast_fail=True)

        # Vital Punishment: leftover-AP filler, self-cast like Bold
        # Punishment, 4-turn cooldown. Lowest priority -- only fires
        # if AP survived buff + Dissolution + Attraction. Same post-walk
        # hotkey-drop risk as the other spells.
        if (self.state.snapshot().in_combat and me_cell
                and my_ap >= VITAL_AP_COST):
            turns_since_vital = new_turn - self.last_vital_turn
            if turns_since_vital >= VITAL_COOLDOWN_TURNS:
                if pending_settle > 0:
                    time.sleep(pending_settle + VITAL_POST_WALK_EXTRA_SETTLE_SEC)
                    pending_settle = 0.0
                self._cast_vital_punishment(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= VITAL_AP_COST
                self.last_vital_turn = new_turn
                print(f"  vital cast on turn {new_turn}; ap_left~{my_ap} "
                      f"(next available turn {new_turn + VITAL_COOLDOWN_TURNS})")
            else:
                print(f"  vital: on cooldown ({turns_since_vital}/"
                      f"{VITAL_COOLDOWN_TURNS} turns since last cast on "
                      f"turn {self.last_vital_turn})")

        if not self.state.snapshot().in_combat:
            return
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)

    def _play_tofu_turn(self, me_cell, my_ap, my_mp, enemies, new_turn):
        """Tofu retreat branch. Returns True if the turn was handled
        (caller returns immediately), False to fall through to normal
        combat (cornered case)."""
        nearest = enemies[0]
        dist = cell_distance(me_cell, nearest.cell) if me_cell else 99
        cells_to_close = max(0, dist - 1)
        can_reach = me_cell is not None and cells_to_close <= my_mp
        can_attack = my_ap >= DISSOLUTION_AP_COST
        will_attack = (dist == 1 or can_reach) and can_attack
        can_retreat = me_cell is not None and pick_retreat_step(
            me_cell, nearest.cell, self.state.snapshot(),
            set(), set(self.static_obstacles)) is not None
        can_attract = (me_cell is not None
                       and self._pick_attraction_target(me_cell, my_ap) is not None)
        if not will_attack and not can_retreat and not can_attract:
            print(f"  [tofu] cornered at {me_cell}: no retreat step "
                  f"from id={nearest.id} cell={nearest.cell} dist={dist} "
                  f"and can't close+cast (mp={my_mp} ap={my_ap}) "
                  f"and no attraction target on line with LoS; "
                  f"falling back to normal combat this turn")
            return False  # fall through

        print(f"  [tofu] nearest id={nearest.id} cell={nearest.cell} "
              f"dist={dist} my_cell={me_cell} my_ap={my_ap} my_mp={my_mp}")
        mp_remaining = my_mp
        pending_settle = 0.0
        if can_reach and can_attack:
            if dist > 1:
                print(f"  [tofu] closing to attack (need {cells_to_close} "
                      f"mp, have {my_mp}; ap={my_ap})")
                me_cell, _, mp_remaining, pending_settle = walk_toward(
                    nearest.cell, self.state, self.cal, self.static_obstacles)
                enemies = alive_enemies(self.state.snapshot())
                dist = (cell_distance(me_cell, enemies[0].cell)
                        if enemies and me_cell else 99)
            if dist == 1 and my_ap >= DISSOLUTION_AP_COST:
                if pending_settle > 0:
                    time.sleep(pending_settle + DISSOLUTION_POST_WALK_EXTRA_SETTLE_SEC)
                self._cast_dissolution(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= DISSOLUTION_AP_COST
                pending_settle = 0.0
        elif dist == 1 and can_attack:
            self._cast_dissolution(me_cell)
            time.sleep(CAST_WAIT_SEC)
            my_ap -= DISSOLUTION_AP_COST
        elif dist == 1:
            print(f"  [tofu] adjacent but ap={my_ap} < "
                  f"{DISSOLUTION_AP_COST}; not casting")

        # Attraction from current cell: pulls the kiter in. Replaces the
        # old bow burst. On a successful cast the kite is broken and we
        # leave tofu mode.
        if (self.state.snapshot().in_combat and me_cell
                and my_ap >= ATTRACTION_AP_COST):
            target = self._pick_attraction_target(me_cell, my_ap)
            if target is not None:
                if pending_settle > 0:
                    time.sleep(pending_settle + ATTRACTION_POST_WALK_EXTRA_SETTLE_SEC)
                    pending_settle = 0.0
                self._cast_attraction(target.cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= ATTRACTION_AP_COST
                if self.dist_tracker.tofu_detected:
                    print(f"  [tofu] attraction pulled id={target.id}; "
                          f"exiting retreat mode -- the kite is broken")
                    self.dist_tracker.tofu_detected = False

        # Vital Punishment leftover-AP filler (see play_turn for rationale).
        # Cast before retreat so the click lands on a stationary cell.
        if (self.state.snapshot().in_combat and me_cell
                and my_ap >= VITAL_AP_COST):
            turns_since_vital = new_turn - self.last_vital_turn
            if turns_since_vital >= VITAL_COOLDOWN_TURNS:
                self._cast_vital_punishment(me_cell)
                time.sleep(CAST_WAIT_SEC)
                my_ap -= VITAL_AP_COST
                self.last_vital_turn = new_turn
                print(f"  [tofu] vital cast on turn {new_turn}; "
                      f"ap_left~{my_ap} (next available turn "
                      f"{new_turn + VITAL_COOLDOWN_TURNS})")
            else:
                print(f"  [tofu] vital: on cooldown ({turns_since_vital}/"
                      f"{VITAL_COOLDOWN_TURNS})")

        if mp_remaining > 0 and me_cell:
            live = alive_enemies(self.state.snapshot())
            if live:
                anchor = live[0].cell
                steps = random.randint(0, mp_remaining)
                print(f"  [tofu] retreating {steps} step(s) away from "
                      f"cell={anchor} (mp_left={mp_remaining})")
                if steps > 0:
                    walk_away(anchor, self.state, self.cal,
                              self.static_obstacles, max_steps=steps)
        if not self.state.snapshot().in_combat:
            return True
        print("  PASS (pass-turn hotkey)")
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)
        return True

    # --- Spell casts (thin wrappers around dofus.actions.cast_at_cell) ---

    def _cast_dissolution(self, my_cell):
        print(f"  CAST Dissolution hotkey={DISSOLUTION_HOTKEY!r} self_cell={my_cell}")
        cast_at_cell(DISSOLUTION_HOTKEY, my_cell, self.cal)

    def _cast_bold_punishment(self, my_cell):
        print(f"  CAST Bold Punishment hotkey={BUFF_HOTKEY!r} self_cell={my_cell}")
        cast_at_cell(BUFF_HOTKEY, my_cell, self.cal)

    def _cast_vital_punishment(self, my_cell):
        print(f"  CAST Vital Punishment hotkey={VITAL_HOTKEY!r} self_cell={my_cell}")
        cast_at_cell(VITAL_HOTKEY, my_cell, self.cal)

    def _cast_swap(self, target_cell):
        print(f"  CAST Swap hotkey={SWAP_HOTKEY!r} target_cell={target_cell}")
        cast_at_cell(SWAP_HOTKEY, target_cell, self.cal)

    def _cast_attraction(self, target_cell):
        print(f"  CAST Attraction hotkey={ATTRACTION_HOTKEY!r} "
              f"target_cell={target_cell}")
        cast_at_cell(ATTRACTION_HOTKEY, target_cell, self.cal)

    # --- Attraction targeting ---

    @staticmethod
    def _same_iso_line(a, b):
        """True iff `a` and `b` share an iso-grid axis -- the only
        geometry Attraction accepts. In (u, v) coords (see cell_grid),
        the four edge-step directions move along u xor v, so two cells
        on the same axial line share either u or v."""
        ua, va = cell_to_uv(a)
        ub, vb = cell_to_uv(b)
        return ua == ub or va == vb

    def _pick_attraction_target(self, me_cell, my_ap):
        """Nearest alive enemy that Attraction can pull, or None.

        Attraction (slot 1, 3 AP) is line-only -- the target must share
        an iso axis with us -- and needs LoS. Range [ATTRACTION_MIN_RANGE
        .. ATTRACTION_MAX_RANGE]. Adjacent enemies are skipped: pulling
        them is a no-op (they're already in melee, where Dissolution
        wants them). LoS blockers = static obstacles + all other live
        entities."""
        if not me_cell or my_ap < ATTRACTION_AP_COST:
            return None
        snap = self.state.snapshot()
        other_alive = {
            e.cell for e in snap.fight_entities.values()
            if e.alive and e.cell > 0 and e.id != snap.my_id
        }
        candidates = []
        for e in alive_enemies(snap):
            d = cell_distance(me_cell, e.cell)
            if d < max(2, ATTRACTION_MIN_RANGE) or d > ATTRACTION_MAX_RANGE:
                continue
            if not self._same_iso_line(me_cell, e.cell):
                continue
            blockers = set(self.static_obstacles) | (other_alive - {e.cell})
            if not line_of_sight(me_cell, e.cell, blockers):
                continue
            candidates.append((d, e))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        target = candidates[0][1]
        print(f"  attraction: target id={target.id} cell={target.cell} "
              f"dist={candidates[0][0]} (line-aligned, LoS clear)")
        return target

    # --- Swap targeting ---

    def _pick_swap_target(self, me_cell, my_ap):
        """Returns the enemy to swap with, or None.

        Swap is worth casting iff:
          - my_ap >= SWAP_MIN_AP (otherwise we won't have AP for the
            follow-up Dissolution)
          - exactly one alive enemy is adjacent to me (2+ means
            Dissolution already hits multiple targets, no swap needed)
          - that adjacent enemy has at least one OTHER alive enemy
            adjacent to its own cell -- after swap we'll land in the
            target's old cell and Dissolution will hit BOTH the
            swap target (now in our old cell) and that other enemy."""
        if not me_cell or my_ap < SWAP_MIN_AP:
            return None
        snap = self.state.snapshot()
        # alive_enemies hides summons when real mobs are still up; using
        # it here means we won't swap *with* a summon. Summons next to
        # the swap target can still serve as Dissolution collateral, so
        # the "nearby" check below stays broad over fight_entities.
        adjacent = [
            e for e in alive_enemies(snap)
            if cell_distance(me_cell, e.cell) == 1
        ]
        if len(adjacent) != 1:
            print(f"  swap: skip (adjacent_enemies={len(adjacent)}, "
                  f"need exactly 1)")
            return None
        target = adjacent[0]
        nearby = [
            e for e in snap.fight_entities.values()
            if e.alive and e.id != snap.my_id and e.id != target.id
            and e.cell > 0
            and cell_distance(target.cell, e.cell) == 1
        ]
        if not nearby:
            print(f"  swap: skip (id={target.id} cell={target.cell} has "
                  f"no other enemy adjacent to it -- swap wouldn't gain "
                  f"a second Dissolution target)")
            return None
        print(f"  swap: 1 adjacent enemy id={target.id} cell={target.cell} "
              f"and it has {len(nearby)} other enemy/ies adjacent "
              f"(cells={[e.cell for e in nearby]}); casting swap "
              f"(my_ap={my_ap} >= {SWAP_MIN_AP})")
        return target

    def _me_cell_after_swap(self, old_cell, target_cell):
        """Resolve our cell after a swap cast. The proxy parses GA;4;
        (Transposition) and updates my_cell, but the packet arrives a
        few tens of ms after the click ack -- poll briefly for the
        snapshot to reflect it. Fall back to target_cell (geometrically
        exact: we land where the swap target was) if the wait times out."""
        wait_for(self.state,
                 lambda s: my_fight_cell(s) not in (None, old_cell),
                 timeout=1.0)
        snap_cell = my_fight_cell(self.state.snapshot())
        if snap_cell and snap_cell != old_cell:
            return snap_cell
        print(f"  swap: proxy didn't surface new my_cell within 1.0s; "
              f"predicting target_cell={target_cell}")
        return target_cell

    # --- Obstacle hygiene ---

    def _prune_obstacles_from_entities(self, map_id, snap):
        """Drop obstacle cells that overlap live entities or saved
        start cells. Calibration sometimes mis-clicks mob spawn cells
        as obstacles; this prunes them when we observe a fight there."""
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
