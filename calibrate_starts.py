"""Calibrate fight starting positions and obstacles for a given map.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with my_id and map_id populated.
  - config.json has cell_calibration (run calibrate_cells.py first).

Usage:
    python3 calibrate_starts.py <world_x> <world_y> [N]

Default N=2 starting positions.

Flow:
  Phase 1 — N starting-cell clicks:
    1. Click the cell on screen where you want the character to stand at
       fight start (use the placement screen, or any map view where the
       cells are visible).
    2. The clicked (x, y) is converted to a cell id via cell_calibration.
    3. The cell id is appended to the saved list.
    Esc during phase 1 = abort.

  Phase 2 — obstacle clicks:
    1. Click any cell you want marked as an obstacle (unwalkable).
    2. Repeat as many times as you want; duplicates are ignored.
    Esc during phase 2 = "done", proceeds to save.

After both phases, the cells and obstacles are written to:
  map_data/<world_x>_<world_y>.json with shape
    {"world": [x, y], "map_id": <id>, "cells": [...], "obstacles": [...]}
"""
import json
import queue
import sys
import time
from pathlib import Path

from pynput import keyboard, mouse

from cell_grid import xy_to_cell
from proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
MAP_DATA_DIR = Path(__file__).with_name("map_data")
PROXY_ADDR = "127.0.0.1:9999"


def main():
    if len(sys.argv) < 3:
        print("usage: calibrate_starts.py <world_x> <world_y> [N]")
        sys.exit(2)
    world_x = int(sys.argv[1])
    world_y = int(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    cfg = json.loads(CONFIG_PATH.read_text())
    cal = cfg.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json. Run calibrate_cells.py first.")
        sys.exit(1)
    max_residual = max(cal["cell_w"], cal["cell_h"])

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[calibrate-starts] connecting to proxy at {PROXY_ADDR}...")
    deadline = time.time() + 5
    while time.time() < deadline and not state.snapshot().connected:
        time.sleep(0.1)
    if not state.snapshot().connected:
        print("[calibrate-starts] not connected to proxy. Is it running?")
        sys.exit(1)
    deadline = time.time() + 5
    while time.time() < deadline and state.snapshot().map_id == 0:
        time.sleep(0.1)
    snap = state.snapshot()
    if snap.map_id == 0:
        print("[calibrate-starts] proxy hasn't reported a map yet "
              "(no GDM seen). Walk between maps once or restart Dofus "
              "with the proxy already running.")
        sys.exit(1)
    map_id = snap.map_id
    print(f"[calibrate-starts] map_id={map_id} world=({world_x},{world_y}) "
          f"my_id={snap.my_id}")

    click_q: queue.Queue = queue.Queue()
    stop = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y))

    def on_key(key):
        if key == keyboard.Key.esc:
            stop["flag"] = True
            # Do NOT return False — listener must stay alive for phase 2.

    mouse_listener = mouse.Listener(on_click=on_click)
    key_listener = keyboard.Listener(on_press=on_key)
    mouse_listener.start()
    key_listener.start()

    def click_to_cell(xy):
        cell, residual = xy_to_cell(
            xy[0], xy[1],
            cal["origin_x"], cal["origin_y"],
            cal["cell_w"], cal["cell_h"],
        )
        return cell, residual

    # Phase 1: starting cells.
    print(f"\n[calibrate-starts] phase 1: click {n} starting cell(s) "
          f"in the order main.py should click them. Esc to abort.\n")
    cells = []
    while len(cells) < n and not stop["flag"]:
        idx = len(cells) + 1
        print(f"  click {idx}/{n}: click the starting cell...")
        try:
            xy = click_q.get(timeout=120)
        except queue.Empty:
            print("    no click received in 120s, aborting.")
            break
        cell, residual = click_to_cell(xy)
        if residual > max_residual:
            print(f"    click=({xy[0]},{xy[1]}) -> cell {cell} but residual "
                  f"{residual:.1f}px > {max_residual:.1f}px (likely outside "
                  f"the grid); try again.")
            continue
        cells.append(cell)
        print(f"    click=({xy[0]},{xy[1]}) -> cell {cell} (residual {residual:.1f}px)")

    if stop["flag"]:
        mouse_listener.stop()
        key_listener.stop()
        print("[calibrate-starts] aborted by user during phase 1.")
        sys.exit(1)
    if len(cells) < n:
        mouse_listener.stop()
        key_listener.stop()
        print("[calibrate-starts] not enough starting cells captured.")
        sys.exit(1)

    # Phase 2: obstacles. Esc now means "done", not abort.
    stop["flag"] = False
    while not click_q.empty():
        try:
            click_q.get_nowait()
        except queue.Empty:
            break
    print(f"\n[calibrate-starts] phase 2: click obstacle cells. "
          f"Press Esc when done.\n")
    obstacles = []
    while not stop["flag"]:
        try:
            xy = click_q.get(timeout=0.5)
        except queue.Empty:
            continue
        cell, residual = click_to_cell(xy)
        if residual > max_residual:
            print(f"    click=({xy[0]},{xy[1]}) -> cell {cell} but residual "
                  f"{residual:.1f}px > {max_residual:.1f}px (likely outside "
                  f"the grid); ignored.")
            continue
        if cell in obstacles:
            print(f"    cell {cell} already marked; skipping")
            continue
        obstacles.append(cell)
        print(f"    obstacle #{len(obstacles)}: cell={cell} (residual {residual:.1f}px)")

    mouse_listener.stop()
    key_listener.stop()

    MAP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MAP_DATA_DIR / f"{world_x}_{world_y}.json"
    data = {
        "world": [world_x, world_y],
        "map_id": map_id,
        "cells": cells,
        "obstacles": sorted(set(obstacles)),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(data, indent=2))
    print(f"\n[calibrate-starts] saved {len(cells)} start cell(s) and "
          f"{len(obstacles)} obstacle(s) for map_id={map_id} "
          f"world=({world_x},{world_y}).")
    print(f"[calibrate-starts] wrote {out_path}")

    state.stop()


if __name__ == "__main__":
    main()
