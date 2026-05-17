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


def xy_to_cell(x, y, origin_x, origin_y, cell_w, cell_h, max_sub_row=42):
    """Inverse of cell_to_xy. Returns (cell_id, residual_px) -- the cell
    whose center is closest to (x, y) under the given calibration. The
    residual is Euclidean distance from the click to that cell's center;
    > ~cell_w/2 means the click was outside any cell."""
    sr_est = int((y - origin_y) / (cell_h / 2.0))
    best_cell = 0
    best_d2 = float("inf")
    for sub_row in {sr_est - 1, sr_est, sr_est + 1}:
        if sub_row < 0 or sub_row > max_sub_row:
            continue
        odd = sub_row % 2
        offset = -cell_w / 2.0 if odd else 0.0
        row_len = ODD_ROW_LEN if odd else EVEN_ROW_LEN
        p_est = int((x - origin_x - offset) / cell_w)
        for pos in {p_est - 1, p_est, p_est + 1}:
            if pos < 0 or pos >= row_len:
                continue
            cell = (sub_row // 2) * CELLS_PER_PAIR + (EVEN_ROW_LEN if odd else 0) + pos
            px, py = cell_to_xy(cell, origin_x, origin_y, cell_w, cell_h)
            d2 = (px - x) ** 2 + (py - y) ** 2
            if d2 < best_d2:
                best_d2 = d2
                best_cell = cell
    return best_cell, best_d2 ** 0.5


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


CANVAS_MIN_SUBROW = 1
CANVAS_MAX_SUBROW = 31


def on_map(cell):
    """True iff `cell` is a valid playable Dofus Retro cell.

    Every map shares the same fixed canvas (per-map differences are
    obstacles, not bounds). The canvas is a screen-aligned rectangle:

      - sub_row in [1, 31].  sub_row 0 (cells 0..13) sits above the
        playable area; cells at sub_row 32+ fall below the visible
        game window.
      - pos==0 is off-map on every sub_row. Confirmed by clicking the
        left edge of the diamond top-to-bottom (see
        `calibrate_left_edge.py`): every left-edge click resolves to
        an odd-row pos=1 cell, and the visually neighbouring even-row
        cells are also at pos=1 -- so the entire `pos==0` column sits
        west of the diamond. Used to keep even-row pos=0 as on-map;
        the retreat loop kept clicking those cells and Dofus silently
        ignored every click.

    Po-distance==1 alone doesn't catch off-canvas cells since e.g.
    (cell 0, cell 14) is a legitimate edge-neighbour pair in (u, v)
    terms."""
    if cell < 0:
        return False
    sub_row, pos = cell_to_subrow_pos(cell)
    if sub_row < CANVAS_MIN_SUBROW or sub_row > CANVAS_MAX_SUBROW:
        return False
    if pos == 0:
        return False
    return True


NEIGHBOR_DELTAS = (-14, 15, -15, 14)  # NE, SE, NW, SW


def neighbors(cell):
    """The 4 edge-adjacent cells of `cell` (NE, SE, NW, SW).

    No bounds checking — callers should drop cells whose Po distance from
    `cell` is not 1 (which happens when the result wraps off-grid), and
    cells where `on_map` is False (off the playable diamond)."""
    return [cell + d for d in NEIGHBOR_DELTAS]


def a_star(start, goal, blocked=()):
    """Shortest cell-id path from `start` to `goal` avoiding `blocked` cells.

    Returns a list of cells `[start, ..., goal]` or `None` if unreachable
    (or if `start`/`goal` is itself blocked). Each step is one edge-adjacent
    neighbor; off-grid wraps are filtered by requiring Po distance 1
    between consecutive cells. The heuristic is `cell_distance` (Po
    range), which equals the true cost on an open grid and is therefore
    admissible — A* returns an optimal path.

    Pair with `calibrate_map_cells.py` output:

        data = json.loads(Path("map_data/0_4.json").read_text())
        path = a_star(start_cell, goal_cell, blocked=data["obstacles"])
    """
    import heapq

    blocked = set(blocked)
    if start == goal:
        return [start]
    if start in blocked or goal in blocked:
        return None

    open_heap = [(cell_distance(start, goal), 0, start)]
    came_from = {start: None}
    g_score = {start: 0}

    while open_heap:
        _, g, current = heapq.heappop(open_heap)
        if current == goal:
            path = []
            c = current
            while c is not None:
                path.append(c)
                c = came_from[c]
            path.reverse()
            return path
        if g > g_score[current]:
            continue  # stale heap entry
        for n in neighbors(current):
            if n in blocked:
                continue
            if cell_distance(n, current) != 1:
                continue  # off-grid wrap
            if not on_map(n):
                continue  # off the playable diamond
            tentative = g + 1
            if tentative < g_score.get(n, float("inf")):
                came_from[n] = current
                g_score[n] = tentative
                f = tentative + cell_distance(n, goal)
                heapq.heappush(open_heap, (f, tentative, n))

    return None


def reachable_within(start, mp_budget, blocked=()):
    """BFS cells reachable from `start` in at most `mp_budget` steps.

    Returns {cell: steps_from_start} including `start` at distance 0.
    `blocked` cells are not entered (still allowed as neighbors during
    expansion check, just never enqueued). Drops off-grid wraps by
    requiring each step to be Po-distance 1 from the previous cell."""
    blocked = set(blocked)
    seen = {start: 0}
    frontier = [start]
    for step in range(1, mp_budget + 1):
        next_frontier = []
        for c in frontier:
            for n in neighbors(c):
                if n in seen or n in blocked:
                    continue
                if cell_distance(n, c) != 1:
                    continue  # off-grid wrap
                if not on_map(n):
                    continue  # off the playable diamond
                seen[n] = step
                next_frontier.append(n)
        if not next_frontier:
            break
        frontier = next_frontier
    return seen


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
