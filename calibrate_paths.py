"""Calibrate switch cells across many maps in one walking session.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with map_id populated.
  - config.json has cell_calibrations for the active screen
    (resolved via --screen, $FIGHTER_SCREEN, or default_screen).
  - The map_data Postgres DB is reachable.

Usage:
    python3 calibrate_paths.py <world_x> <world_y>

Flow:
  Tell the script where you started (world coords). Then walk through
  doors. Each click on a switch cell records the cell as that map's
  N/E/S/W switch; once the proxy reports a new map_id, the script
  follows the inferred direction (W:x-1, E:x+1, N:y-1, S:y+1) to
  figure out which world coords you've arrived at, and keeps going.
  Press Esc when done. All touched maps get upserted into the DB.

  Existing rows preserve `cells` and `obstacles` (only switch_cells
  is rewritten per modified map). The script refuses to start if the
  proxy's current map_id is already pinned to a different world by
  existing DB rows (catches wrong starting coords).
"""
import argparse
import json
import queue
import sys
import time
from pathlib import Path

from pynput import keyboard, mouse

from dofus.cell_grid import cell_to_uv, xy_to_cell
from dofus.map_data import load_all as load_map_data, save as save_map_data
from dofus.proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("world_x", type=int)
    parser.add_argument("world_y", type=int)
    parser.add_argument("--screen", default=None,
                        help="calibration key in config.json[cell_calibrations]")
    args = parser.parse_args()
    start_world = (args.world_x, args.world_y)

    from fighter.helpers import load_cal
    cal = load_cal(args.screen)
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

    # Single read of every calibrated map from the DB. We mutate the
    # in-memory entries during the walk; `modified` flags gate which
    # ones get save()'d at the end.
    try:
        db_entries = load_map_data()
    except Exception as exc:
        print(f"[calibrate-paths] could not query map_data DB: {exc}")
        state.stop()
        sys.exit(1)

    by_world: dict[tuple[int, int], dict] = {}
    map_id_to_world: dict[int, tuple[int, int]] = {}
    for entry in db_entries.values():
        w = entry.get("world")
        mid = entry.get("map_id")
        if isinstance(mid, int) and isinstance(w, list) and len(w) == 2:
            key = (int(w[0]), int(w[1]))
            entry["modified"] = False
            by_world[key] = entry
            map_id_to_world[mid] = key

    def get_data(world: tuple[int, int]) -> dict:
        if world in by_world:
            return by_world[world]
        by_world[world] = {
            "world": list(world),
            "map_id": None,
            "cells": [],
            "switch_cells": {},
            "obstacles": [],
            "modified": False,
        }
        return by_world[world]

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
        saved = []
        for world, d in by_world.items():
            if not d.get("modified"):
                continue
            # Only switch_cells changes during this script -- keep the
            # save call narrow so we don't clobber resources/pois the
            # user may have recorded out-of-band.
            out = {
                "world": list(world),
                "map_id": d["map_id"],
                "switch_cells": d["switch_cells"],
            }
            save_map_data(out)
            saved.append((world, len(d["switch_cells"])))
        return saved

    try:
        while not stop["flag"]:
            snap = state.snapshot()
            # Proxy reports a new map -> commit pending click with the
            # new map_id as the destination.
            if (st["pending"] is not None
                    and snap.map_id
                    and snap.map_id != st["pending"]["src_map_id"]):
                commit_pending(snap.map_id)

            try:
                x, y, click_map_id = click_q.get(timeout=0.2)
            except queue.Empty:
                continue

            # Click was captured on a different map than the one we
            # track as current -> the transition happened but we
            # missed it in the snap poll. Use the click's map_id to
            # commit the pending direction, then fall through to
            # treat this click as a fresh action on the new map.
            if click_map_id and click_map_id != st["cur_map_id"]:
                if st["pending"] is not None:
                    commit_pending(click_map_id)
                else:
                    print(f"    click on {fmt_map(click_map_id)} but no "
                          f"pending direction from "
                          f"{fmt_map(st['cur_map_id'])} — can't infer "
                          f"world; click ignored.")
                    continue

            # If a pending is still around at this point, the previous
            # click was on the same map as this one and didn't trigger
            # a transition (otherwise commit_pending above would have
            # cleared it). Treat it as a non-switch click and drop it.
            if st["pending"] is not None:
                p = st["pending"]
                print(f"    previous cell={p['cell']} ({p['direction']}) "
                      f"didn't trigger a map change; replacing.")
                st["pending"] = None

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
