"""Dofus Retro cell-id geometry.

The map is an isometric grid rotated 45 degrees. Cells are interleaved on
two sub-row types, alternating top-to-bottom:

  - Even sub-rows (0, 2, 4, ...): 14 cells, no x-offset.
  - Odd sub-rows  (1, 3, 5, ...): 15 cells, shifted half a cell to the left
                                  (so they straddle the gaps of the even row).

A "chess-row" of the underlying rotated grid spans both sub-rows = 29 cells
total. Hence cell-id deltas for edge-sharing neighbors are:

  E:  +1     W:  -1
  S:  +29    N:  -29
  NE: -14    NW: -15
  SE: +15    SW: +14

Going right by +1 in cell-id is visually horizontal on screen because the
chess-grid's "east" axis sits at 45 deg to both iso axes, summing them: a
single +SE step (+15) combined with one -NE step (-(-14)=+14)... wait, the
cleaner way: E = NE + SE -> +1 = -14 + 15. Yes.

Calibration (origin_x, origin_y, cell_w, cell_h) is the pixel position of
the center of cell 0 (origin_x, origin_y) plus the per-cell screen pitch.
"""

CELLS_PER_PAIR = 29
EVEN_ROW_LEN = 14
ODD_ROW_LEN = 15


def cell_to_subrow_pos(cell):
    """cell_id -> (sub_row, pos_in_row). sub_row 0 is topmost, even."""
    pair = cell // CELLS_PER_PAIR
    rem = cell % CELLS_PER_PAIR
    if rem < EVEN_ROW_LEN:
        return 2 * pair, rem
    return 2 * pair + 1, rem - EVEN_ROW_LEN


def cell_to_xy(cell, origin_x, origin_y, cell_w, cell_h):
    """Center pixel (x, y) of cell_id under the given calibration."""
    sub_row, pos = cell_to_subrow_pos(cell)
    offset = -cell_w / 2.0 if (sub_row % 2) else 0.0
    x = origin_x + pos * cell_w + offset
    y = origin_y + sub_row * (cell_h / 2.0)
    return int(round(x)), int(round(y))


def cell_to_uv(cell):
    """cell_id -> (u, v) iso-axis coords. +u steps SE on screen, +v steps SW.
    These are the two edge-neighbor axes: an edge-adjacent cell differs by
    exactly one unit in u or v.
    """
    sub_row, pos = cell_to_subrow_pos(cell)
    odd = sub_row % 2
    u = (sub_row + 2 * pos - odd) // 2
    v = (sub_row - 2 * pos + odd) // 2
    return u, v


def cell_distance(a, b):
    """Dofus 'Po' range between two cells: number of edge-step moves to walk
    from a to b. Equal to L1 norm in (u, v) iso coords."""
    ua, va = cell_to_uv(a)
    ub, vb = cell_to_uv(b)
    return abs(ua - ub) + abs(va - vb)


NEIGHBOR_DELTAS = (-14, 15, -15, 14)  # NE, SE, NW, SW


def neighbors(cell):
    """The 4 edge-adjacent cells of `cell` (NE, SE, NW, SW).

    No bounds checking — callers should drop cells whose Po distance from
    `cell` is not 1 (which happens when the result wraps off-grid)."""
    return [cell + d for d in NEIGHBOR_DELTAS]


def fit_calibration(pairs):
    """Least-squares fit of (origin_x, origin_y, cell_w, cell_h) from
    (click_xy, cell_id) samples. Returns dict with the four floats and
    `residual_px` (RMS pixel error)."""
    import numpy as np

    pts = list(pairs)
    if len(pts) < 2:
        raise ValueError("need at least 2 calibration pairs")

    A_x, b_x, A_y, b_y = [], [], [], []
    for (cx, cy), cell in pts:
        sub_row, pos = cell_to_subrow_pos(cell)
        offset_coef = -0.5 if (sub_row % 2) else 0.0
        # cx = 1*origin_x + (pos + offset_coef)*cell_w
        A_x.append([1.0, pos + offset_coef])
        b_x.append(cx)
        # cy = 1*origin_y + (0.5*sub_row)*cell_h
        A_y.append([1.0, 0.5 * sub_row])
        b_y.append(cy)

    A_x = np.array(A_x)
    A_y = np.array(A_y)
    sol_x, *_ = np.linalg.lstsq(A_x, np.array(b_x), rcond=None)
    sol_y, *_ = np.linalg.lstsq(A_y, np.array(b_y), rcond=None)
    origin_x, cell_w = sol_x
    origin_y, cell_h = sol_y

    sq = 0.0
    for (cx, cy), cell in pts:
        px, py = cell_to_xy(cell, origin_x, origin_y, cell_w, cell_h)
        sq += (cx - px) ** 2 + (cy - py) ** 2
    rms = (sq / len(pts)) ** 0.5

    return {
        "origin_x": float(origin_x),
        "origin_y": float(origin_y),
        "cell_w": float(cell_w),
        "cell_h": float(cell_h),
        "residual_px": float(rms),
        "samples": len(pts),
    }
