"""Per-map calibrated data: starting cells, NSEW exits, obstacles,
gatherable resources, and points of interest (zaap, bank entrance, ...).

Backed by Postgres (see db/schema.sql and docker-compose.yml). The
public Python API rehydrates rows into the same entry-dict shape the
old JSON files had, so callers that read `entry.get("cells")` /
`entry.get("switch_cells")` etc. don't change. New fields surface as
`entry.get("resources")` and `entry.get("pois")`.

Entry shape:

    {
      "world":        [x, y],
      "map_id":       <int>,
      "cells":        [<start_cell>, ...],        # in saved click order
      "switch_cells": {"north": <cell>, ...},      # only registered dirs
      "obstacles":    [<blocked_cell>, ...],
      "resources":    [{"cell": <c>, "type": <s>, "name": <s>}, ...],
      "pois":         [{"cell": <c>, "type": <s>, "name": <s>|None}, ...],
      "saved_at":     "YYYY-MM-DD HH:MM:SS",
    }

Dofus world-coord convention (per DIRECTION_WORLD_DELTA): north
decreases y, south increases y, east increases x, west decreases x.

Manual / calibrated counterpart to `obstacles.py`'s runtime-learned
blocked store at `~/.auto-fighter/blocked.json`.

Connection: reads MAP_DB_URL env var, defaults to
`postgresql://auto:auto@127.0.0.1:5432/auto_fighter` (the docker-compose
service in this repo). A single autocommit connection is opened lazily
on first use and reused. `save()` runs its mutations inside an
explicit transaction so a failure mid-update doesn't half-write a map.
"""
import os
from collections import deque

DEFAULT_DB_URL = "postgresql://auto:auto@127.0.0.1:5432/auto_fighter"
DB_URL_ENV = "MAP_DB_URL"

DIRECTION_WORLD_DELTA = {
    "north": (0, -1),
    "south": (0, 1),
    "east":  (1, 0),
    "west":  (-1, 0),
}
OPPOSITE_DIRECTION = {
    "north": "south",
    "south": "north",
    "east":  "west",
    "west":  "east",
}

_CONN = None


def _get_conn():
    """Lazy autocommit connection. Re-opens if the previous one was
    closed (e.g. server restarted). Imports psycopg inside the function
    so importing this module doesn't require the driver be installed —
    only callers that actually touch the DB pay that cost."""
    global _CONN
    if _CONN is not None and not _CONN.closed:
        return _CONN
    import psycopg
    url = os.environ.get(DB_URL_ENV, DEFAULT_DB_URL)
    _CONN = psycopg.connect(url, autocommit=True)
    return _CONN


def close_conn():
    """Close the module-level connection, if any. Tests/long-lived
    processes call this on shutdown; short scripts don't need to."""
    global _CONN
    if _CONN is not None and not _CONN.closed:
        _CONN.close()
    _CONN = None


def load_all():
    """Return {map_id: entry_dict} for every map row in the DB.

    One SELECT per child table; entries are joined in memory. Fine for
    the tens-to-low-hundreds of maps we calibrate by hand.
    """
    conn = _get_conn()
    entries = {}
    with conn.cursor() as cur:
        cur.execute("SELECT map_id, world_x, world_y, saved_at FROM maps")
        for map_id, wx, wy, saved_at in cur.fetchall():
            entries[map_id] = {
                "world": [wx, wy],
                "map_id": map_id,
                "cells": [],
                "switch_cells": {},
                "obstacles": [],
                "resources": [],
                "pois": [],
                "saved_at": saved_at.strftime("%Y-%m-%d %H:%M:%S") if saved_at else None,
            }

        cur.execute("SELECT map_id, cell FROM start_cells ORDER BY map_id, seq")
        for mid, cell in cur.fetchall():
            e = entries.get(mid)
            if e is not None:
                e["cells"].append(cell)

        cur.execute("SELECT map_id, direction, cell FROM switch_cells")
        for mid, direction, cell in cur.fetchall():
            e = entries.get(mid)
            if e is not None:
                e["switch_cells"][direction] = cell

        cur.execute("SELECT map_id, cell FROM obstacles ORDER BY map_id, cell")
        for mid, cell in cur.fetchall():
            e = entries.get(mid)
            if e is not None:
                e["obstacles"].append(cell)

        cur.execute(
            "SELECT map_id, cell, res_type, name FROM map_resources "
            "ORDER BY map_id, cell"
        )
        for mid, cell, rtype, name in cur.fetchall():
            e = entries.get(mid)
            if e is not None:
                e["resources"].append({"cell": cell, "type": rtype, "name": name})

        cur.execute(
            "SELECT map_id, poi_type, cell, name FROM map_pois "
            "ORDER BY map_id, poi_type, cell"
        )
        for mid, ptype, cell, name in cur.fetchall():
            e = entries.get(mid)
            if e is not None:
                e["pois"].append({"cell": cell, "type": ptype, "name": name})

    return entries


