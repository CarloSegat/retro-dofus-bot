"""Calibrate switch cells across many maps in one walking session.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with map_id populated.
  - config.json has cell_calibration.

Usage:
    python3 calibrate_paths.py <world_x> <world_y>

Flow:
  Tell the script where you started (world coords). Then walk through
  doors. Each click on a switch cell records the cell as that map's
  N/E/S/W switch; once the proxy reports a new map_id, the script
  follows the inferred direction (W:x-1, E:x+1, N:y-1, S:y+1) to
  figure out which world coords you've arrived at, and keeps going.
  Press Esc when done. All touched maps get a file written in
  map_data/.

  Existing per-map files preserve `cells` and `obstacles`. The script
  refuses to start if the proxy's current map_id is already pinned to
  a different world by another file (catches wrong starting coords).
"""
import argparse
import json
import queue
import sys
import time
from pathlib import Path

from pynput import keyboard, mouse

from dofus.cell_grid import cell_to_uv, xy_to_cell
from dofus.proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
MAP_DATA_DIR = Path(__file__).with_name("map_data")
PROXY_ADDR = "127.0.0.1:9999"

# Canvas centre in iso (u, v) coords. See infer_direction.
CANVAS_CENTER_UV_SUM = 16
CANVAS_CENTER_UV_DIFF = 14

DIR_DELTA = {
    "west":  (-1, 0),
    "east":  (1, 0),
    "north": (0, -1),
    "south": (0, 1),
}

# A click on a switch cell triggers a GDM packet shortly after. If
# nothing arrives within this window, treat the click as a non-switch
# (clicked an obstacle, a regular floor, etc) and discard.
PENDING_TRANSITION_TIMEOUT_SEC = 2.5


def infer_direction(cell):
    """(direction, margin) — see original implementation. margin <= 1
    means the click sits on a NE/SE/SW/NW diagonal and the call is
    ambiguous."""
    u, v = cell_to_uv(cell)
    da = (u + v) - CANVAS_CENTER_UV_SUM   # +south, -north
    db = (u - v) - CANVAS_CENTER_UV_DIFF  # +east,  -west
    if abs(db) >= abs(da):
        direction = "east" if db > 0 else "west"
    else:
        direction = "south" if da > 0 else "north"
    return direction, abs(abs(da) - abs(db))


