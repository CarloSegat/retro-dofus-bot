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
import json
import os
import sys
import time
from pathlib import Path
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
from fighter.logging_setup import instance_log_dir
from fighter.watchdog import ProgressWatchdog, StaleClientError
from fighter.helpers import (
    IDLE_POLL_SEC,
    PROXY_ADDR,
    load_cal,
    make_exchange_dismiss_callback,
    make_npc_dialog_dismiss_callback,
    prompt_int,
    prompt_yn,
    resolve_screen_name,
    wait_for,
)
from fighter.ecaflip import (
    HEADS_AP_COST,
    HEADS_HOTKEY,
    Ecaflip,
)
from fighter.enutrof import (
    COINS_AP_COST,
    COINS_HOTKEY,
    Enutrof,
)
from fighter.navigator import MapNavigator
from fighter.regen import HpRegen
from mouse_keyboard import click_at_focused
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
CLASS_ECAFLIP = "ecaflip"

# Where we persist the last successful prompt cycle so the next run can
# offer a one-keystroke "reuse" path.
#
# Stored under the user's XDG config dir, NOT in the repo root. Each
# docker desktop container has its own /home/bot (the host has its own
# /home/car), so this naturally isolates per-container -- the repo itself
# is bind-mounted at /workspace and shared, which would otherwise let one
# container clobber another's "last run" between bot starts.
_LAST_RUN_PATH = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
    / "auto-fighter"
    / "last_run.json"
)


