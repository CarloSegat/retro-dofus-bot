"""Orchestrator: top-level state machine that owns every other class.

Reads runtime settings, wires the callback graph, then runs the main
loop. The loop's job is to look at the current proxy snapshot and
delegate to the right class:

  in_combat       -> Combat.run() (the turn loop fires our callbacks)
  in_placement    -> ready up + place starting cells + pass turn
  idle, mob found -> Engager.try_engage
  idle, no mob    -> MapNavigator.travel

All class-to-class subscriptions happen in __init__. To understand who
reacts to what, you read that block.
"""
import os
import sys
import time
from types import SimpleNamespace

import mss

from dofus.actions import pass_turn
from dofus.inventory import Inventory
from dofus.map_data import (
    build_world_index,
    get_farming_area,
    list_farming_areas,
    load_all as load_map_data,
)
from dofus.proxy_client import ProxyState
from fighter.combat import Combat, TurnContext  # noqa: F401 (re-exported)
from fighter.death_watcher import DeathWatcher
from fighter.engager import Engager, COMBAT_START_TIMEOUT
from fighter.helpers import (
    IDLE_POLL_SEC,
    PROXY_ADDR,
    load_cal,
    make_exchange_dismiss_callback,
    prompt_int,
    prompt_yn,
    wait_for,
)
from fighter.enutrof import (
    COINS_AP_COST,
    COINS_HOTKEY,
    Enutrof,
)
from fighter.navigator import MapNavigator
from fighter.regen import HpRegen
from fighter.sacrieur import (
    DISSOLUTION_AP_COST,
    DISSOLUTION_HOTKEY,
    PASS_TURN_HOTKEY,
    PASS_TURN_PRE_DELAY_SEC,
    Sacrieur,
)
# fighter.weapon.Weapon is intentionally not wired anymore -- the bow has
# been replaced by Attraction (line-only ranged pull). The Weapon class
# is kept on disk for potential future re-use.
from utils import CFG, make_ctx


CLASS_SACRIEUR = "sacrieur"
CLASS_ENUTROF = "enutrof"


def prompt_character_class():
    """Ask which character profile to play. Defaults to Sacrieur."""
    default = (CFG.get("character_class") or CLASS_SACRIEUR).lower()
    while True:
        raw = input(f"  character class [sacrieur/enutrof, default {default}]: ").strip().lower()
        if not raw:
            return default
        if raw in (CLASS_SACRIEUR, "s", "sacri", "sac"):
            return CLASS_SACRIEUR
        if raw in (CLASS_ENUTROF, "e", "enu"):
            return CLASS_ENUTROF
        print(f"  unknown class {raw!r}; type 'sacrieur' or 'enutrof'")


def prompt_farming_area():
    """Ask which farming area to scope navigation to. Returns the area
    dict (with area_id, name, map_ids) or None for free-roam.

    No areas in the DB -> silently returns None (free-roam) so a fresh
    install still works."""
    areas = list_farming_areas()
    if not areas:
        return None
    print("  available farming areas:")
    for i, a in enumerate(areas, start=1):
        print(f"    {i}. {a['name']!r} ({a['map_count']} map(s))")
    print(f"    0. (free-roam -- no area constraint)")
    while True:
        raw = input(f"  pick farming area [0..{len(areas)}, default 0]: ").strip()
        if not raw:
            return None
        try:
            choice = int(raw)
        except ValueError:
            print(f"  not a number")
            continue
        if choice == 0:
            return None
        if 1 <= choice <= len(areas):
            full = get_farming_area(areas[choice - 1]["area_id"])
            return full
        print(f"  out of range (0..{len(areas)})")


def prompt_runtime_settings():
    """Interactive startup questions. Returns SimpleNamespace with
    character_class, farming_area, buff_enabled (Sacrieur only),
    max_group_size, min_hp."""
    print("[fighter] runtime settings (Enter for default):")
    character_class = prompt_character_class()
    farming_area = prompt_farming_area()
    if character_class == CLASS_SACRIEUR:
        buff_enabled = prompt_yn("  cast Bold Punishment buff?",
                                 default=bool(CFG.get("sacrid_buff_enabled", True)))
    else:
        buff_enabled = False  # unused for non-Sacrieur classes
    max_group_size = prompt_int(
        "  max mob group size to engage (0 = no cap)",
        default=int(CFG.get("max_mob_group_size", 8)))
    min_hp = prompt_int(
        "  min HP before engaging",
        default=int(CFG.get("min_hp_to_engage", 500)))
    return SimpleNamespace(
        character_class=character_class,
        farming_area=farming_area,
        buff_enabled=buff_enabled,
        max_group_size=max_group_size,
        min_hp=min_hp,
    )


