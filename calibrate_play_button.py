"""Capture the Ankama Launcher PLAY-button click position.

The standalone restart script (`scripts/restart_dofus.py`) needs to auto-click
PLAY in the launcher after killing/relaunching a hung Dofus client.
Pixel position depends on screen geometry, so each visible environment
(docker VNC desktop, host laptop, ...) needs its own calibration --
mirroring `cell_calibrations`. Stored under
`config.json[restart_clicks][<screen_name>][play_button]`.

Pre-reqs: open the Ankama Launcher in the desktop manually, then
maximize it (e.g. xdotool windowmove + windowsize 100% 100% on the
"Ankama Launcher" window) so the calibrated position matches what
`scripts/restart_dofus.py` will see after it maximizes the launcher itself.

Usage:
    python3 calibrate_play_button.py <screen_name>

Example:
    python3 calibrate_play_button.py docker_ubuntu

Flow:
  Press Enter to arm the listener, then click the PLAY button in the
  maximized launcher. After the click the listener stops and the
  position is written.
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

    print(f"[play-cal] will capture the PLAY-button position for "
          f"screen={name!r} and save under "
          f"config.json[restart_clicks][{name!r}][play_button].\n")
    print("Pre-req: the Ankama Launcher must be open and maximized "
          "(the restart script maximizes it the same way before "
          "clicking, so calibration only stays valid against that "
          "geometry).\n")
    input("Press Enter when ready, then click the PLAY button...")

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
        print(f"[play-cal] no click within {LISTENER_TIMEOUT_SEC:.0f}s; "
              f"aborting.")
        sys.exit(1)

    x, y = captured[0]
    record = {
        "play_button": [x, y],
        "calibrated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"\n[play-cal] play_button = {record['play_button']}")
    resp = input(f"\nwrite to config.json[restart_clicks][{name!r}]? "
                 f"[Y/n] ").strip().lower()
    if resp and resp not in ("y", "yes"):
        print("[play-cal] aborted; nothing written.")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    cfg.setdefault("restart_clicks", {})[name] = record
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[play-cal] wrote restart_clicks[{name}] to {CONFIG_PATH.name}.")


if __name__ == "__main__":
    main()