def _load_last_run():
    try:
        with open(_LAST_RUN_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _save_last_run(args):
    data = {
        "screen_name": args.screen_name,
        "character_class": args.character_class,
        "farming_area_id": (args.farming_area or {}).get("area_id")
            if args.farming_area else None,
        "buff_enabled": args.buff_enabled,
        "aon_enabled": args.aon_enabled,
        "max_group_size": args.max_group_size,
        "min_hp": args.min_hp,
    }
    try:
        _LAST_RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
        _LAST_RUN_PATH.write_text(json.dumps(data, indent=2))
    except OSError as e:
        print(f"  warning: could not save last-run settings to "
              f"{_LAST_RUN_PATH}: {e}")


def _summarize_last_run(data):
    area_id = data.get("farming_area_id")
    if area_id:
        area = get_farming_area(area_id)
        farming = (f"{area['name']!r}" if area
                   else f"area_id={area_id} (NOT FOUND -- will fall back to free-roam)")
    else:
        farming = "free-roam"
    return "\n".join([
        f"    screen={data.get('screen_name')!r}",
        f"    class={data.get('character_class')!r}",
        f"    farming_area={farming}",
        f"    buff_enabled={bool(data.get('buff_enabled'))} "
        f"aon_enabled={bool(data.get('aon_enabled'))}",
        f"    max_group_size={data.get('max_group_size')} "
        f"min_hp={data.get('min_hp')}",
    ])


def _reuse_last_run(data, screen_override=None):
    """Materialize the saved dict into the SimpleNamespace
    prompt_runtime_settings returns. Re-fetches the farming area by id
    (the DB may have changed; missing -> free-roam). `screen_override`
    is the explicit --screen value if the caller passed one -- it wins
    over the saved screen so `python3 main.py --screen X` is honored
    even when reusing."""
    farming_area = None
    area_id = data.get("farming_area_id")
    if area_id:
        farming_area = get_farming_area(area_id)
        if not farming_area:
            print(f"  saved farming area_id={area_id} no longer exists; "
                  f"running free-roam")
    screen_name = data.get("screen_name")
    if screen_override:
        screen_name = resolve_screen_name(screen_override)
    return SimpleNamespace(
        screen_name=screen_name,
        character_class=data.get("character_class"),
        farming_area=farming_area,
        buff_enabled=bool(data.get("buff_enabled")),
        aon_enabled=bool(data.get("aon_enabled")),
        max_group_size=int(data.get("max_group_size", 8)),
        min_hp=int(data.get("min_hp", 500)),
    )


def prompt_screen_name(default):
    """Ask which calibration in config.json[cell_calibrations] to use.
    `default` is the pre-resolved name (--screen -> env -> config).
    Always prompts so the active screen is visibly confirmed, even
    when only one is configured (catches the case where the resolved
    default doesn't match what's actually calibrated)."""
    cals = CFG.get("cell_calibrations") or {}
    known = sorted(cals.keys())
    if not known:
        # load_cal will exit with a clearer message; just return whatever.
        return default
    opts = "/".join(known)
    if default in known:
        default_label = f"default {default}"
    elif default:
        default_label = f"no fit for {default!r} -- pick one"
    else:
        default_label = "no default"
    while True:
        raw = input(f"  screen [{opts}, {default_label}]: ").strip()
        if not raw:
            if default in known:
                return default
            print(f"  no usable default; pick one of {known}")
            continue
        if raw in known:
            return raw
        print(f"  unknown screen {raw!r}; pick one of {known}")


def prompt_character_class():
    """Ask which character profile to play. Defaults to Sacrieur."""
    default = (CFG.get("character_class") or CLASS_SACRIEUR).lower()
    while True:
        raw = input(f"  character class [sacrieur/enutrof/ecaflip, default {default}]: ").strip().lower()
        if not raw:
            return default
        if raw in (CLASS_SACRIEUR, "s", "sacri", "sac"):
            return CLASS_SACRIEUR
        if raw in (CLASS_ENUTROF, "e", "enu"):
            return CLASS_ENUTROF
        if raw in (CLASS_ECAFLIP, "ec", "eca"):
            return CLASS_ECAFLIP
        print(f"  unknown class {raw!r}; type 'sacrieur', 'enutrof', or 'ecaflip'")


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


def prompt_runtime_settings(screen_name=None, auto_reuse=False):
    """Interactive startup questions. Returns SimpleNamespace with
    screen_name, character_class, farming_area, buff_enabled (Sacrieur
    only), max_group_size, min_hp. `screen_name` is the value from
    --screen (or None) used as the default for the screen prompt.

    If a previous run's settings exist on disk (`.last_run.json`),
    summarize them and offer a Y/n shortcut to reuse them verbatim,
    skipping every other prompt.

    `auto_reuse=True` (from `main.py --auto-reuse`) skips every prompt
    -- including the Y/n -- and returns the last saved run verbatim.
    Used by the orchestrator's in-bot restart path where no operator
    is at the terminal. Falls through to interactive mode with a
    warning if no saved settings exist."""
    print("[fighter] runtime settings (Enter for default):")
    last = _load_last_run()
    if auto_reuse:
        if last:
            print("  --auto-reuse: reusing previous settings without prompting")
            print(_summarize_last_run(last))
            return _reuse_last_run(last, screen_override=screen_name)
        print("  --auto-reuse: no saved settings on disk; falling back to "
              "interactive prompts")
    if last:
        print("  previous run:")
        print(_summarize_last_run(last))
        if prompt_yn("  reuse previous settings?", default=True):
            return _reuse_last_run(last, screen_override=screen_name)
    screen_name = prompt_screen_name(resolve_screen_name(screen_name))
    character_class = prompt_character_class()
    farming_area = prompt_farming_area()
    if character_class == CLASS_SACRIEUR:
        buff_enabled = prompt_yn("  cast Bold Punishment buff?",
                                 default=bool(CFG.get("sacrid_buff_enabled", True)))
    else:
        buff_enabled = False  # unused for non-Sacrieur classes

    if character_class == CLASS_ECAFLIP:
        aon_enabled = prompt_yn("  cast All or Nothing?",
                                default=bool(CFG.get("ecaflip_aon_enabled", True)))
    else:
        aon_enabled = False  # unused for non-Ecaflip classes

    max_group_size = prompt_int(
        "  max mob group size to engage (0 = no cap)",
        default=int(CFG.get("max_mob_group_size", 8)))
    min_hp = prompt_int(
        "  min HP before engaging",
        default=int(CFG.get("min_hp_to_engage", 500)))
    args = SimpleNamespace(
        screen_name=screen_name,
        character_class=character_class,
        farming_area=farming_area,
        buff_enabled=buff_enabled,
        aon_enabled=aon_enabled,
        max_group_size=max_group_size,
        min_hp=min_hp,
    )
    _save_last_run(args)
    return args


# Loop-rate policy for the in-bot auto-restart path. Tuned for the
# "operator gone overnight" worst case: a structural failure (bad
# calibration / banned account / dead launcher) would otherwise
# silently trip the watchdog hundreds of times before someone
# noticed.
LOOP_WINDOW_SEC = 3600          # rolling 1h window
LOOP_WARN_TRIPS = 3             # >= 3 trips in 1h -> loud warning
LOOP_HARDSTOP_TRIPS = 10        # >= 10 trips in 1h -> stop restarting
LOOP_HARDSTOP_SLEEP_SEC = 60    # quiet beat before the non-zero exit
                                # so a `docker logs --tail` shows the
                                # marker on the same screen.


class Orchestrator:
    """Owns ProxyState and every other fighter class. Wires callbacks
    in __init__; run() is the state machine loop."""

    def __init__(self, screen_name=None, auto_reuse=False):
        self.auto_reuse = auto_reuse
        if auto_reuse:
            # Loud marker so a `grep auto-restart logs/.../fighter.log*`
            # walks straight to the post-restart child(ren). Pairs with
            # the "[watchdog] === restart_everything BEGIN ===" block
            # in the *previous* pid's tail.
            print(f"[session] auto-restart child (pid={os.getpid()}): "
                  f"this process was started by `main.py --auto-reuse`, "
                  f"almost certainly because the previous pid tripped "
                  f"the ProgressWatchdog. See `restart_history.jsonl` "
                  f"in the same logs/<instance>/ dir for the trip "
                  f"reason and the prior pid's tail for the snapshot.")
            # Immediate triage view: the operator SSH'ing in to a long-
            # running instance should see thrash patterns in the first
            # screen of log output instead of having to read JSONL by
            # hand.
            self._print_restart_history_summary()
        self.args = prompt_runtime_settings(screen_name, auto_reuse=auto_reuse)
        if self.args.character_class == CLASS_SACRIEUR and not DISSOLUTION_HOTKEY:
            print("config.json is missing 'sacrid_dissolution_hotkey'. Set it to "
                  "the key Dissolution is bound to (e.g. \"2\").")
            sys.exit(1)
        if self.args.character_class == CLASS_ENUTROF and not COINS_HOTKEY:
            print("config.json is missing 'enutrof_coins_hotkey'. Set it to "
                  "the key Coins Throwing is bound to (e.g. \"1\").")
            sys.exit(1)
        if self.args.character_class == CLASS_ECAFLIP and not HEADS_HOTKEY:
            print("config.json is missing 'ecaflip_heads_or_tails_hotkey'. Set it to "
                  "the key Heads or Tails is bound to (e.g. \"3\").")
            sys.exit(1)
        self.cal = load_cal(self.args.screen_name)
        self.map_data = load_map_data()
        self.map_by_world = build_world_index(self.map_data)
        self._print_startup_banner()

        self.state = ProxyState(PROXY_ADDR)
        self.state.on_event(make_exchange_dismiss_callback())
        self.state.on_event(make_npc_dialog_dismiss_callback())
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

        self._sct = mss.mss()
        self._ctx = make_ctx(self._sct)

        # --- Construct the classes ---
        if self.args.character_class == CLASS_SACRIEUR:
            self.fighter = Sacrieur(self.state, self.cal, self.map_data,
                                    buff_enabled=self.args.buff_enabled)
        elif self.args.character_class == CLASS_ECAFLIP:
            self.fighter = Ecaflip(self.state, self.cal, self.map_data,
                                   aon_enabled=self.args.aon_enabled)
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
        self.combat.on_fight_engaged(self.death_watcher.on_fight_engaged)
        self.combat.on_fight_engaged(self._dismiss_fight_ui)
        self.combat.on_turn_start(self.fighter.play_turn)
        self.combat.on_turn_start(self._log_turn)
        self.combat.on_turn_end(self._log_turn_end)
        self.combat.on_fight_ended(self.inventory.on_fight_ended)
        self.combat.on_fight_ended(self.death_watcher.on_fight_ended)
        self.combat.on_fight_ended(self._on_fight_ended)

        # Orchestrator-level state
        self.placed_for_engage_ts = 0.0
        self.last_map_id = snap.map_id

        # No-progress watchdog. Idle threshold (default 300s, override
        # `watchdog_idle_no_progress_sec`) trips when the snapshot's
        # progress signature stays constant for too long. In-fight
        # stalls go through the same `_restart_everything` path via
        # Combat raising StaleClientError.
        self.watchdog = ProgressWatchdog(
            threshold_sec=float(CFG.get("watchdog_idle_no_progress_sec", 300)),
            on_stuck=self._restart_everything,
        )

    def _print_startup_banner(self):
        print(f"[fighter] cal[{self.args.screen_name}]: "
              f"origin=({self.cal['origin_x']:.1f},"
              f"{self.cal['origin_y']:.1f}) "
              f"cell={self.cal['cell_w']:.2f}x{self.cal['cell_h']:.2f}")
        print(f"[fighter] character_class={self.args.character_class}")
        if self.args.character_class == CLASS_SACRIEUR:
            print(f"[fighter] dissolution_hotkey={DISSOLUTION_HOTKEY!r} "
                  f"ap_cost={DISSOLUTION_AP_COST}")
            print(f"[fighter] buff: {'enabled' if self.args.buff_enabled else 'DISABLED'}")
        elif self.args.character_class == CLASS_ECAFLIP:
            print(f"[fighter] heads_or_tails_hotkey={HEADS_HOTKEY!r} "
                  f"ap_cost={HEADS_AP_COST}")
            print(f"[fighter] all-or-nothing: "
                  f"{'enabled' if self.args.aon_enabled else 'DISABLED'}")
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

    def _dismiss_fight_ui(self, snap):
        """Collapse the top-right turn-order bar via its `<>` toggle so
        its UI doesn't overlap clickable cells. Pixel position is per-
        screen; missing calibration -> one-shot warning + no-op."""
        screen = self.args.screen_name
        cals = CFG.get("fight_ui_dismiss_clicks", {}).get(screen)
        if not cals or "collapse_turn_order" not in cals:
            if not getattr(self, "_fight_ui_warn_logged", False):
                print(f"[fight-ui] no calibrated <> position for "
                      f"screen={screen!r}; turn-order bar will not be "
                      f"collapsed. Run "
                      f"`python3 calibrate_fight_ui_dismiss.py {screen}` "
                      f"during a fight to capture it.")
                self._fight_ui_warn_logged = True
            return
        x, y = cals["collapse_turn_order"]
        time.sleep(0.5)
        print(f"[fight-ui] collapsing turn-order bar via click ({x},{y})")
        click_at_focused(x, y)

    # --- Main loop ---

    def run(self):
        try:
            while True:
                snap = self.state.snapshot()
                if snap.map_id != self.last_map_id:
                    self.navigator.on_map_changed(self.last_map_id, snap.map_id)
                    self.last_map_id = snap.map_id
                    snap = self.state.snapshot()

                self.watchdog.check(snap)

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
        try:
            self.combat.run()
        except StaleClientError as e:
            self._restart_everything(f"combat stale-turn: {e}")

    def _restart_everything(self, reason):
        """Watchdog/stale-turn callback: kill + relaunch Dofus, then
        re-exec self via main.py --auto-reuse. Never returns under
        normal flow (execv replaces the process).

        Logs are the post-mortem story for the operator. Each step
        prints a labeled `[watchdog]` line so a 4-hour-later grep
        on `restart_everything` walks the timeline end to end. Also
        appends one JSONL row to `logs/<instance>/restart_history.jsonl`
        so the full history survives log rotation (24h chunks otherwise
        roll the evidence off).
        """
        snap = self.state.snapshot()
        print(f"[watchdog] === restart_everything BEGIN ===")
        print(f"[watchdog] reason: {reason}")
        self._log_trip_snapshot(snap)
        self._append_restart_history(reason, snap)

        try:
            self.state.stop()
        except Exception as e:
            print(f"[watchdog] proxy stop failed (continuing): {e}")
        try:
            self._sct.close()
        except Exception:
            pass

        from scripts.restart_dofus import restart_dofus_client
        try:
            ok = restart_dofus_client(self.args.screen_name)
            if not ok:
                print("[watchdog] restart_dofus_client returned False; "
                      "re-execing anyway -- fresh process may recover via "
                      "proxy reconnect")
        except SystemExit:
            raise
        except Exception as e:
            print(f"[watchdog] restart_dofus_client raised {e!r}; "
                  f"re-execing anyway")

        argv = [sys.executable, sys.argv[0], "--auto-reuse"]
        # Only forward --screen if the operator passed it explicitly;
        # otherwise let the saved last_run / config default decide.
        if "--screen" in sys.argv and self.args.screen_name:
            argv += ["--screen", self.args.screen_name]
        print(f"[watchdog] handing off to execv (this pid={os.getpid()} dies "
              f"now); next argv={argv}")
        print(f"[watchdog] === restart_everything END ===")
        os.execv(sys.executable, argv)

    def _log_trip_snapshot(self, snap):
        """Dump every field useful for "what was the bot looking at
        when it gave up". Single multi-line block so grep -A picks it
        up as one chunk."""
        me = (snap.fight_entities or {}).get(snap.my_id)
        now = time.time()
        engage_age = (now - snap.last_fight_engage_ts) \
            if snap.last_fight_engage_ts > 0 else None
        end_age = (now - snap.last_fight_end_ts) \
            if snap.last_fight_end_ts > 0 else None
        event_age = (now - snap.last_event_ts) \
            if snap.last_event_ts > 0 else None
        print(f"[watchdog]   connected={snap.connected} "
              f"my_id={snap.my_id} my_cell={snap.my_cell} "
              f"map_id={snap.map_id} fight_phase={snap.fight_phase!r}")
        print(f"[watchdog]   hp={snap.my_life}/{snap.my_life_max} "
              f"est_hp={snap.estimated_life()} "
              f"sitting={snap.sitting} "
              f"pods={snap.pods}/{snap.pods_max}({snap.pods_max_overweight})")
        print(f"[watchdog]   last_event={('%.0fs ago' % event_age) if event_age is not None else 'never'} "
              f"last_engage={('%.0fs ago' % engage_age) if engage_age is not None else 'never'} "
              f"last_fight_end={('%.0fs ago' % end_age) if end_age is not None else 'never'}")
        print(f"[watchdog]   turn_actor={snap.turn_actor} "
              f"turn_number={snap.turn_number} "
              f"fight_entities={len(snap.fight_entities or {})} "
              f"mobs_on_map={len(snap.mobs or {})}")
        if me is not None:
            print(f"[watchdog]   me-in-fight: cell={me.cell} ap={me.ap} "
                  f"mp={me.mp} alive={me.alive}")
        # Surface engager's "this group is filtered, retry in Nms" state
        # so we can tell if we were idling because every visible group
        # was disqualified (HP, pods, max_group_size).
        try:
            wait_ms = self.engager.all_walking_groups_filtered(snap)
        except Exception:
            wait_ms = None
        if wait_ms is not None:
            print(f"[watchdog]   engager: all visible mob groups filtered "
                  f"(retry in {wait_ms}ms)")

    def _append_restart_history(self, reason, snap):
        """One JSONL row per trip. Survives log rotation (rotated chunks
        only keep 24h). Tail this file weeks later to count trips,
        spot patterns, and correlate with map/character.

        After append, applies the loop-rate policy:
          - >= LOOP_WARN_TRIPS in last LOOP_WINDOW_SEC: loud warning
          - >= LOOP_HARDSTOP_TRIPS in last LOOP_WINDOW_SEC: refuse to
            execv again, exit non-zero so the operator notices. Trying
            to restart faster than every ~6 min for an hour means the
            issue is structural (bad calibration, perma-disconnect,
            account banned) and more restarts just burn the launcher.
        """
        try:
            row = {
                "ts": time.time(),
                "iso": time.strftime("%Y-%m-%d %H:%M:%S"),
                "pid": os.getpid(),
                "reason": reason,
                "character": os.environ.get("FIGHTER_CHARACTER") or "?",
                "instance": os.environ.get("FIGHTER_INSTANCE") or "?",
                "screen": self.args.screen_name,
                "map_id": snap.map_id,
                "my_id": snap.my_id,
                "my_cell": snap.my_cell,
                "fight_phase": snap.fight_phase,
                "my_life": snap.my_life,
                "my_life_max": snap.my_life_max,
                "estimated_life": snap.estimated_life(),
                "last_engage_ts": snap.last_fight_engage_ts,
                "last_fight_end_ts": snap.last_fight_end_ts,
                "last_event_ts": snap.last_event_ts,
            }
            path = instance_log_dir() / "restart_history.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a") as f:
                f.write(json.dumps(row) + "\n")
            print(f"[watchdog] appended restart row to {path}")
        except Exception as e:
            # Logging-best-effort; don't block the restart on a disk hiccup.
            print(f"[watchdog] could not write restart_history.jsonl: {e}")
            return

        recent = self._recent_restart_trips(LOOP_WINDOW_SEC)
        if not recent:
            return
        if len(recent) >= LOOP_HARDSTOP_TRIPS:
            print(f"[watchdog] !!! RUNAWAY RESTART LOOP: "
                  f"{len(recent)} trips in last {LOOP_WINDOW_SEC // 60} min "
                  f"(threshold {LOOP_HARDSTOP_TRIPS}); refusing to "
                  f"execv yet again. Sleeping {LOOP_HARDSTOP_SLEEP_SEC}s "
                  f"and exiting non-zero -- operator must investigate "
                  f"(check calibration, account status, anti-cheat ban).")
            time.sleep(LOOP_HARDSTOP_SLEEP_SEC)
            sys.exit(99)
        if len(recent) >= LOOP_WARN_TRIPS:
            iso_list = ", ".join(r.get("iso", "?") for r in recent[-LOOP_WARN_TRIPS:])
            print(f"[watchdog] !! restart loop suspected: "
                  f"{len(recent)} trips in last {LOOP_WINDOW_SEC // 60} min "
                  f"(>= warn threshold {LOOP_WARN_TRIPS}); recent ISO "
                  f"timestamps: {iso_list}")

    @staticmethod
    def _read_restart_history(limit=None):
        """Read the per-instance restart_history.jsonl tail. Returns a
        list of dicts (oldest first). `limit` keeps only the last N
        rows. Tolerates partial/corrupt lines (skips them)."""
        path = instance_log_dir() / "restart_history.jsonl"
        if not path.exists():
            return []
        rows = []
        try:
            with path.open() as f:
                lines = f.readlines()
        except OSError:
            return []
        if limit is not None:
            lines = lines[-limit:]
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    def _recent_restart_trips(self, window_sec):
        """Restart rows whose ts falls within the last `window_sec`
        seconds. Used for loop-rate detection."""
        now = time.time()
        return [r for r in self._read_restart_history()
                if isinstance(r.get("ts"), (int, float))
                and now - r["ts"] <= window_sec]

    def _print_restart_history_summary(self):
        """Auto-restart-child startup triage: total trips, trips in last
        hour, last 5 with iso + map_id + reason snippet. Cheap (reads
        one append-only file) and prints to the live log so an SSH'd
        operator sees the thrash story immediately."""
        rows = self._read_restart_history()
        if not rows:
            print(f"[session] restart_history.jsonl: empty (this may be "
                  f"the first auto-restart on this instance)")
            return
        recent_h = [r for r in rows
                    if isinstance(r.get("ts"), (int, float))
                    and time.time() - r["ts"] <= 3600]
        print(f"[session] restart_history.jsonl: {len(rows)} total trip(s), "
              f"{len(recent_h)} in the last 1h")
        for r in rows[-5:]:
            reason = (r.get("reason") or "").splitlines()[0][:80]
            print(f"[session]   {r.get('iso', '?')}  "
                  f"pid={r.get('pid', '?')}  "
                  f"map={r.get('map_id', '?')}  "
                  f"phase={r.get('fight_phase', '?')}  "
                  f"-> {reason}")

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
