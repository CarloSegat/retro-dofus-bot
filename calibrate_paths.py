"""Calibrate just the NSEW switch cells of the current map, auto-inferring
direction from each click.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with map_id populated.
  - config.json has cell_calibration.

Usage:
    python3 calibrate_paths.py <world_x> <world_y>

Flow:
  Click any switch cell. The script computes the cell's iso (u, v)
  coords and decides north/east/south/west by which axis of deviation
  from the canvas centre dominates. Press Esc when done.

  Re-clicking another cell whose inferred direction matches an already
  recorded one overwrites the previous entry. Existing `cells` and
  `obstacles` in `map_data/<world_x>_<world_y>.json` are preserved; a
  fresh file is written with empty cells/obstacles if none exists.
"""
import argparse
import json
import queue
import sys
import time
from pathlib import Path

from pynput import keyboard, mouse

from cell_grid import cell_to_uv, xy_to_cell
from proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
MAP_DATA_DIR = Path(__file__).with_name("map_data")
PROXY_ADDR = "127.0.0.1:9999"

# Centre of the Dofus Retro canvas in iso (u, v) coords. Canvas spans
# sub_row 1..31 (so u+v in [1, 31], midpoint 16) and the playable
# horizontal extent is u-v in [1, 27] (midpoint 14). A switch cell sits
# at one of the four diamond apexes, so its dominant axis of deviation
# from this centre identifies which side of the map it borders.
CANVAS_CENTER_UV_SUM = 16
CANVAS_CENTER_UV_DIFF = 14


def infer_direction(cell):
    """(direction, margin) — direction the clicked switch cell sits on
    relative to the canvas centre, and the |Δaxis| - |Δother| gap (a
    larger margin means a more confident call; 0 means the click was
    on the NE/SE/SW/NW diagonal and the call could go either way)."""
    u, v = cell_to_uv(cell)
    da = (u + v) - CANVAS_CENTER_UV_SUM   # +south, -north
    db = (u - v) - CANVAS_CENTER_UV_DIFF  # +east,  -west
    if abs(db) >= abs(da):
        direction = "east" if db > 0 else "west"
    else:
        direction = "south" if da > 0 else "north"
    return direction, abs(abs(da) - abs(db))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("world_x", type=int)
    parser.add_argument("world_y", type=int)
    args = parser.parse_args()
    world_x, world_y = args.world_x, args.world_y

    out_path = MAP_DATA_DIR / f"{world_x}_{world_y}.json"
    preloaded: dict = {}
    if out_path.exists():
        try:
            preloaded = json.loads(out_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[calibrate-paths] could not read {out_path}: {exc}")
            sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    cal = cfg.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        sys.exit(1)
    max_residual = max(cal["cell_w"], cal["cell_h"])

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[calibrate-paths] connecting to proxy at {PROXY_ADDR}...")
    deadline = time.time() + 5
    while time.time() < deadline and not state.snapshot().connected:
        time.sleep(0.1)
    if not state.snapshot().connected:
        print("[calibrate-paths] not connected to proxy. Is it running?")
        sys.exit(1)
    deadline = time.time() + 5
    while time.time() < deadline and state.snapshot().map_id == 0:
        time.sleep(0.1)
    snap = state.snapshot()
    if snap.map_id == 0:
        print("[calibrate-paths] proxy hasn't reported a map yet (no GDM "
              "seen). Walk between maps once or restart Dofus with the "
              "proxy already running.")
        sys.exit(1)
    map_id = snap.map_id

    if preloaded.get("map_id") not in (None, map_id):
        print(f"[calibrate-paths] {out_path} has map_id={preloaded['map_id']} "
              f"but proxy reports map_id={map_id}. Refusing to overwrite. "
              f"Are you on the wrong map, or do the world coords mismatch?")
        sys.exit(1)

    print(f"[calibrate-paths] map_id={map_id} world=({world_x},{world_y}) "
          f"my_id={snap.my_id}")

    switch_cells: dict[str, int] = dict(preloaded.get("switch_cells") or {})
    if switch_cells:
        print(f"[calibrate-paths] preloaded switch cells: {switch_cells}")

    click_q: queue.Queue = queue.Queue()
    stop = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y))

    def on_key(key):
        if key == keyboard.Key.esc:
            stop["flag"] = True

    mouse_listener = mouse.Listener(on_click=on_click)
    key_listener = keyboard.Listener(on_press=on_key)
    mouse_listener.start()
    key_listener.start()

    print("\n[calibrate-paths] click any switch cell; direction will be "
          "inferred. Esc when done.\n")

    while not stop["flag"]:
        try:
            x, y = click_q.get(timeout=0.5)
        except queue.Empty:
            continue
        cell, residual = xy_to_cell(
            x, y,
            cal["origin_x"], cal["origin_y"],
            cal["cell_w"], cal["cell_h"],
        )
        if residual > max_residual:
            print(f"    click=({x},{y}) -> cell {cell} but residual "
                  f"{residual:.1f}px > {max_residual:.1f}px (outside grid); "
                  f"ignored.")
            continue
        direction, margin = infer_direction(cell)
        prev = switch_cells.get(direction)
        switch_cells[direction] = cell
        note = "(low confidence — click sits on a diagonal)" if margin <= 1 else ""
        if prev is not None and prev != cell:
            print(f"    cell={cell} -> {direction} (was {prev}) {note}".rstrip())
        else:
            print(f"    cell={cell} -> {direction} {note}".rstrip())

    mouse_listener.stop()
    key_listener.stop()

    if not switch_cells:
        print("[calibrate-paths] no switch cells recorded; nothing to save.")
        state.stop()
        sys.exit(0)

    MAP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "world": [world_x, world_y],
        "map_id": map_id,
        "cells": list(preloaded.get("cells") or []),
        "switch_cells": switch_cells,
        "obstacles": sorted(set(preloaded.get("obstacles") or [])),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(data, indent=2))
    print(f"\n[calibrate-paths] saved {len(switch_cells)} switch cell(s) "
          f"({', '.join(sorted(switch_cells))}) for map_id={map_id} "
          f"world=({world_x},{world_y}).")
    if not data["cells"]:
        print("[calibrate-paths] note: cells=[] — main.py will refuse to "
              "fight on this map until you also run calibrate_map_cells.py.")
    print(f"[calibrate-paths] wrote {out_path}")

    state.stop()


if __name__ == "__main__":
    main()
