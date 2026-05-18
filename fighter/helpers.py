"""Cross-cutting helpers used by multiple fighter classes.

Most of these are small utilities -- proxy-state waiting, fight-cell
lookups, stats persistence, runtime prompts -- that no single class
deserves to own. Constants that get read by more than one class also
live here.
"""
import json
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


def load_cal():
    """Cell calibration dict from config.json. Exits if missing."""
    cal = CFG.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        sys.exit(1)
    return cal


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
    """Alive enemies in current fight, sorted by Po distance from us."""
    me_cell = my_fight_cell(snap)
    enemies = [
        e for e in snap.fight_entities.values()
        if e.alive and e.id != snap.my_id and e.cell > 0
    ]
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