def save(entry):
    """Upsert `entry` into the DB inside one transaction.

    Field-level semantics: a child table is wiped-and-rewritten ONLY if
    its key is present in `entry`. So a partial update like
    `{"map_id": M, "world": [x,y], "obstacles": [...]}` rewrites
    obstacles and leaves cells/switch_cells/resources/pois untouched.
    `load_all()` produces full entries, so the round-trip
    load -> mutate -> save preserves everything.

    Returns True on success, False if the entry lacks the keys we need
    to identify the map (map_id + world).
    """
    map_id = entry.get("map_id")
    world = entry.get("world")
    if map_id is None or not (isinstance(world, (list, tuple)) and len(world) == 2):
        return False
    wx, wy = int(world[0]), int(world[1])
    map_id = int(map_id)

    conn = _get_conn()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO maps (map_id, world_x, world_y, saved_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (map_id) DO UPDATE
                  SET world_x = EXCLUDED.world_x,
                      world_y = EXCLUDED.world_y,
                      saved_at = NOW()
                """,
                (map_id, wx, wy),
            )

            if "cells" in entry:
                cur.execute("DELETE FROM start_cells WHERE map_id = %s", (map_id,))
                rows = [(map_id, i, int(c))
                        for i, c in enumerate(entry["cells"] or [])]
                if rows:
                    cur.executemany(
                        "INSERT INTO start_cells (map_id, seq, cell) "
                        "VALUES (%s, %s, %s)",
                        rows,
                    )

            if "switch_cells" in entry:
                cur.execute("DELETE FROM switch_cells WHERE map_id = %s", (map_id,))
                rows = [(map_id, d, int(c))
                        for d, c in (entry["switch_cells"] or {}).items()]
                if rows:
                    cur.executemany(
                        "INSERT INTO switch_cells (map_id, direction, cell) "
                        "VALUES (%s, %s, %s)",
                        rows,
                    )

            if "obstacles" in entry:
                cur.execute("DELETE FROM obstacles WHERE map_id = %s", (map_id,))
                rows = [(map_id, int(c))
                        for c in sorted(set(entry["obstacles"] or []))]
                if rows:
                    cur.executemany(
                        "INSERT INTO obstacles (map_id, cell) VALUES (%s, %s)",
                        rows,
                    )

            if "resources" in entry:
                cur.execute("DELETE FROM map_resources WHERE map_id = %s", (map_id,))
                rows = [(map_id, int(r["cell"]), r["type"], r["name"])
                        for r in (entry["resources"] or [])]
                if rows:
                    cur.executemany(
                        "INSERT INTO map_resources (map_id, cell, res_type, name) "
                        "VALUES (%s, %s, %s, %s)",
                        rows,
                    )

            if "pois" in entry:
                cur.execute("DELETE FROM map_pois WHERE map_id = %s", (map_id,))
                rows = [(map_id, p["type"], int(p["cell"]), p.get("name"))
                        for p in (entry["pois"] or [])]
                if rows:
                    cur.executemany(
                        "INSERT INTO map_pois (map_id, poi_type, cell, name) "
                        "VALUES (%s, %s, %s, %s)",
                        rows,
                    )
    return True


def build_world_index(map_data):
    """{(world_x, world_y): entry} for every entry with a valid world field."""
    out = {}
    for entry in map_data.values():
        world = entry.get("world")
        if not (isinstance(world, (list, tuple)) and len(world) == 2):
            continue
        out[(int(world[0]), int(world[1]))] = entry
    return out


def target_map_id(entry, direction, by_world):
    """The map_id reached by walking `direction` from `entry`, or None if
    the target world coord has no calibrated map_data entry."""
    world = entry.get("world")
    if not (isinstance(world, (list, tuple)) and len(world) == 2):
        return None
    delta = DIRECTION_WORLD_DELTA.get(direction)
    if delta is None:
        return None
    target = by_world.get((int(world[0]) + delta[0], int(world[1]) + delta[1]))
    return target.get("map_id") if target else None


def find_path(start_world, target_world, by_world):
    """BFS over the calibrated map graph. Returns the shortest list of
    NSEW directions to walk from `start_world` to `target_world`, or
    `None` if no such path exists in the currently-calibrated data.

    Edge rule (outbound only): from world W to neighbour W', the move
    is valid iff:
      1. by_world[W] has a `switch_cells` entry for the direction from
         W to W' (we actually have a cell to click to leave W that way)
      2. W' is also in by_world (we know what the next map is)

    NOTE: this is intentionally *broader* than safe_directions() --
    we don't require the *return* switch on intermediate maps because
    walk_to is one-way: we don't need to come back through the same
    edge. If you want the round-trip-safe variant, use safe_directions
    when computing neighbours instead of switch_cells.

    Returns [] (empty path) if start_world == target_world, None if
    unreachable, otherwise a list like ["north", "east", "east"].
    """
    start = (int(start_world[0]), int(start_world[1]))
    goal = (int(target_world[0]), int(target_world[1]))
    if start == goal:
        return []
    if start not in by_world or goal not in by_world:
        return None
    visited = {start}
    queue = deque([(start, [])])
    while queue:
        cur, path = queue.popleft()
        switches = by_world[cur].get("switch_cells") or {}
        for direction, delta in DIRECTION_WORLD_DELTA.items():
            if direction not in switches:
                continue
            nbr = (cur[0] + delta[0], cur[1] + delta[1])
            if nbr in visited or nbr not in by_world:
                continue
            new_path = path + [direction]
            if nbr == goal:
                return new_path
            visited.add(nbr)
            queue.append((nbr, new_path))
    return None


def safe_directions(entry, by_world):
    """Directions from `entry` whose target map is calibrated AND has
    the opposite switch cell calibrated (= return path exists)."""
    world = entry.get("world")
    if not (isinstance(world, (list, tuple)) and len(world) == 2):
        return []
    switches = entry.get("switch_cells") or {}
    out = []
    wx, wy = int(world[0]), int(world[1])
    for direction in switches:
        delta = DIRECTION_WORLD_DELTA.get(direction)
        if delta is None:
            continue
        target = by_world.get((wx + delta[0], wy + delta[1]))
        if not target:
            continue
        if OPPOSITE_DIRECTION[direction] not in (target.get("switch_cells") or {}):
            continue
        out.append(direction)
    return out