def load_existing(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("world_x", type=int)
    parser.add_argument("world_y", type=int)
    args = parser.parse_args()
    start_world = (args.world_x, args.world_y)

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

    # Build lookup map_id -> (world_x, world_y) from existing files so
    # we can name maps in logs and detect conflicts when we infer new
    # world coords during the walk.
    map_id_to_world: dict[int, tuple[int, int]] = {}
    for f in MAP_DATA_DIR.glob("*.json"):
        d = load_existing(f)
        if not d:
            continue
        mid = d.get("map_id")
        w = d.get("world")
        if isinstance(mid, int) and isinstance(w, list) and len(w) == 2:
            map_id_to_world[mid] = (w[0], w[1])

    # Per-map data we'll write back at the end. Lazy-loaded on first
    # touch, then mutated in memory. `modified` is the flag that gates
    # the file write -- maps we only walked through but never recorded
    # a switch for are left alone.
    cache: dict[tuple[int, int], dict] = {}

    def get_data(world: tuple[int, int]) -> dict:
        if world in cache:
            return cache[world]
        path = MAP_DATA_DIR / f"{world[0]}_{world[1]}.json"
        existing = load_existing(path) or {}
        cache[world] = {
            "world": list(world),
            "map_id": existing.get("map_id"),
            "cells": list(existing.get("cells") or []),
            "switch_cells": dict(existing.get("switch_cells") or {}),
            "obstacles": sorted(set(existing.get("obstacles") or [])),
            "modified": False,
        }
        return cache[world]

    cur_map_id = snap.map_id
    cur_world = start_world
    known = map_id_to_world.get(cur_map_id)
    if known is not None and known != cur_world:
        print(f"[calibrate-paths] proxy reports map_id={cur_map_id}, which "
              f"existing files map to world={known}, but you passed "
              f"world={cur_world}. Refusing to start. Use the correct "
              f"coords, or delete the conflicting file.")
        state.stop()
        sys.exit(1)
    map_id_to_world[cur_map_id] = cur_world
    d = get_data(cur_world)
    if d.get("map_id") and d["map_id"] != cur_map_id:
        print(f"[calibrate-paths] file for {cur_world} has map_id={d['map_id']} "
              f"but proxy reports {cur_map_id}. Refusing to start.")
        state.stop()
        sys.exit(1)
    if d.get("map_id") != cur_map_id:
        d["map_id"] = cur_map_id
        d["modified"] = True

    print(f"[calibrate-paths] starting on map_id={cur_map_id} world={cur_world} "
          f"my_id={snap.my_id}")
    if d["switch_cells"]:
        print(f"[calibrate-paths] preloaded switch cells for {cur_world}: "
              f"{d['switch_cells']}")

    def fmt_map(mid: int) -> str:
        w = map_id_to_world.get(mid)
        return f"map_id={mid} ({w[0]},{w[1]})" if w else f"map_id={mid} (?,?)"

    click_q: queue.Queue = queue.Queue()
    stop = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y, state.snapshot().map_id))

    def on_key(key):
        if key == keyboard.Key.esc:
            stop["flag"] = True

    mouse_listener = mouse.Listener(on_click=on_click)
    key_listener = keyboard.Listener(on_press=on_key)
    mouse_listener.start()
    key_listener.start()

    print("\n[calibrate-paths] click switch cells as you walk. The script "
          "follows you across maps. Esc when done.\n")

    # `pending` holds a click that hasn't yet resolved into a map
    # transition. We commit it when snap.map_id changes; discard it on
    # timeout (the click didn't trigger a switch).
    st = {
        "cur_world": cur_world,
        "cur_map_id": cur_map_id,
        "pending": None,
    }

    def commit_pending(new_map_id: int):
        p = st["pending"]
        src_world = p["src_world"]
        cell = p["cell"]
        direction = p["direction"]

        src = get_data(src_world)
        prev = src["switch_cells"].get(direction)
        src["switch_cells"][direction] = cell
        src["modified"] = True

        dx, dy = DIR_DELTA[direction]
        dst_world = (src_world[0] + dx, src_world[1] + dy)

        # If we've seen this destination map_id before under a
        # different world, our direction inference is probably wrong
        # (diagonal click) -- undo the switch recording rather than
        # silently corrupt files.
        prior = map_id_to_world.get(new_map_id)
        if prior is not None and prior != dst_world:
            print(f"    !! direction says destination is {dst_world}, "
                  f"but {fmt_map(new_map_id)} is already recorded as "
                  f"{prior}. Undoing switch recording for "
                  f"{src_world}.{direction}.")
            if prev is None:
                src["switch_cells"].pop(direction, None)
            else:
                src["switch_cells"][direction] = prev
            # We still walked to the known map, so reflect that.
            st["cur_world"] = prior
            st["cur_map_id"] = new_map_id
            st["pending"] = None
            return

        map_id_to_world[new_map_id] = dst_world
        dst = get_data(dst_world)
        if dst.get("map_id") and dst["map_id"] != new_map_id:
            print(f"    !! file for {dst_world} has map_id={dst['map_id']} "
                  f"but we arrived at {new_map_id}. Keeping the new id; "
                  f"check for stale files.")
        if dst.get("map_id") != new_map_id:
            dst["map_id"] = new_map_id
            dst["modified"] = True

        prev_str = f" (was {prev})" if prev is not None and prev != cell else ""
        print(f"    {src_world} {direction}={cell}{prev_str} -> arrived "
              f"{fmt_map(new_map_id)}")

        st["cur_world"] = dst_world
        st["cur_map_id"] = new_map_id
        st["pending"] = None

    def save_all():
        MAP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        saved = []
        for world, d in cache.items():
            if not d.get("modified"):
                continue
            path = MAP_DATA_DIR / f"{world[0]}_{world[1]}.json"
            out = {
                "world": list(world),
                "map_id": d["map_id"],
                "cells": list(d["cells"]),
                "switch_cells": d["switch_cells"],
                "obstacles": sorted(set(d["obstacles"])),
                "saved_at": ts,
            }
            path.write_text(json.dumps(out, indent=2))
            saved.append((world, len(d["switch_cells"])))
        return saved

    try:
        while not stop["flag"]:
            snap = state.snapshot()
            if st["pending"] is not None:
                if snap.map_id and snap.map_id != st["pending"]["src_map_id"]:
                    commit_pending(snap.map_id)
                elif time.time() - st["pending"]["ts"] > PENDING_TRANSITION_TIMEOUT_SEC:
                    p = st["pending"]
                    print(f"    cell={p['cell']} ({p['direction']}) didn't "
                          f"trigger a map change; discarding.")
                    st["pending"] = None

            try:
                x, y, click_map_id = click_q.get(timeout=0.2)
            except queue.Empty:
                continue

            if st["pending"] is not None:
                print("    previous click still pending a transition; "
                      "this click ignored.")
                continue

            if click_map_id != st["cur_map_id"]:
                # Click landed during/after a transition we already
                # processed. Skip rather than misattribute.
                print(f"    click on {fmt_map(click_map_id)} ignored "
                      f"(currently on {fmt_map(st['cur_map_id'])}).")
                continue

            cell, residual = xy_to_cell(
                x, y,
                cal["origin_x"], cal["origin_y"],
                cal["cell_w"], cal["cell_h"],
            )
            if residual > max_residual:
                print(f"    click=({x},{y}) -> cell {cell} but residual "
                      f"{residual:.1f}px > {max_residual:.1f}px (outside "
                      f"grid); ignored.")
                continue
            direction, margin = infer_direction(cell)
            note = "" if margin > 1 else " (low confidence — diagonal)"
            print(f"    {fmt_map(st['cur_map_id'])} cell={cell} -> "
                  f"{direction}{note}; waiting for map change...")
            st["pending"] = {
                "cell": cell,
                "direction": direction,
                "src_world": st["cur_world"],
                "src_map_id": st["cur_map_id"],
                "ts": time.time(),
            }
    finally:
        mouse_listener.stop()
        key_listener.stop()
        state.stop()

    saved = save_all()
    if not saved:
        print("[calibrate-paths] no switch cells recorded; nothing to save.")
        return
    print("\n[calibrate-paths] saved:")
    for world, n in saved:
        print(f"  ({world[0]},{world[1]}): {n} switch cell(s) total")


if __name__ == "__main__":
    main()
