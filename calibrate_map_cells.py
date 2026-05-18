"""Calibrate per-map cell data: starting positions, NSEW exits, obstacles.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with my_id and map_id populated.
  - config.json has cell_calibration.

Usage:
    python3 calibrate_map_cells.py <world_x> <world_y> [N] [--obs-only|--switches-only]

Default N=2 starting positions.

  --obs-only: skip phases 1 and 2 and only edit the obstacle list of an
              already-calibrated map. The existing cells/switch_cells
              are preserved. Requires the map_data file to exist.

  --switches-only: skip phases 1 and 3 and only re-do the NSEW switch
              cells. The existing cells/obstacles are preserved.
              Requires the map_data file to exist.

Flow:
  Phase 1 — N starting-cell clicks:
    Click the cells where main.py should place the character at fight
    start. Esc = abort.

  Phase 2 — NSEW switch-map cells:
    For each of north / east / south / west, click the cell that
    transfers the character to the next map in that direction.
    'n' = skip a direction the map doesn't have.
    Esc = abort.

  Phase 3 — obstacles:
    Click any cell to mark it unwalkable. Shift+click an existing
    obstacle to remove it (useful when fixing a mis-click without
    redoing the whole map). Press Esc when done.

    When recalibrating an existing map, the previous obstacle list is
    preloaded so shift+click can drop specific entries while plain
    clicks keep adding new ones.

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
import argparse
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
    parser = argparse.ArgumentParser()
    parser.add_argument("world_x", type=int)
    parser.add_argument("world_y", type=int)
    parser.add_argument("n", type=int, nargs="?", default=2,
                        help="number of starting cells to record (default 2)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--obs-only", action="store_true",
                      help="skip phases 1 and 2; only edit the obstacle list "
                           "of an existing map_data file")
    mode.add_argument("--switches-only", action="store_true",
                      help="skip phases 1 and 3; only re-do the NSEW switch "
                           "cells of an existing map_data file")
    args = parser.parse_args()
    world_x, world_y, n = args.world_x, args.world_y, args.n
    obs_only, switches_only = args.obs_only, args.switches_only

    out_path = MAP_DATA_DIR / f"{world_x}_{world_y}.json"
    preloaded: dict = {}
    preloaded_obstacles: list[int] = []
    if obs_only or switches_only:
        flag_name = "--obs-only" if obs_only else "--switches-only"
        if not out_path.exists():
            print(f"[calibrate-map-cells] {flag_name} requires {out_path} to "
                  f"already exist. Run a full calibration first.")
            sys.exit(1)
        try:
            preloaded = json.loads(out_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"[calibrate-map-cells] could not read {out_path}: {exc}")
            sys.exit(1)
        preloaded_obstacles = list(preloaded.get("obstacles") or [])
        if obs_only:
            print(f"[calibrate-map-cells] --obs-only: preserving "
                  f"{len(preloaded.get('cells') or [])} start cell(s) and "
                  f"{len(preloaded.get('switch_cells') or {})} switch cell(s) "
                  f"from {out_path}")
        else:
            print(f"[calibrate-map-cells] --switches-only: preserving "
                  f"{len(preloaded.get('cells') or [])} start cell(s) and "
                  f"{len(preloaded_obstacles)} obstacle(s) from {out_path}")
    elif out_path.exists():
        resp = input(
            f"[calibrate-map-cells] {out_path} already exists. "
            f"Recalibrating will overwrite cells/switches; obstacles "
            f"are preloaded so shift+click can drop entries. Continue? [y/N] "
        ).strip().lower()
        if resp not in ("y", "yes"):
            print("[calibrate-map-cells] aborted.")
            sys.exit(0)
        try:
            preloaded = json.loads(out_path.read_text())
            preloaded_obstacles = list(preloaded.get("obstacles") or [])
        except (OSError, json.JSONDecodeError):
            preloaded = {}
            preloaded_obstacles = []

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
    shift_held = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y, shift_held["flag"]))

    def on_key(key):
        if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            shift_held["flag"] = True
            return
        if key == keyboard.Key.esc:
            stop["flag"] = True
            # Listener stays alive so phases 2 and 3 can still see input.
            return
        # 'n' skips the current NSEW direction in phase 2 (ignored elsewhere).
        char = getattr(key, "char", None)
        if char == "n":
            key_q.put("n")

    def on_key_release(key):
        if key in (keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r):
            shift_held["flag"] = False

    mouse_listener = mouse.Listener(on_click=on_click)
    key_listener = keyboard.Listener(on_press=on_key, on_release=on_key_release)
    mouse_listener.start()
    key_listener.start()

    def click_to_cell(xy):
        cell, residual = xy_to_cell(
            xy[0], xy[1],
            cal["origin_x"], cal["origin_y"],
            cal["cell_w"], cal["cell_h"],
        )
        return cell, residual

    if obs_only:
        cells: list[int] = list(preloaded.get("cells") or [])
        switch_cells: dict[str, int] = dict(preloaded.get("switch_cells") or {})
        print(f"\n[calibrate-map-cells] --obs-only: skipping phases 1 and 2 "
              f"(preserving {len(cells)} start cell(s) and "
              f"{len(switch_cells)} switch cell(s)).")
    elif switches_only:
        cells = list(preloaded.get("cells") or [])
        print(f"\n[calibrate-map-cells] --switches-only: skipping phase 1 "
              f"(preserving {len(cells)} start cell(s)).")
        switch_cells = {}
    else:
        # ----- Phase 1: starting cells -----
        print(f"\n[calibrate-map-cells] phase 1: click {n} starting cell(s) "
              f"in the order main.py should click them. Esc to abort.\n")
        cells = []
        switch_cells = {}
        while len(cells) < n and not stop["flag"]:
            idx = len(cells) + 1
            print(f"  click {idx}/{n}: click the starting cell...")
            try:
                x, y, _shift = click_q.get(timeout=120)
            except queue.Empty:
                print("    no click received in 120s, aborting.")
                break
            cell, residual = click_to_cell((x, y))
            if residual > max_residual:
                print(f"    click=({x},{y}) -> cell {cell} but residual "
                      f"{residual:.1f}px > {max_residual:.1f}px (likely outside "
                      f"the grid); try again.")
                continue
            cells.append(cell)
            print(f"    click=({x},{y}) -> cell {cell} (residual {residual:.1f}px)")

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

    # ----- Phase 2: NSEW switch-map cells ----- (skipped by --obs-only)
    if not obs_only:
        print(f"\n[calibrate-map-cells] phase 2: click the N/E/S/W switch cells "
              f"(cells that transfer to the next map).")
        print("  'n' = skip a direction this map doesn't have. Esc = abort.\n")
        for direction in SWITCH_DIRECTIONS:
            if stop["flag"]:
                break
            while not stop["flag"]:
                _drain(click_q)
                _drain(key_q)
                print(f"  [{direction}] click the {direction} switch cell, or 'n' to skip:")
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
                x, y, _shift = ev[1]
                cell, residual = click_to_cell((x, y))
                if residual > max_residual:
                    print(f"    click=({x},{y}) -> cell {cell} but residual "
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

    # ----- Phase 3: obstacles ----- (skipped by --switches-only; Esc = done)
    stop["flag"] = False
    _drain(click_q)
    _drain(key_q)
    obstacles = list(dict.fromkeys(preloaded_obstacles))  # de-dup, preserve order
    if switches_only:
        print(f"\n[calibrate-map-cells] --switches-only: skipping phase 3 "
              f"(preserving {len(obstacles)} obstacle(s)).")
    else:
        if obstacles:
            print(f"\n[calibrate-map-cells] phase 3: {len(obstacles)} obstacle(s) "
                  f"preloaded from existing file. Click to add, shift+click to "
                  f"remove, Esc when done.\n")
        else:
            print(f"\n[calibrate-map-cells] phase 3: click obstacle cells (add), "
                  f"shift+click to remove. Press Esc when done.\n")
    while not switches_only and not stop["flag"]:
        try:
            x, y, shift = click_q.get(timeout=0.5)
        except queue.Empty:
            continue
        cell, residual = click_to_cell((x, y))
        if residual > max_residual:
            print(f"    click=({x},{y}) -> cell {cell} but residual "
                  f"{residual:.1f}px > {max_residual:.1f}px (likely outside "
                  f"the grid); ignored.")
            continue
        if shift:
            if cell in obstacles:
                obstacles.remove(cell)
                print(f"    REMOVED cell={cell} ({len(obstacles)} left)")
            else:
                print(f"    shift+click cell={cell} not in obstacles; no-op")
            continue
        if cell in obstacles:
            print(f"    cell {cell} already marked; skipping")
            continue
        obstacles.append(cell)
        print(f"    obstacle #{len(obstacles)}: cell={cell} (residual {residual:.1f}px)")

    mouse_listener.stop()
    key_listener.stop()

    MAP_DATA_DIR.mkdir(parents=True, exist_ok=True)
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
