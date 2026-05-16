"""Calibrate per-map cell data: starting positions, NSEW exits, obstacles.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with my_id and map_id populated.
  - config.json has cell_calibration.

Usage:
    python3 calibrate_map_cells.py <world_x> <world_y> [N]

Default N=2 starting positions.

Flow:
  Phase 1 — N starting-cell clicks:
    Click the cells where main.py should place the character at fight
    start. Esc = abort.

  Phase 2 — NSEW switch-map cells:
    For each of north / east / south / west, click the cell that
    transfers the character to the next map in that direction.
    's' = skip a direction the map doesn't have.
    Esc = abort.

  Phase 3 — obstacles:
    Click any cell to mark it unwalkable. Press Esc when done.

After all phases, writes map_data/<world_x>_<world_y>.json:
    {
      "world":        [x, y],
      "map_id":       <id>,
      "cells":        [<start>, ...],
      "switch_cells": {"north": <cell>, ...},   # only registered dirs
      "obstacles":    [<blocked>, ...],
      "saved_at":     "..."
    }
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
SWITCH_DIRECTIONS = ["north", "east", "south", "west"]


def _drain(q):
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        pass


def main():
    if len(sys.argv) < 3:
        print("usage: calibrate_map_cells.py <world_x> <world_y> [N]")
        sys.exit(2)
    world_x = int(sys.argv[1])
    world_y = int(sys.argv[2])
    n = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    cfg = json.loads(CONFIG_PATH.read_text())
    cal = cfg.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        sys.exit(1)
    max_residual = max(cal["cell_w"], cal["cell_h"])

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[calibrate-map-cells] connecting to proxy at {PROXY_ADDR}...")
    deadline = time.time() + 5
    while time.time() < deadline and not state.snapshot().connected:
        time.sleep(0.1)
    if not state.snapshot().connected:
        print("[calibrate-map-cells] not connected to proxy. Is it running?")
        sys.exit(1)
    deadline = time.time() + 5
    while time.time() < deadline and state.snapshot().map_id == 0:
        time.sleep(0.1)
    snap = state.snapshot()
    if snap.map_id == 0:
        print("[calibrate-map-cells] proxy hasn't reported a map yet "
              "(no GDM seen). Walk between maps once or restart Dofus "
              "with the proxy already running.")
        sys.exit(1)
    map_id = snap.map_id
    print(f"[calibrate-map-cells] map_id={map_id} world=({world_x},{world_y}) "
          f"my_id={snap.my_id}")

    click_q: queue.Queue = queue.Queue()
    key_q: queue.Queue = queue.Queue()
    stop = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y))

    def on_key(key):
        if key == keyboard.Key.esc:
            stop["flag"] = True
            # Listener stays alive so phases 2 and 3 can still see input.
            return
        # 's' skips the current NSEW direction in phase 2 (ignored elsewhere).
        # Note: 's' is not suppressed from Dofus, but has no harmful effect.
        char = getattr(key, "char", None)
        if char == "s":
            key_q.put("s")

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

    # ----- Phase 1: starting cells -----
    print(f"\n[calibrate-map-cells] phase 1: click {n} starting cell(s) "
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
        print("[calibrate-map-cells] aborted by user during phase 1.")
        sys.exit(1)
    if len(cells) < n:
        mouse_listener.stop()
        key_listener.stop()
        print("[calibrate-map-cells] not enough starting cells captured.")
        sys.exit(1)

    # ----- Phase 2: NSEW switch-map cells -----
    print(f"\n[calibrate-map-cells] phase 2: click the N/E/S/W switch cells "
          f"(cells that transfer to the next map).")
    print("  's' = skip a direction this map doesn't have. Esc = abort.\n")
    switch_cells: dict[str, int] = {}
    for direction in SWITCH_DIRECTIONS:
        if stop["flag"]:
            break
        while not stop["flag"]:
            _drain(click_q)
            _drain(key_q)
            print(f"  [{direction}] click the {direction} switch cell, or 's' to skip:")
            ev = None
            deadline = time.time() + 120
            while time.time() < deadline and ev is None and not stop["flag"]:
                try:
                    ev = ("click", click_q.get(timeout=0.1))
                    break
                except queue.Empty:
                    pass
                try:
                    key_q.get_nowait()
                    ev = ("skip", None)
                    break
                except queue.Empty:
                    pass
            if ev is None or stop["flag"]:
                print(f"  [{direction}] no input within timeout; skipping.")
                break
            if ev[0] == "skip":
                print(f"  [{direction}] skipped.")
                break
            xy = ev[1]
            cell, residual = click_to_cell(xy)
            if residual > max_residual:
                print(f"    click=({xy[0]},{xy[1]}) -> cell {cell} but residual "
                      f"{residual:.1f}px > {max_residual:.1f}px (outside grid); try again.")
                continue
            switch_cells[direction] = cell
            print(f"    {direction} switch cell = {cell} (residual {residual:.1f}px)")
            break

    if stop["flag"]:
        mouse_listener.stop()
        key_listener.stop()
        print("[calibrate-map-cells] aborted by user during phase 2.")
        sys.exit(1)

    # ----- Phase 3: obstacles ----- (Esc now means "done", not abort.)
    stop["flag"] = False
    _drain(click_q)
    _drain(key_q)
    print(f"\n[calibrate-map-cells] phase 3: click obstacle cells. "
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
        "switch_cells": switch_cells,
        "obstacles": sorted(set(obstacles)),
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    out_path.write_text(json.dumps(data, indent=2))
    print(f"\n[calibrate-map-cells] saved {len(cells)} start cell(s), "
          f"{len(switch_cells)} switch cell(s) ({', '.join(switch_cells) or 'none'}), "
          f"and {len(obstacles)} obstacle(s) for map_id={map_id} "
          f"world=({world_x},{world_y}).")
    print(f"[calibrate-map-cells] wrote {out_path}")

    state.stop()


if __name__ == "__main__":
    main()
