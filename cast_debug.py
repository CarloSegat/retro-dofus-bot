"""Debug: cast the slot-2 spell exactly once, 5 seconds after launch.

Isolates the press('2') + click(cursor_pos) sequence from the rest of
main.py so we can pin down whether the cast pipeline itself works, with
no proxy / no walk logic in the way.

Procedure:
  1. In Dofus: be in a fight, on your turn, with an enemy adjacent.
  2. From auto-fighter/:  python3 -u cast_debug.py
  3. Within 5s, click on the Dofus window so it has focus and hover
     the mouse over the enemy cell.
  4. The script will then:
       - print the cursor position once per second during the countdown
       - press '2' once
       - wait `--settle` seconds (default 0.4) for Dofus to show the
         spell-aim reticle
       - left-click at the cursor position captured at countdown end

Optional flags:
  --hotkey K     spell hotkey to press     (default: config sacrid_dissolution_hotkey, fall back to "2")
  --wait N       countdown seconds         (default: 5)
  --settle S     pause between press+click (default: 0.4)
  --no-press     skip the spell key press, just click (test click alone)
  --no-click     skip the click, just press the spell (test press alone)
"""
import argparse
import time

from utils import CFG, click, cursor_pos, press


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--hotkey", default=CFG.get("sacrid_dissolution_hotkey") or "2")
    p.add_argument("--wait", type=int, default=5)
    p.add_argument("--settle", type=float, default=0.4)
    p.add_argument("--no-press", dest="do_press", action="store_false")
    p.add_argument("--no-click", dest="do_click", action="store_false")
    args = p.parse_args()

    print(
        f"[cast_debug] hotkey={args.hotkey!r} wait={args.wait}s settle={args.settle}s "
        f"press={args.do_press} click={args.do_click}"
    )
    print(f"[cast_debug] position cursor on the target in Dofus.")
    for i in range(args.wait, 0, -1):
        x, y = cursor_pos()
        print(f"  {i}s... cursor at ({x},{y})")
        time.sleep(1)

    x, y = cursor_pos()
    print(f"[cast_debug] firing. cursor captured at ({x},{y})")

    if args.do_press:
        print(f"  PRESS {args.hotkey!r}")
        press(args.hotkey)
    else:
        print("  (skipping press)")

    print(f"  settle {args.settle}s")
    time.sleep(args.settle)

    if args.do_click:
        print(f"  CLICK ({x},{y})")
        click(x, y)
    else:
        print("  (skipping click)")

    print("[cast_debug] done. check Dofus: did the spell fire? did AP drop?")


if __name__ == "__main__":
    main()
