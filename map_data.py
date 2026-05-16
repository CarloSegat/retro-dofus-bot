"""Per-map calibrated data: starting cells and obstacle cells.

Written by `calibrate_starts.py` to `map_data/<world_x>_<world_y>.json`.
Read by `main.py` at startup so the bot knows where to stand at fight
start and which cells the A* path planner should treat as unwalkable.

On-disk schema (one file per map):

    {
      "world":     [x, y],
      "map_id":    <int>,
      "cells":     [<start_cell>, ...],
      "obstacles": [<blocked_cell>, ...],
      "saved_at":  "YYYY-MM-DD HH:MM:SS"
    }

Public API:
  load_all() -> {map_id: data}
      Scan map_data/ and return an index keyed by map_id (proxy gives
      map_id at runtime, not world coords). Files missing `map_id` are
      skipped.

This is the manual / calibrated counterpart to `obstacles.py`'s
runtime-learned blocked store at `~/.auto-fighter/blocked.json`.
"""
import json
from pathlib import Path

MAP_DATA_DIR = Path(__file__).with_name("map_data")


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
