"""Calibrate the cell-id -> screen-pixel transform.

Pre-reqs:
  - Go proxy is up and publishing on 127.0.0.1:9999.
  - Dofus is in-world and the proxy has captured your character id (ASK packet).

Usage:
    python3 calibrate_cells.py [N]

Default N=5 calibration clicks. For each click:
  1. Click somewhere on the map in Dofus (the character will walk there).
  2. The calibrator waits until the proxy reports a stable destination cell.
  3. It pairs your click position with the cell id.

After N clicks it fits (origin_x, origin_y, cell_w, cell_h) by least-squares
and writes them under cell_calibration in config.json. The bot uses that
transform to convert mob-group cells (from the proxy) to screen pixels.

Tips for a good fit:
  - Spread clicks across the map (corners + center).
  - Don't click in town (you'll teleport instead of walking).
  - If you click on a non-walkable cell, no my_cell change fires; just try
    again. The calibrator will time out and re-prompt.

Esc anywhere stops the calibrator.
"""
import json
import queue
import sys
import time
from pathlib import Path

from pynput import keyboard, mouse

from cell_grid import cell_to_xy, fit_calibration
from proxy_client import ProxyState

CONFIG_PATH = Path(__file__).with_name("config.json")
PROXY_ADDR = "127.0.0.1:9999"
PER_CLICK_TIMEOUT = 12.0   # max seconds to wait for a cell change after a click
STABLE_QUIET_SEC = 1.2     # cell must stop changing for this long to count as "arrived"


def wait_for_stable_cell(state: ProxyState, start_cell: int, timeout: float) -> int | None:
    """Block until the proxy's my_cell differs from start_cell and then
    stays put for STABLE_QUIET_SEC. Returns the new cell, or None on timeout."""
    deadline = time.time() + timeout
    last_seen = start_cell
    last_change = time.time()
    while time.time() < deadline:
        snap = state.snapshot()
        cur = snap.my_cell
        if cur != last_seen:
            last_seen = cur
            last_change = time.time()
        if last_seen != start_cell and time.time() - last_change >= STABLE_QUIET_SEC:
            return last_seen
        time.sleep(0.1)
    return None


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[calibrate] connecting to proxy at {PROXY_ADDR}...")
    deadline = time.time() + 5
    while time.time() < deadline and not state.snapshot().connected:
        time.sleep(0.1)
    if not state.snapshot().connected:
        print("[calibrate] not connected to proxy. Is `sudo go run ./cmd/proxy` running?")
        sys.exit(1)
    # Wait up to 5s for the proxy's cached state snapshot to arrive and
    # populate my_id. With the replay fix in eventHub, this should be ~100ms.
    deadline = time.time() + 5
    while time.time() < deadline and state.snapshot().my_id == 0:
        time.sleep(0.1)
    snap = state.snapshot()
    if snap.my_id == 0:
        if snap.last_event_ts == 0:
            print("[calibrate] no state events received from proxy.")
            print("            The proxy is listening but hasn't parsed any game packets.")
            print("            Restart Dofus while the proxy is already running.")
        else:
            print("[calibrate] proxy is talking, but never saw ASK (character id unknown).")
            print("            Close Dofus completely, then re-launch with the proxy running.")
        sys.exit(1)
    print(f"[calibrate] my_id={snap.my_id} current my_cell={snap.my_cell}")

    click_q: queue.Queue = queue.Queue()
    stop = {"flag": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left:
            click_q.put((x, y))

    def on_key(key):
        if key == keyboard.Key.esc:
            stop["flag"] = True
            return False

    mouse_listener = mouse.Listener(on_click=on_click)
    key_listener = keyboard.Listener(on_press=on_key)
    mouse_listener.start()
    key_listener.start()

    print(f"\n[calibrate] need {n} clicks. Click cells spread across the map (corners + center).")
    print("[calibrate] DON'T click in town (you'll teleport). Esc to abort.\n")

    pairs = []
    while len(pairs) < n and not stop["flag"]:
        idx = len(pairs) + 1
        starting = state.snapshot().my_cell
        print(f"  click {idx}/{n}: standing on cell {starting}. Click anywhere on the map...")
        try:
            xy = click_q.get(timeout=60)
        except queue.Empty:
            print("    no click received in 60s, aborting.")
            break
        print(f"    clicked screen=({xy[0]}, {xy[1]}); waiting for character to arrive...")
        dest = wait_for_stable_cell(state, starting, PER_CLICK_TIMEOUT)
        if dest is None:
            print(f"    no cell change within {PER_CLICK_TIMEOUT}s -- try a different spot.")
            continue
        pairs.append((xy, dest))
        print(f"    -> arrived at cell {dest}.   ({len(pairs)}/{n} good clicks)")

    mouse_listener.stop()
    key_listener.stop()

    if stop["flag"]:
        print("[calibrate] aborted by user.")
        sys.exit(1)
    if len(pairs) < 2:
        print("[calibrate] not enough samples to fit a transform.")
        sys.exit(1)

    print(f"\n[calibrate] fitting transform from {len(pairs)} samples...")
    fit = fit_calibration(pairs)
    print(f"  origin_x = {fit['origin_x']:.2f}")
    print(f"  origin_y = {fit['origin_y']:.2f}")
    print(f"  cell_w   = {fit['cell_w']:.2f}")
    print(f"  cell_h   = {fit['cell_h']:.2f}")
    print(f"  residual = {fit['residual_px']:.2f} px (RMS)")

    if fit["residual_px"] > 30:
        print("  WARNING: large residual. Re-run with clicks spread further apart.")

    # Round-trip predictions for the user to eyeball.
    print("\n[calibrate] predicted vs actual screen positions:")
    for xy, cell in pairs:
        px, py = cell_to_xy(cell, fit["origin_x"], fit["origin_y"], fit["cell_w"], fit["cell_h"])
        dx, dy = px - xy[0], py - xy[1]
        print(f"  cell {cell:>4} -> predict=({px},{py}) clicked=({xy[0]},{xy[1]})  delta=({dx:+},{dy:+})")

    # Persist.
    cfg = json.loads(CONFIG_PATH.read_text())
    cfg["cell_calibration"] = {
        "origin_x": fit["origin_x"],
        "origin_y": fit["origin_y"],
        "cell_w": fit["cell_w"],
        "cell_h": fit["cell_h"],
        "residual_px": fit["residual_px"],
        "samples": fit["samples"],
        "fitted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"\n[calibrate] saved to {CONFIG_PATH.name} under 'cell_calibration'.")

    state.stop()


if __name__ == "__main__":
    main()
