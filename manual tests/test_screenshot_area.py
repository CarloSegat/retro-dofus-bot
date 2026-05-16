"""Grab a single screenshot of an arbitrary rectangle and save it.

Usage:
    python3 test_screenshot_area.py                          # uses DEFAULT_AREA below
    python3 test_screenshot_area.py X1 Y1 X2 Y2              # custom area
    python3 test_screenshot_area.py X1 Y1 X2 Y2 PREP_SEC     # custom prep wait

Saves to debug_shots/<timestamp>.png and prints the path + the pixel size.
"""
import sys
import time
from pathlib import Path
import cv2
import mss
import numpy as np

DEFAULT_AREA = [[1621, 523], [1883, 578]]
DEFAULT_PREP = 2.0
OUT_DIR = Path(__file__).with_name("debug_shots")


def main():
    args = sys.argv[1:]
    if len(args) >= 4:
        area = [[int(args[0]), int(args[1])], [int(args[2]), int(args[3])]]
        prep = float(args[4]) if len(args) >= 5 else DEFAULT_PREP
    else:
        area = DEFAULT_AREA
        prep = float(args[0]) if args else DEFAULT_PREP

    (p1, p2) = area
    x1, y1 = min(p1[0], p2[0]), min(p1[1], p2[1])
    x2, y2 = max(p1[0], p2[0]), max(p1[1], p2[1])
    w, h = x2 - x1, y2 - y1
    print(f"area: ({x1},{y1})-({x2},{y2})  size: {w}x{h}")

    if prep > 0:
        print(f"switch to game in {prep:g}s...")
        time.sleep(prep)

    OUT_DIR.mkdir(exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    out_path = OUT_DIR / f"{ts}_{x1}-{y1}_{x2}-{y2}.png"

    with mss.mss() as sct:
        shot = np.array(sct.grab({"left": x1, "top": y1, "width": w, "height": h}))[:, :, :3]
    cv2.imwrite(str(out_path), shot)
    print(f"saved: {out_path}")


if __name__ == "__main__":
    main()
