"""Per-map calibrated data: starting cells, NSEW exits, obstacle cells.

Written by `calibrate_map_cells.py` to `map_data/<world_x>_<world_y>.json`.
Read by `main.py` at startup so the bot knows where to stand at fight
start and which cells the A* path planner should treat as unwalkable.

On-disk schema (one file per map):

    {
      "world":        [x, y],
      "map_id":       <int>,
      "cells":        [<start_cell>, ...],
      "switch_cells": {"north": <cell>, ...},  # only registered dirs
      "obstacles":    [<blocked_cell>, ...],
      "saved_at":     "YYYY-MM-DD HH:MM:SS"
    }

Public API:
  load_all() -> {map_id: data}
      Scan map_data/ and return an index keyed by map_id (proxy gives
      map_id at runtime, not world coords). Files missing `map_id` are
      skipped.
  build_world_index(map_data) -> {(x, y): data}
      Re-key by world coords for neighbour lookups.
  safe_directions(entry, by_world) -> list of directions on `entry`
      whose target map is calibrated AND has the opposite switch cell.

Navigation constants:
  DIRECTION_WORLD_DELTA  -- {direction: (dx, dy)}. Dofus convention:
                            north decreases y, south increases y, east
                            increases x, west decreases x.
  OPPOSITE_DIRECTION     -- {direction: opposite_direction}.

This is the manual / calibrated counterpart to `obstacles.py`'s
runtime-learned blocked store at `~/.auto-fighter/blocked.json`.
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
    """Directions from `entry` whose target map is calibrated *and* has the
    opposite switch cell calibrated (so we can return).

    Skips entries without a valid world field or switch_cells dict."""
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
