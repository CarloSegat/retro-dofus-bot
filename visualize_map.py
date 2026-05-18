"""Render a calibrated map's cell data as an iso-layout PNG.

    python3 visualize_map.py <world_x> <world_y> [--out FILE]

Colours: walkable=gray, obstacle=black, our start cell=blue,
switch cells=green with N/E/S/W label. Cells are drawn as diamonds
matching Dofus's iso grid (odd sub_rows shifted half a cell left).
The cell-id is drawn on each tile.

Use this to sanity-check calibration -- if obstacles look like a ring
around the playable area, mob-spawn cells got mis-clicked.
"""
import argparse
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from cell_grid import (
    CANVAS_MAX_SUBROW,
    CANVAS_MIN_SUBROW,
    CELLS_PER_PAIR,
    EVEN_ROW_LEN,
    ODD_ROW_LEN,
    cell_to_subrow_pos,
    on_map,
)


CELL_W = 60
CELL_H = 30
ORIGIN_X = 30  # leave margin for off-canvas pos=0 column
ORIGIN_Y = 30
TEXT = (40, 40, 40)
WALK = (210, 210, 210)
OBSTACLE = (20, 20, 20)
START = (60, 120, 230)
SWITCH = (60, 170, 80)
GRID_LINE = (130, 130, 130)


def cell_center(cell):
    sub_row, pos = cell_to_subrow_pos(cell)
    offset = -CELL_W / 2 if (sub_row % 2) else 0
    x = ORIGIN_X + pos * CELL_W + offset
    y = ORIGIN_Y + sub_row * (CELL_H / 2)
    return x, y


def diamond(cell):
    cx, cy = cell_center(cell)
    return [
        (cx, cy - CELL_H / 2),
        (cx + CELL_W / 2, cy),
        (cx, cy + CELL_H / 2),
        (cx - CELL_W / 2, cy),
    ]


def all_canvas_cells():
    """Yield every cell_id that on_map() accepts within the canvas."""
    for sub_row in range(CANVAS_MIN_SUBROW, CANVAS_MAX_SUBROW + 1):
        odd = sub_row % 2
        row_len = ODD_ROW_LEN if odd else EVEN_ROW_LEN
        for pos in range(row_len):
            cell = (sub_row // 2) * CELLS_PER_PAIR + (EVEN_ROW_LEN if odd else 0) + pos
            if on_map(cell):
                yield cell


def find_font(size):
    for candidate in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ):
        if Path(candidate).exists():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                continue
    return ImageFont.load_default()


def render(world_x, world_y, out_path):
    data_path = Path("map_data") / f"{world_x}_{world_y}.json"
    if not data_path.exists():
        sys.exit(f"no calibration file: {data_path}")
    data = json.loads(data_path.read_text())
    obstacles = set(data.get("obstacles") or [])
    start_cells = set(data.get("cells") or [])
    switch_cells = data.get("switch_cells") or {}
    switch_inv = {cell: direction for direction, cell in switch_cells.items()}

    width = int(ORIGIN_X * 2 + (max(EVEN_ROW_LEN, ODD_ROW_LEN) + 1) * CELL_W)
    height = int(ORIGIN_Y * 2 + (CANVAS_MAX_SUBROW + 1) * (CELL_H / 2))
    img = Image.new("RGB", (width, height), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    font = find_font(10)
    label_font = find_font(11)

    for cell in all_canvas_cells():
        if cell in obstacles:
            fill = OBSTACLE
        elif cell in start_cells:
            fill = START
        elif cell in switch_inv:
            fill = SWITCH
        else:
            fill = WALK
        poly = diamond(cell)
        draw.polygon(poly, fill=fill, outline=GRID_LINE)
        text_colour = (240, 240, 240) if fill in (OBSTACLE, START, SWITCH) else TEXT
        cx, cy = cell_center(cell)
        draw.text((cx, cy), str(cell), fill=text_colour, font=font, anchor="mm")
        if cell in switch_inv:
            draw.text((cx, cy + 10), switch_inv[cell][:1].upper(),
                      fill=(255, 255, 255), font=label_font, anchor="mm")

    map_id = data.get("map_id", "?")
    legend = (f"map_id={map_id} world=({world_x},{world_y})  "
              f"obstacles={len(obstacles)} starts={len(start_cells)} "
              f"switches={len(switch_cells)}")
    draw.text((10, height - 18), legend, fill=(0, 0, 0), font=label_font)

    img.save(out_path)
    print(f"wrote {out_path}  ({width}x{height})")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("world_x", type=int)
    p.add_argument("world_y", type=int)
    p.add_argument("--out", default=None,
                   help="output PNG path (default: /tmp/map_<x>_<y>.png)")
    args = p.parse_args()
    out = args.out or f"/tmp/map_{args.world_x}_{args.world_y}.png"
    render(args.world_x, args.world_y, out)


if __name__ == "__main__":
    main()
