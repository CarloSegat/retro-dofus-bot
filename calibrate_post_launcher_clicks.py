"""Capture server + character click positions for scripts/restart_dofus.py.

After `scripts/restart_dofus.py` clicks PLAY on the maximized Ankama Launcher
and the "Dofus Retro" window comes up, the game shows:

  1. "Choose a server"     -- always
  2. "Choose your character" -- ONLY when no character is already
                              mid-fight; otherwise the game auto-
                              selects after server selection

This script captures the click position for each. Both are stored
under the same `config.json[restart_clicks][<screen_name>]` entry
that `calibrate_play_button.py` writes to, alongside the existing
`play_button` key.

Pre-req: the maximized Dofus Retro window is on "Choose a server".
Easiest way to get there: run `scripts/restart_dofus.py` first (it will
restart the client and stop when "Dofus Retro" appears -- if you've
not yet calibrated server/character, the script's later server-click
step is a no-op anyway).

Usage:
    python3 calibrate_post_launcher_clicks.py <screen_name>

Example:
    python3 calibrate_post_launcher_clicks.py docker_ubuntu

Flow (per slot):
  1. Script prints the instruction and arms the click listener.
  2. You click the target ONCE (single left-click).
  3. Script records the position, then waits for you to press Enter.
  4. Between Enter presses, you do whatever it takes to advance the
     game to the next screen (e.g. double-click the server card,
     click the "Select" button). The listener is paused during this
     phase so those navigation clicks are NOT captured.
"""
import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from pynput import mouse

CONFIG_PATH = Path(__file__).with_name("config.json")
LISTENER_TIMEOUT_SEC = 300.0

SLOTS = [
    ("server_first",
     "On the 'Choose a server' screen, click ONCE on the server card "
     "you want to connect to (Allisteria for berlinthree). The runtime "
     "will double-click this position to enter."),
    ("character_first",
     "After navigating to 'Choose your character' (double-click the "
     "server card you just calibrated, or click 'Select'), click ONCE "
     "on the character you want to play. Dofus always puts the "
     "last-played character on the far left, so calibration there is "
     "robust across sessions."),
]


def capture_click(slot_name):
    """Arm a one-shot pynput listener; return (x, y) or None on timeout."""
    captured = []

    def on_click(x, y, button, pressed):
        if not pressed or button != mouse.Button.left:
            return
        captured.append((x, y))
        return False

    listener = mouse.Listener(on_click=on_click)
    listener.start()
    listener.join(timeout=LISTENER_TIMEOUT_SEC)
    if not captured:
        return None
    return captured[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("name", help="screen name, e.g. docker_ubuntu")
    args = parser.parse_args()
    name = args.name

    print(f"[post-launcher-cal] capturing {len(SLOTS)} click positions "
          f"for screen={name!r}; saving under "
          f"config.json[restart_clicks][{name!r}].\n")

    record = {}
    for idx, (key, instruction) in enumerate(SLOTS, start=1):
        print(f"--- slot {idx}/{len(SLOTS)}: {key} ---")
        print(instruction)
        input("Press Enter when you're ready to single-click the target...")
        print("Listening for one left-click...")
        pos = capture_click(key)
        if pos is None:
            print(f"[post-launcher-cal] no click captured within "
                  f"{LISTENER_TIMEOUT_SEC:.0f}s; aborting.")
            sys.exit(1)
        x, y = pos
        record[key] = [x, y]
        print(f"  captured {key} = ({x},{y})\n")

    print("[post-launcher-cal] captured all positions:")
    for k in record:
        print(f"  {k} = {record[k]}")

    resp = input(f"\nmerge into config.json[restart_clicks][{name!r}]? "
                 f"[Y/n] ").strip().lower()
    if resp and resp not in ("y", "yes"):
        print("[post-launcher-cal] aborted; nothing written.")
        sys.exit(1)

    cfg = json.loads(CONFIG_PATH.read_text())
    section = cfg.setdefault("restart_clicks", {}).setdefault(name, {})
    section.update(record)
    section["post_launcher_calibrated_at"] = datetime.now().strftime(
        "%Y-%m-%d %H:%M:%S")
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2) + "\n")
    print(f"[post-launcher-cal] merged server_first + character_first "
          f"into restart_clicks[{name}].")


if __name__ == "__main__":
    main()
