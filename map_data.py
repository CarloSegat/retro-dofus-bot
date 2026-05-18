"""Per-map calibrated data: starting cells, NSEW exits, obstacles.

Written by `calibrate_map_cells.py`, read by `main.py`. One JSON file
per map at `map_data/<world_x>_<world_y>.json`:

    {
      "world":        [x, y],
      "map_id":       <int>,
      "cells":        [<start_cell>, ...],
      "switch_cells": {"north": <cell>, ...},  # only registered dirs
      "obstacles":    [<blocked_cell>, ...],
      "saved_at":     "YYYY-MM-DD HH:MM:SS"
    }

Dofus world-coord convention (per DIRECTION_WORLD_DELTA): north
decreases y, south increases y, east increases x, west decreases x.

Manual / calibrated counterpart to `obstacles.py`'s runtime-learned
blocked store at `~/.auto-fighter/blocked.json`.
"""
import json
from pathlib import Path

MAP_DATA_DIR = Path(__file__).with_name("map_data")

DIRECTION_WORLD_DELTA = {
    "north": (0, -1),
    "south": (0, 1),
    "east":  (1, 0),
    "west":  (-1, 0),
}
OPPOSITE_DIRECTION = {
    "north": "south",
    "south": "north",
    "east":  "west",
    "west":  "east",
}


def load_all():
    """Return {map_id: parsed_file_dict} for every JSON in map_data/."""
    out = {}
    if not MAP_DATA_DIR.exists():
        return out
    for path in MAP_DATA_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        mid = data.get("map_id")
        if mid is None:
            continue
        out[int(mid)] = data
    return out


def file_path_for(entry):
    """Path to the on-disk JSON file for a map_data entry, derived from
    its world coords. Returns None if `entry` has no usable world field."""
    world = entry.get("world")
    if not (isinstance(world, (list, tuple)) and len(world) == 2):
        return None
    return MAP_DATA_DIR / f"{int(world[0])}_{int(world[1])}.json"


def save(entry):
    """Write `entry` back to its world-derived JSON file. No-op if the
    world field is missing. Preserves the same field order calibration
    writes (world, map_id, cells, switch_cells, obstacles, saved_at)."""
    path = file_path_for(entry)
    if path is None:
        return False
    ordered = {k: entry[k] for k in
               ("world", "map_id", "cells", "switch_cells",
                "obstacles", "saved_at")
               if k in entry}
    for k, v in entry.items():
        if k not in ordered:
            ordered[k] = v
    path.write_text(json.dumps(ordered, indent=2))
    return True


def build_world_index(map_data):
    """{(world_x, world_y): entry} for every entry with a valid world field."""
    out = {}
    for entry in map_data.values():
        world = entry.get("world")
        if not (isinstance(world, (list, tuple)) and len(world) == 2):
            continue
        out[(int(world[0]), int(world[1]))] = entry
    return out


def target_map_id(entry, direction, by_world):
    """The map_id reached by walking `direction` from `entry`, or None if
    the target world coord has no calibrated map_data file."""
    world = entry.get("world")
    if not (isinstance(world, (list, tuple)) and len(world) == 2):
        return None
    delta = DIRECTION_WORLD_DELTA.get(direction)
    if delta is None:
        return None
    target = by_world.get((int(world[0]) + delta[0], int(world[1]) + delta[1]))
    return target.get("map_id") if target else None


def safe_directions(entry, by_world):
    """Directions from `entry` whose target map is calibrated AND has
    the opposite switch cell calibrated (= return path exists)."""
    world = entry.get("world")
    if not (isinstance(world, (list, tuple)) and len(world) == 2):
        return []
    switches = entry.get("switch_cells") or {}
    out = []
    wx, wy = int(world[0]), int(world[1])
    for direction in switches:
        delta = DIRECTION_WORLD_DELTA.get(direction)
        if delta is None:
            continue
        target = by_world.get((wx + delta[0], wy + delta[1]))
        if not target:
            continue
        if OPPOSITE_DIRECTION[direction] not in (target.get("switch_cells") or {}):
            continue
        out.append(direction)
    return out
