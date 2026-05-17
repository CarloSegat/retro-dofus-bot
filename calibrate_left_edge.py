"""One-off: click 6 cells along the LEFT edge of the playable diamond
top-to-bottom. For each click print the resolved cell id and its NW/SW
neighbours. The goal is to find cells that `on_map` currently flags as
walkable but lie outside the diamond -- those are the cells the bot
retreats into and gets stuck on (e.g. cell 174 = sub_row 12, pos 0).

Usage:
    python3 calibrate_left_edge.py

Click order: top-to-bottom along the visible left edge of the diamond,
6 cells. The script prints for each click:
  - the resolved cell id, (sub_row, pos)
  - on_map() verdict
  - the NW and SW neighbours and whether on_map currently keeps them

After all 6 clicks it summarises the set of cells the bot believes are
on-map but never showed up as a click target -- those are the cells
likely to be silently off-canvas.
"""
import json
from pathlib import Path

from pynput import mouse

from cell_grid import (
    cell_to_subrow_pos,
    cell_to_xy,
    neighbors,
    on_map,
    xy_to_cell,
)

CONFIG_PATH = Path(__file__).with_name("config.json")
N_CLICKS = 6
NEIGHBOR_LABELS = ("NE", "SE", "NW", "SW")


def describe(cell):
    sr, pos = cell_to_subrow_pos(cell)
    flag = "on_map" if on_map(cell) else "OFF"
    return f"cell={cell} (sr={sr},pos={pos}) {flag}"


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    cal = cfg.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        return 1
    ox, oy = cal["origin_x"], cal["origin_y"]
    cw, ch = cal["cell_w"], cal["cell_h"]
    print(f"calibration: origin=({ox:.1f},{oy:.1f}) cell={cw:.2f}x{ch:.2f}")
    print(f"\nClick {N_CLICKS} cells along the LEFT edge of the diamond, "
          f"top-to-bottom.\n")

    clicks = []
    pending = {"want": False}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left and pending["want"]:
            clicks.append((x, y))
            pending["want"] = False

    listener = mouse.Listener(on_click=on_click)
    listener.start()

    clicked_cells = []
    for i in range(N_CLICKS):
        pending["want"] = True
        print(f"  -> click left-edge cell {i + 1}/{N_CLICKS} ...")
        while pending["want"]:
            try:
                listener.join(timeout=0.1)
            except KeyboardInterrupt:
                listener.stop()
                return 1
            if not listener.running:
                return 1
        x, y = clicks[-1]
        cell, residual = xy_to_cell(x, y, ox, oy, cw, ch)
        sr, pos = cell_to_subrow_pos(cell)
        px, py = cell_to_xy(cell, ox, oy, cw, ch)
        clicked_cells.append(cell)
        print(f"     click=({x},{y})  residual={residual:.1f}px")
        print(f"     -> {describe(cell)}  center=({px},{py}) "
              f"dx={x - px} dy={y - py}")
        for label, n in zip(NEIGHBOR_LABELS, neighbors(cell)):
            note = ""
            if label in ("NW", "SW"):
                note = "  <- candidate off-map (W of left-edge click)"
            print(f"        {label}: {describe(n)}{note}")
        print()

    listener.stop()

    print("=" * 60)
    print("SUMMARY:")
    print(f"  clicked cells (top->bottom): {clicked_cells}")
    print()
    print("Look at the NW/SW neighbours above. Any cell flagged 'on_map'")
    print("there but that you would NOT click as a valid game cell is what")
    print("on_map() should be tightened to reject.")
    print()
    print("Common patterns to look for:")
    print("  * even sub_row, pos=0: leftmost column of even rows -- currently")
    print("    on_map=True. If these are visually outside the diamond, add")
    print("    a filter like `sub_row even AND pos==0 AND sub_row < K` or")
    print("    similar to on_map().")
    print("  * odd sub_row, pos=1: leftmost valid column of odd rows. These")
    print("    should match your click cells if the left edge sits there.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