class Orchestrator:
    """Owns ProxyState and every other fighter class. Wires callbacks
    in __init__; run() is the state machine loop."""

    def __init__(self):
        self.args = prompt_runtime_settings()
        if self.args.character_class == CLASS_SACRIEUR and not DISSOLUTION_HOTKEY:
            print("config.json is missing 'sacrid_dissolution_hotkey'. Set it to "
                  "the key Dissolution is bound to (e.g. \"2\").")
            sys.exit(1)
        if self.args.character_class == CLASS_ENUTROF and not COINS_HOTKEY:
            print("config.json is missing 'enutrof_coins_hotkey'. Set it to "
                  "the key Coins Throwing is bound to (e.g. \"1\").")
            sys.exit(1)
        self.cal = load_cal()
        self.map_data = load_map_data()
        self.map_by_world = build_world_index(self.map_data)
        self._print_startup_banner()

        self.state = ProxyState(PROXY_ADDR)
        self.state.on_event(make_exchange_dismiss_callback())
        self.state.start()
        print(f"[fighter] connecting to proxy at {PROXY_ADDR}...")
        if not wait_for(self.state, lambda s: s.connected and s.my_id != 0, 10.0):
            snap = self.state.snapshot()
            print(f"[fighter] proxy not ready: connected={snap.connected} "
                  f"my_id={snap.my_id}")
            sys.exit(1)
        snap = self.state.snapshot()
        print(f"[fighter] ready: my_id={snap.my_id} my_cell={snap.my_cell} "
              f"map={snap.map_id}")

        self._sct = mss.mss(backend=os.environ.get("MSS_BACKEND", "default"))
        self._ctx = make_ctx(self._sct)

        # --- Construct the classes ---
        if self.args.character_class == CLASS_SACRIEUR:
            self.fighter = Sacrieur(self.state, self.cal, self.map_data,
                                    buff_enabled=self.args.buff_enabled)
        else:
            self.fighter = Enutrof(self.state, self.cal, self.map_data)
        self.hp_regen = HpRegen(self.state, min_hp=self.args.min_hp)
        self.inventory = Inventory(self.state)
        self.engager = Engager(self.state, self.cal, self.hp_regen,
                               self.map_data, self.inventory,
                               max_group_size=self.args.max_group_size)
        area_map_ids = (self.args.farming_area or {}).get("map_ids") \
            if self.args.farming_area else None
        self.navigator = MapNavigator(self.state, self.cal, self.map_data,
                                      self.map_by_world, self.engager,
                                      farming_area_map_ids=area_map_ids)
        if area_map_ids is not None and snap.map_id not in area_map_ids:
            print(f"[fighter] WARNING: starting map_id={snap.map_id} is NOT "
                  f"in farming area {self.args.farming_area['name']!r}; the "
                  f"bot may be unable to navigate until you walk back in")
        self.combat = Combat(self.state, self._ctx)
        self.death_watcher = DeathWatcher(
            self.state, self.navigator, on_aggro=self.combat.run,
            farming_area_map_ids=area_map_ids,
        )
        self.state.on_event(self.death_watcher.on_proxy_event)

        # --- Wire callbacks (single source of truth) ---
        self.combat.on_fight_engaged(self.fighter.on_fight_engaged)
        self.combat.on_fight_engaged(self.hp_regen.on_fight_engaged)
        self.combat.on_turn_start(self.fighter.play_turn)
        self.combat.on_turn_start(self._log_turn)
        self.combat.on_turn_end(self._log_turn_end)
        self.combat.on_fight_ended(self.inventory.on_fight_ended)
        self.combat.on_fight_ended(self.death_watcher.on_fight_ended)
        self.combat.on_fight_ended(self._on_fight_ended)

        # Orchestrator-level state
        self.placed_for_engage_ts = 0.0
        self.last_map_id = snap.map_id

    def _print_startup_banner(self):
        print(f"[fighter] cal: origin=({self.cal['origin_x']:.1f},"
              f"{self.cal['origin_y']:.1f}) "
              f"cell={self.cal['cell_w']:.2f}x{self.cal['cell_h']:.2f}")
        print(f"[fighter] character_class={self.args.character_class}")
        if self.args.character_class == CLASS_SACRIEUR:
            print(f"[fighter] dissolution_hotkey={DISSOLUTION_HOTKEY!r} "
                  f"ap_cost={DISSOLUTION_AP_COST}")
            print(f"[fighter] buff: {'enabled' if self.args.buff_enabled else 'DISABLED'}")
        else:
            print(f"[fighter] coins_hotkey={COINS_HOTKEY!r} "
                  f"ap_cost={COINS_AP_COST}")
        print(f"[fighter] min-hp threshold: wait until >= {self.args.min_hp} HP "
              f"before engaging")
        if self.args.farming_area is not None:
            fa = self.args.farming_area
            print(f"[fighter] farming area: {fa['name']!r} "
                  f"({len(fa['map_ids'])} map(s)) -- navigation constrained")
        else:
            print(f"[fighter] farming area: (free-roam, no constraint)")
        if self.args.max_group_size > 0:
            print(f"[fighter] max-group-size: skip mob groups with > "
                  f"{self.args.max_group_size} members")
        else:
            print(f"[fighter] max-group-size: no cap (engaging any group size)")
        from dofus.map_data import safe_directions
        with_switches = sum(1 for d in self.map_data.values() if d.get("switch_cells"))
        with_safe = sum(1 for d in self.map_data.values()
                        if safe_directions(d, self.map_by_world))
        print(f"[fighter] navigation: {len(self.map_data)} map(s) calibrated, "
              f"{with_switches} have switch_cells, "
              f"{with_safe} have >= 1 return-safe neighbour")

    # --- Combat callbacks owned by Orchestrator ---

    def _log_turn(self, ctx):
        me = ctx.snap.fight_entities.get(ctx.snap.my_id)
        ap = me.ap if me else 0
        mp = me.mp if me else 0
        print(f"[orchestrator] turn {ctx.turn_n} (ap={ap} mp={mp})")

    def _log_turn_end(self, ctx):
        print(f"[orchestrator] turn {ctx.turn_n} end")

    def _on_fight_ended(self, snap):
        print(f"[orchestrator] fight ended; map={snap.map_id}")

    # --- Main loop ---

    def run(self):
        try:
            while True:
                snap = self.state.snapshot()
                if snap.map_id != self.last_map_id:
                    self.navigator.on_map_changed(self.last_map_id, snap.map_id)
                    self.last_map_id = snap.map_id
                    snap = self.state.snapshot()

                if snap.in_combat:
                    self._tick_combat(snap)
                    continue

                if snap.in_placement:
                    self._tick_placement(snap)
                    continue

                self._tick_idle(snap)
        finally:
            self._sct.close()

    def _tick_combat(self, snap):
        ents = snap.fight_entities
        others = [e for e in ents.values() if e.id != snap.my_id]
        print(f"[fighter] phase=combat (map={snap.map_id}) "
              f"entities={len(ents)} enemies={len(others)}, running sacrid combat")
        self.combat.run()

    def _tick_placement(self, snap):
        print(f"[fighter] phase=placement (map={snap.map_id}); readying up")
        time.sleep(CFG["fight_ready_wait_sec"])
        if snap.last_fight_engage_ts != self.placed_for_engage_ts:
            self.engager.place_starting_cells(snap)
            self.placed_for_engage_ts = snap.last_fight_engage_ts
        pass_turn(PASS_TURN_HOTKEY, PASS_TURN_PRE_DELAY_SEC)
        if not wait_for(self.state,
                        lambda s: s.in_combat or not s.in_fight,
                        COMBAT_START_TIMEOUT):
            print(f"[fighter] still in placement after {COMBAT_START_TIMEOUT}s; "
                  f"will press ready again next iteration")

    def _tick_idle(self, snap):
        if self.death_watcher.try_recover():
            return
        target = self.engager.find_target(snap)
        if target is None:
            wait_ms = self.engager.all_walking_groups_filtered(snap)
            if wait_ms is not None:
                time.sleep(min(wait_ms / 1000.0 + 0.05, IDLE_POLL_SEC))
                return
            self.navigator.travel(snap, self.args.max_group_size)
            return
        # Got a target -- map isn't empty, clear cooldown then engage.
        self.navigator.clear_empty_flag(snap.map_id)
        self.engager.try_engage(target)
