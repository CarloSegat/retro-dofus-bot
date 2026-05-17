# Cell geometry

Dofus iso grid is **rotated 45°**. Cell numbering is *not* a simple chess
grid:

- Sub-rows alternate width: **even = 14 cells, odd = 15 cells (offset
  half cell left)**. Two sub-rows = **29 cells = one chess-row**.
- Edge-adjacent neighbor deltas: NE=−14, SE=+15, NW=−15, SW=+14.
  Implied: E=+1, S=+29, W=−1, N=−29.

Helpers in `cell_grid.py`:

- `cell_to_xy(cell, origin_x, origin_y, cell_w, cell_h)`
- `cell_distance(a, b)` — Dofus "Po" range, L1 in iso (u,v) basis
- `neighbors(cell)` — the 4 edge-adjacent cells, used by the Sacrid
  loop's walk-to-adjacent step
- `on_map(cell)` — drops the off-map column (odd sub-row, pos 0).
  Those positions compute to `origin_x − cell_w/2` which lands outside
  the game window's left edge; calibrated map data confirms odd rows
  start at pos 1, not pos 0. `a_star` / `reachable_within` /
  `pick_next_step` use this to refuse off-window walk clicks.

## Calibration

`cell_calibration` in `config.json` is per-window-size; if you resize
the game it must be re-derived. No script ships for that today — pull
the old `calibrate_cells.py` from git if you need to refit.

## Gotcha

`row=c//14, col=c%14` is **wrong** for Dofus. Use the 29-cells-per-pair
sub-row layout above.
