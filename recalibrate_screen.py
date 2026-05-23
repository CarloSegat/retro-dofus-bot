"""Recompute pixel <-> cell calibration for a named screen.

The pixel calibration (origin_x, origin_y, cell_w, cell_h) depends on
the visible Dofus window's size and position, which differs between
hosts (host laptop vs docker VNC desktop vs whatever). This script
fits a fresh calibration for one named screen and stores it under
`config.json[cell_calibrations][<name>]`.

Pre-reqs:
  - Go proxy running on 127.0.0.1:9999 with my_id and my_cell populated.
  - Dofus visible and walkable.

Usage:
    python3 recalibrate_screen.py <name>

Examples:
    python3 recalibrate_screen.py host_ubuntu
    python3 recalibrate_screen.py docker_ubuntu

Flow:
  Stand still. The script asks for 4 clicks at the diamond's extremes
  (top row, bottom row, leftmost column, rightmost column). After each
  click the character walks; once the proxy reports `my_cell` settled
  on a new value, the (click_xy, new_cell) pair is recorded. The
  picked cell's (sub_row, pos) is echoed so you can see whether the
  click actually hit the requested edge. After all 4 pairs the script
  runs the least-squares fit from `dofus.cell_grid.fit_calibration`
  and writes the result.

  The 4 extremes are deliberate -- they give the longest possible
  lever arms for the fit, so small click-precision errors don't
  amplify into a misaligned grid the way they did with 4 clumped
  samples.

If config.json has no `default_screen` yet, it is set to <name>.
"""
import argparse
import json
import queue
import sys
import time
from datetime import datetime
from pathlib import Path

from pynput import keyboard, mouse

from dofus.cell_grid import cell_to_subrow_pos, fit_calibration
from dofus.proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
PROXY_ADDR = "127.0.0.1:9999"

CELL_SETTLE_SEC = 1.0      # require my_cell stable for this long
CELL_SETTLE_TIMEOUT = 30.0 # max wait for a walk to finish

# 4 extremes of the playable diamond. Order matters only for the prompt;
# the fit is symmetric.
EXTREME_PROMPTS = [
    ("TOP",    "topmost row of the diamond (north tip)"),
    ("BOTTOM", "bottommost row of the diamond (south tip)"),
    ("LEFT",   "leftmost column (west tip)"),
    ("RIGHT",  "rightmost column (east tip)"),
]


def wait_for_new_cell(state, prev_cell):
    """Wait until snap.my_cell differs from prev_cell and stays stable
    for CELL_SETTLE_SEC. Returns the new cell, or None on timeout."""
    deadline = time.time() + CELL_SETTLE_TIMEOUT
    last_seen = state.snapshot().my_cell
    settled_since = None
    while time.time() < deadline:
        cur = state.snapshot().my_cell
        if cur != prev_cell and cur != 0:
            if cur == last_seen:
                if settled_since is None:
                    settled_since = time.time()
                elif time.time() - settled_since >= CELL_SETTLE_SEC:
                    return cur
            else:
                last_seen = cur
                settled_since = time.time()
        else:
            settled_since = None
        time.sleep(0.1)
    return None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="screen name to store under, e.g. host_ubuntu")
    args = parser.parse_args()

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[recalibrate-screen] connecting to proxy at {PROXY_ADDR}...")
    deadline = time.time() + 5
    while time.time() < deadline and not state.snapshot().connected:
        time.sleep(0.1)
    if not state.snapshot().connected:
        print("[recalibrate-screen] proxy not reachable. Is it running?")
        sys.exit(1)
    deadline = time.time() + 5
    while time.time() < deadline and state.snapshot().my_cell == 0:
        time.sleep(0.1)
    snap = state.snapshot()
    if snap.my_cell == 0:
        print("[recalibrate-screen] proxy hasn't reported my_cell. Walk "
              "once inside Dofus so the proxy sees a GA;1; event.")
        sys.exit(1)
    print(f"[recalibrate-screen] starting at my_cell={snap.my_cell} "
          f"map={snap.map_id}")

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

    print(f"\n[recalibrate-screen] click the 4 extremes of the visible "
          f"diamond. Wait for the character to settle on each cell "
          f"before clicking the next. Esc to abort.\n")

    pairs: list[tuple[tuple[int, int], int]] = []
    sample_idx = 0
    while sample_idx < len(EXTREME_PROMPTS) and not stop["flag"]:
        label, hint = EXTREME_PROMPTS[sample_idx]
        prev_cell = state.snapshot().my_cell
        print(f"  sample {sample_idx + 1}/4 [{label}]: click a walkable "
              f"cell on the {hint}. Standing on cell {prev_cell}.")
        try:
            xy = click_q.get(timeout=120)
        except queue.Empty:
            print("    no click in 120s; aborting.")
            stop["flag"] = True
            break
        new_cell = wait_for_new_cell(state, prev_cell)
        if new_cell is None:
            print(f"    walk never settled on a new cell within "
                  f"{CELL_SETTLE_TIMEOUT:.0f}s (obstacle? same cell?); "
                  f"retrying the same extreme -- try a clearly walkable "
                  f"cell at the {label} edge.")
            continue
        sub_row, pos = cell_to_subrow_pos(new_cell)
        pairs.append((xy, new_cell))
        sample_idx += 1
        print(f"    click={xy} -> cell {new_cell} (sub_row={sub_row}, "
              f"pos={pos}) recorded for {label}")

    mouse_listener.stop()
    key_listener.stop()
    state.stop()

    if stop["flag"] or len(pairs) < len(EXTREME_PROMPTS):
        print("[recalibrate-screen] aborted before all 4 extremes were "
              "captured; nothing written.")
        sys.exit(1)

    fit = fit_calibration(pairs)
    fit["fitted_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[recalibrate-screen] fit for screen={args.name!r}:")
    print(f"  origin=({fit['origin_x']:.2f}, {fit['origin_y']:.2f})")
    print(f"  cell={fit['cell_w']:.2f}x{fit['cell_h']:.2f}")
    print(f"  residual_px={fit['residual_px']:.2f} (lower is better; "
          f"<10 is good, >20 indicates a bad sample)")
    print(f"  samples={fit['samples']}")

    resp = input(f"\nwrite to config.json[cell_calibrations][{args.name!r}]? "
                 f"[Y/n] ").strip().lower()
    if resp and resp not in ("y", "yes"):
        print("[recalibrate-screen] aborted; nothing written.")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    cals = cfg.setdefault("cell_calibrations", {})
    cals[args.name] = fit
    if not cfg.get("default_screen"):
        cfg["default_screen"] = args.name
        print(f"[recalibrate-screen] default_screen was unset; pinning it "
              f"to {args.name!r}.")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[recalibrate-screen] wrote {args.name} to {CONFIG_PATH.name}.")


if __name__ == "__main__":
    main()
