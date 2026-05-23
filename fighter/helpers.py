"""Cross-cutting helpers used by multiple fighter classes.

Most of these are small utilities -- proxy-state waiting, fight-cell
lookups, stats persistence, runtime prompts -- that no single class
deserves to own. Constants that get read by more than one class also
live here.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

from dofus.cell_grid import cell_distance
from mouse_keyboard import press_focused
from utils import CFG

PROXY_ADDR = "127.0.0.1:9999"
IDLE_POLL_SEC = 0.5
STATS_FILE = Path(__file__).resolve().parent.parent / "data" / "stats.json"


def resolve_screen_name(explicit=None):
    """Pick the calibration screen name. Precedence: explicit arg
    (from --screen) > FIGHTER_SCREEN env var > config default_screen.
    Returns the name string, or None if nothing is configured."""
    if explicit:
        return explicit
    env = os.environ.get("FIGHTER_SCREEN")
    if env:
        return env
    return CFG.get("default_screen")


def load_cal(screen_name=None):
    """Cell calibration dict from config.json[cell_calibrations][<name>].

    Resolves the screen name via resolve_screen_name (explicit arg ->
    env -> config default). Exits with a clear message if the name is
    missing or doesn't match a known calibration."""
    cals = CFG.get("cell_calibrations") or {}
    if not cals:
        print("missing 'cell_calibrations' in config.json. Run "
              "`python3 recalibrate_screen.py <name>` first.")
        sys.exit(1)
    name = resolve_screen_name(screen_name)
    if not name:
        print(f"no screen specified. Pass --screen <name>, set "
              f"FIGHTER_SCREEN env, or add 'default_screen' to "
              f"config.json. Known screens: {sorted(cals)}")
        sys.exit(1)
    if name not in cals:
        print(f"cell_calibrations has no entry for screen={name!r}. "
              f"Known: {sorted(cals)}. Run "
              f"`python3 recalibrate_screen.py {name}` to create it.")
        sys.exit(1)
    return cals[name]


def wait_for(state, predicate, timeout, poll=0.2):
    """Poll until predicate(snapshot) is True or timeout. Returns bool."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(state.snapshot()):
            return True
        time.sleep(poll)
    return False


def my_fight_cell(snap):
    """Cell of our own character. In fight, the proxy's `my_cell` does NOT
    update from GTM (only from GA;1; movement), so prefer fight_entities."""
    me = snap.fight_entities.get(snap.my_id) if snap.my_id else None
    if me and me.cell > 0:
        return me.cell
    return snap.my_cell


def alive_enemies(snap):
    """Alive enemies in current fight, sorted by Po distance from us.

    Summons are de-prioritised: if any non-summon alive enemy exists,
    summons are filtered out so we don't waste turns on transient
    minions. When only summons remain (real mobs all dead) they come
    back as the targetable set."""
    me_cell = my_fight_cell(snap)
    alive = [
        e for e in snap.fight_entities.values()
        if e.alive and e.id != snap.my_id and e.cell > 0
    ]
    real = [e for e in alive if not e.is_summon]
    enemies = real if real else alive
    enemies.sort(key=lambda e: cell_distance(me_cell, e.cell) if me_cell else 0)
    return enemies


def append_fight_stats(mob_size, duration_sec):
    """Append one {mob_size, fight_duration} record to data/stats.json.

    Reads, appends, atomically replaces the file. Resets to [] if the
    existing file is missing or unparseable."""
    STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
    records = []
    if STATS_FILE.exists():
        try:
            with STATS_FILE.open() as f:
                records = json.load(f)
            if not isinstance(records, list):
                records = []
        except (OSError, json.JSONDecodeError):
            records = []
    records.append({"mob_size": mob_size, "fight_duration": round(duration_sec, 2)})
    tmp = STATS_FILE.with_suffix(".json.tmp")
    with tmp.open("w") as f:
        json.dump(records, f, indent=2)
    tmp.replace(STATS_FILE)


def make_exchange_dismiss_callback():
    """Returns a proxy on_event callback that schedules an Esc 1s after
    any exchange_open event.

    The bot sometimes click-engages a mob whose cell coincides with a
    player in merchant mode -- the click registers as 'open shop' and
    blocks all follow-ups. The proxy emits ECK<kind>|<id> on the wire;
    1s gives the shop UI time to fully render before Esc dismisses it.
    Esc is sent via xdotool, not pynput, so EscStop won't see it
    (XSendEvent sets send_event; pynput filters those)."""
    def cb(ev):
        if ev.get("type") != "exchange_open":
            return
        kind = ev.get("kind")
        target = ev.get("target")
        def fire():
            print(f"[fighter] exchange_open (kind={kind} target={target}) -> Esc")
            try:
                press_focused("Escape")
            except Exception as e:
                print(f"[fighter] exchange Esc failed: {e}")
        threading.Timer(1.0, fire).start()
    return cb


def make_npc_dialog_dismiss_callback():
    """Returns a proxy on_event callback that schedules an Esc 1s after
    any npc_dialog_open event.

    The bot sometimes click-engages a cell that is occupied by an NPC
    (e.g. Rotable the Sad Shepherd standing among gobballs); the click
    opens the NPC's dialog and blocks all follow-ups until dismissed.
    The proxy emits DQ<questionId>[;<npcId>]|<replies...> on the wire;
    1s gives the dialog UI time to render before Esc closes it. Same
    xdotool-via-press_focused trick as the exchange dismiss."""
    def cb(ev):
        if ev.get("type") != "npc_dialog_open":
            return
        question = ev.get("question")
        npc = ev.get("npc")
        def fire():
            print(f"[fighter] npc_dialog_open (question={question} npc={npc}) -> Esc")
            try:
                press_focused("Escape")
            except Exception as e:
                print(f"[fighter] npc dialog Esc failed: {e}")
        threading.Timer(1.0, fire).start()
    return cb


def prompt_int(label, default):
    raw = input(f"{label} [default {default}]: ").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        print(f"  not an int; using default {default}")
        return default


def prompt_yn(label, default=True):
    suffix = "Y/n" if default else "y/N"
    raw = input(f"{label} [{suffix}]: ").strip().lower()
    if not raw:
        return default
    return raw in ("y", "yes")
