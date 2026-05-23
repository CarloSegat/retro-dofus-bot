"""Capture the in-fight turn-order bar collapse-toggle (`<>`) position.

During a fight the top-right of the screen shows a turn-order bar with
character portraits. It can be collapsed by clicking its `<>` symbol,
freeing up map cells covered by it. The orchestrator clicks this
calibrated position on every fight engage. Pixel position depends on
screen geometry, so each visible environment (docker VNC desktop, host
laptop, ...) needs its own calibration -- mirroring `cell_calibrations`
and `restart_clicks`. Stored under
`config.json[fight_ui_dismiss_clicks][<screen_name>][collapse_turn_order]`.

Pre-req: a fight must currently be engaged in the Dofus client so the
turn-order bar is visible and the `<>` toggle is on screen.

Usage:
    python3 calibrate_fight_ui_dismiss.py <screen_name>

Example:
    python3 calibrate_fight_ui_dismiss.py docker_ubuntu

Flow:
  Press Enter to arm the listener, then click the `<>` toggle of the
  turn-order bar. After the click the listener stops and the position
  is written.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pynput import mouse

CONFIG_PATH = Path(__file__).with_name("config.json")
LISTENER_TIMEOUT_SEC = 300.0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="screen name to store under, e.g. docker_ubuntu")
    args = parser.parse_args()
    name = args.name

    print(f"[fight-ui-cal] will capture the turn-order `<>` collapse "
          f"position for screen={name!r} and save under "
          f"config.json[fight_ui_dismiss_clicks][{name!r}]"
          f"[collapse_turn_order].\n")
    print("Pre-req: be in an active fight so the turn-order bar with "
          "its `<>` toggle is visible.\n")
    input("Press Enter when ready, then click the `<>` toggle...")

    captured: list[tuple[int, int]] = []

    def on_click(x, y, button, pressed):
        if not pressed or button != mouse.Button.left:
            return
        print(f"    captured click at ({x},{y})")
        captured.append((x, y))
        return False

    listener = mouse.Listener(on_click=on_click)
    listener.start()
    listener.join(timeout=LISTENER_TIMEOUT_SEC)

    if not captured:
        print(f"[fight-ui-cal] no click within {LISTENER_TIMEOUT_SEC:.0f}s; "
              f"aborting.")
        sys.exit(1)

    x, y = captured[0]
    record = {
        "collapse_turn_order": [x, y],
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"\n[fight-ui-cal] collapse_turn_order = {record['collapse_turn_order']}")
    resp = input(f"\nwrite to config.json[fight_ui_dismiss_clicks][{name!r}]? "
                 f"[Y/n] ").strip().lower()
    if resp and resp not in ("y", "yes"):
        print("[fight-ui-cal] aborted; nothing written.")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    cfg.setdefault("fight_ui_dismiss_clicks", {})[name] = record
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[fight-ui-cal] wrote fight_ui_dismiss_clicks[{name}] to "
          f"{CONFIG_PATH.name}.")


if __name__ == "__main__":
    main()
