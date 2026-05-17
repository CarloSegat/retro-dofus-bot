"""One-off: click the 4 extreme cells of the playable diamond and see
what cell IDs the current calibration / grid math assigns them.

The hypothesis: cells the model thinks are on-map (per cell_grid.on_map)
are actually outside the visual diamond and not walkable. If that's true,
retreat steps into those cells silently fail forever (bot gets stuck in
corners). This script does NOT need the proxy -- pure calibration check.

Usage:
    python3 calibrate_map_extremes.py

Click order:
    1. Topmost cell    (point of the diamond at the TOP)
    2. Bottommost cell (point of the diamond at the BOTTOM)
    3. Leftmost cell   (point of the diamond at the LEFT)
    4. Rightmost cell  (point of the diamond at the RIGHT)

For each click prints:
  - raw (x, y)
  - inferred cell id, sub_row, pos
  - residual px between click and cell center (high = click was between cells)
  - on_map() verdict
  - iso (u, v) coordinates
  - the cell-center (x, y) predicted by cell_to_xy (round-trip)

Then prints a summary highlighting any cell that is on_map=True but lies
visually at a diamond corner -- those are the cells that need filtering.
"""
import json
from pathlib import Path

from pynput import mouse

from cell_grid import (
    cell_to_subrow_pos,
    cell_to_uv,
    cell_to_xy,
    on_map,
    xy_to_cell,
)

CONFIG_PATH = Path(__file__).with_name("config.json")
EXTREMES = ["TOPMOST", "BOTTOMMOST", "LEFTMOST", "RIGHTMOST"]


def main():
    cfg = json.loads(CONFIG_PATH.read_text())
    cal = cfg.get("cell_calibration")
    if not cal:
        print("missing cell_calibration in config.json.")
        return 1

    ox, oy = cal["origin_x"], cal["origin_y"]
    cw, ch = cal["cell_w"], cal["cell_h"]
    print(f"calibration: origin=({ox:.1f},{oy:.1f}) cell={cw:.2f}x{ch:.2f}")

    results = []
    clicks_pending = {"want": True}

    def on_click(x, y, button, pressed):
        if pressed and button == mouse.Button.left and clicks_pending["want"]:
            results.append((x, y))
            clicks_pending["want"] = False

    listener = mouse.Listener(on_click=on_click)
    listener.start()

    print("\nClick the 4 EXTREME corner cells of the playable diamond, in order:")
    print("  1. TOPMOST   2. BOTTOMMOST   3. LEFTMOST   4. RIGHTMOST\n")

    for label in EXTREMES:
        clicks_pending["want"] = True
        print(f"  -> click the {label} cell ...")
        while clicks_pending["want"]:
            try:
                listener.join(timeout=0.1)
            except KeyboardInterrupt:
                listener.stop()
                return 1
            if not listener.running:
                return 1
        x, y = results[-1]
        cell, residual = xy_to_cell(x, y, ox, oy, cw, ch)
        sub_row, pos = cell_to_subrow_pos(cell)
        u, v = cell_to_uv(cell)
        px, py = cell_to_xy(cell, ox, oy, cw, ch)
        flag = "ON-MAP" if on_map(cell) else "OFF-MAP (filtered)"
        print(f"     click=({x},{y})  -> cell {cell} sub_row={sub_row} pos={pos}")
        print(f"     residual={residual:.1f}px  iso=(u={u},v={v})  {flag}")
        print(f"     cell_to_xy round-trip = ({px},{py})  dx={x-px} dy={y-py}\n")

    listener.stop()

    print("=" * 60)
    print("SUMMARY (compare against the rectangular grid extremes):")
    print("  Rectangular grid model assumes EVERY (sub_row, pos) with")
    print("  sub_row even OR pos>0 is walkable. If the diamond's actual")
    print("  topmost cell is e.g. sub_row=0 pos=7 but cell 0 (pos=0) is")
    print("  reported on_map=True, the corner cells (cells 0..6 and 8..13)")
    print("  are outside the diamond and clicks there will silently fail.")
    print()
    print("Per-click details printed above. Look for:")
    print("  * topmost cell: sub_row should be 0; pos is the diamond-tip pos")
    print("  * bottommost cell: max sub_row; pos is mirror of topmost")
    print("  * leftmost cell: smallest x; sub_row at vertical mid of diamond")
    print("  * rightmost cell: largest x; sub_row at vertical mid")
    print()
    print("Any cells the bot has been retreating to (e.g. cell 4, cell 5)")
    print("that lie OUTSIDE the diamond corners reported here are the bug --")
    print("on_map() must reject them but currently doesn't.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
