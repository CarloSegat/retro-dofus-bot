"""Hunter mode: proxy-driven mob targeting.

Pre-reqs:
  - Go proxy is up on 127.0.0.1:9999.
  - Dofus is in-world; proxy has captured your character id (ASK packet).
  - config.json has a valid `cell_calibration` (run calibrate_cells.py).

Loop:
  1. Read proxy snapshot.
  2. If in_fight: run combat cycle, wait for fight_end (proxy), press Esc
     to dismiss the win popup, verify no menu is up, continue.
  3. Else if any mob_groups visible: pick first, convert cell to screen via
     calibration, click. Wait for fight_start (proxy). Loop back.
  4. Else: idle 3s then re-scan (no cross-map roaming in this iteration).

Move mouse to top-left of screen for pyautogui FAILSAFE abort.
"""
import sys
import time
from pathlib import Path

import mss
import pyautogui

from cell_grid import cell_to_xy
from dialogs import ensure_safe_to_resume
from fight import run_combat_cycle  # TODO(auto-fighter): replace with proxy-driven combat using fight_entities
from proxy_client import ProxyState
from utils import CFG, make_ctx

pyautogui.PAUSE = 0.05
pyautogui.FAILSAFE = True

PROXY_ADDR = "127.0.0.1:9999"
FIGHT_START_TIMEOUT = 15.0   # seconds to wait for proxy fight_start after clicking a mob
FIGHT_END_TIMEOUT = 600.0    # max single-fight duration


def load_calibration():
    cal = CFG.get("cell_calibration")
    if not cal:
        print("[hunter] missing cell_calibration in config.json. Run calibrate_cells.py.")
        sys.exit(1)
    return cal


def cell_to_screen(cell, cal):
    return cell_to_xy(cell, cal["origin_x"], cal["origin_y"], cal["cell_w"], cal["cell_h"])


def wait_for(state: ProxyState, predicate, timeout: float, poll: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate(state.snapshot()):
            return True
        time.sleep(poll)
    return False


def wait_proxy_ready(state: ProxyState, timeout: float = 10.0):
    if not wait_for(state, lambda s: s.connected and s.my_id != 0, timeout):
        snap = state.snapshot()
        print(f"[hunter] proxy not ready: connected={snap.connected} my_id={snap.my_id}")
        print("        is `sudo go run ./cmd/proxy` up? Did Dofus log in through it?")
        sys.exit(1)


def main():
    cal = load_calibration()
    print(f"[hunter] calibration: origin=({cal['origin_x']:.1f},{cal['origin_y']:.1f}) "
          f"cell={cal['cell_w']:.2f}x{cal['cell_h']:.2f} residual={cal['residual_px']:.1f}px")

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[hunter] connecting to proxy at {PROXY_ADDR}...")
    wait_proxy_ready(state)
    snap = state.snapshot()
    print(f"[hunter] ready: my_id={snap.my_id} my_cell={snap.my_cell} map={snap.map_id}")

    with mss.mss() as sct:
        ctx = make_ctx(sct)

        while True:
            snap = state.snapshot()

            if snap.in_fight:
                print(f"[hunter] already in fight, running combat cycle")
                run_combat_cycle(ctx)
                if not wait_for(state, lambda s: not s.in_fight, FIGHT_END_TIMEOUT):
                    print(f"[hunter] fight_end never arrived within {FIGHT_END_TIMEOUT}s; aborting")
                    sys.exit(1)
                time.sleep(1.0)
                pyautogui.press("esc")
                time.sleep(0.3)
                if not ensure_safe_to_resume(ctx):
                    print("[hunter] menu still open after Esc -- aborting")
                    sys.exit(1)
                continue

            if snap.mobs:
                cell = next(iter(snap.mobs.keys()))
                mob = snap.mobs[cell]
                x, y = cell_to_screen(cell, cal)
                print(f"[hunter] engaging cell={cell} group={mob.group_id} "
                      f"members={mob.members} -> screen=({x},{y})")
                ctx.click(x, y)
                if wait_for(state, lambda s: s.in_fight, FIGHT_START_TIMEOUT):
                    print(f"[hunter] fight_start received")
                else:
                    print(f"[hunter] click on cell {cell} didn't start a fight in "
                          f"{FIGHT_START_TIMEOUT}s; retrying in 3s")
                    time.sleep(3.0)
                continue

            print(f"[hunter] no mobs on map {snap.map_id}, idling 3s...")
            time.sleep(3.0)


if __name__ == "__main__":
    main()
