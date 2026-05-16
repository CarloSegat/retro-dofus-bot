"""Press Esc after a 3s countdown. Isolates the post-fight Esc from
everything else (no proxy, no fight loop) so we can see whether ANY of
our key-injection paths closes the XP-summary popup.

Usage:
  python3 esc_debug.py                 # default: press_xdotool (--window targeted)
  python3 esc_debug.py --mode raw      # xdotool key Escape (no --window, focus-dependent)
  python3 esc_debug.py --mode focus    # xdotool windowactivate + xdotool key Escape
  python3 esc_debug.py --mode pyautogui # pyautogui keyDown/keyUp 'esc' (the old path)

Procedure:
  1. Trigger the popup manually (finish a fight, or open any dialog).
  2. Click back into your terminal so focus is NOT on Dofus.
  3. Run the script. You have 3 seconds before it fires.
  4. Watch the popup. If it closes, that mode works.
"""
import argparse
import subprocess
import sys
import time

from utils import dofus_window_id, press_xdotool

import pyautogui


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=["xdotool", "raw", "focus", "pyautogui"],
                   default="xdotool",
                   help="how to send Escape (default: xdotool = press_xdotool with --window)")
    p.add_argument("--delay", type=float, default=3.0)
    args = p.parse_args()

    wid = dofus_window_id()
    print(f"[esc_debug] mode={args.mode} dofus_window_id={wid}")
    if wid is None and args.mode in ("xdotool", "focus"):
        print("[esc_debug] WARN: couldn't find a 'Dofus Retro' window; check xdotool search --name 'Dofus Retro'")

    for i in range(int(args.delay), 0, -1):
        print(f"  firing in {i}...")
        time.sleep(1.0)

    print(f"[esc_debug] firing Escape ({args.mode})")
    if args.mode == "xdotool":
        press_xdotool("Escape")
    elif args.mode == "raw":
        subprocess.run(["xdotool", "key", "Escape"], check=True)
    elif args.mode == "focus":
        if wid:
            subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=True)
            time.sleep(0.1)
        subprocess.run(["xdotool", "key", "Escape"], check=True)
    elif args.mode == "pyautogui":
        pyautogui.keyDown("esc")
        time.sleep(0.1)
        pyautogui.keyUp("esc")
    print("[esc_debug] done. Did the popup close?")


if __name__ == "__main__":
    sys.exit(main())
