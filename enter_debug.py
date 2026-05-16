"""Press Enter after a 3s countdown. Isolates the post-fight Enter (the
one that should dismiss the "Ready to fight again?" prompt after Esc
closes the XP summary) from everything else, so we can see whether ANY
of our key-injection paths reaches the game.

Usage:
  python3 enter_debug.py                  # default: press_xdotool (--window targeted)
  python3 enter_debug.py --mode raw       # xdotool key Return (no --window, focus-dependent)
  python3 enter_debug.py --mode focus     # xdotool windowactivate + xdotool key Return
  python3 enter_debug.py --mode pyautogui # pyautogui keyDown/keyUp 'enter' (the old path)

Procedure:
  1. Trigger the popup manually (finish a fight; press Esc to close the
     XP summary so the follow-up prompt is up).
  2. Click back into your terminal so focus is NOT on Dofus.
  3. Run the script. You have 3 seconds before it fires.
  4. Watch the prompt. If it dismisses, that mode works.

X11 key name for Enter is "Return" (xdotool) / "enter" (pyautogui)."""
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
                   help="how to send Enter (default: xdotool = press_xdotool with --window)")
    p.add_argument("--delay", type=float, default=3.0)
    args = p.parse_args()

    wid = dofus_window_id()
    print(f"[enter_debug] mode={args.mode} dofus_window_id={wid}")
    if wid is None and args.mode in ("xdotool", "focus"):
        print("[enter_debug] WARN: couldn't find a 'Dofus Retro' window; check xdotool search --name 'Dofus Retro'")

    for i in range(int(args.delay), 0, -1):
        print(f"  firing in {i}...")
        time.sleep(1.0)

    print(f"[enter_debug] firing Enter ({args.mode})")
    if args.mode == "xdotool":
        press_xdotool("Return")
    elif args.mode == "raw":
        subprocess.run(["xdotool", "key", "Return"], check=True)
    elif args.mode == "focus":
        if wid:
            subprocess.run(["xdotool", "windowactivate", "--sync", wid], check=True)
            time.sleep(0.1)
        subprocess.run(["xdotool", "key", "Return"], check=True)
    elif args.mode == "pyautogui":
        pyautogui.keyDown("enter")
        time.sleep(0.1)
        pyautogui.keyUp("enter")
    print("[enter_debug] done. Did the prompt dismiss?")


if __name__ == "__main__":
    sys.exit(main())
