"""Manage farming areas.

A farming area is a named, strongly-connected subset of calibrated
maps. The fighter is constrained to that subset at runtime (picked at
startup) so it doesn't wander into uncalibrated or unwanted territory.

Usage:

    # interactive create
    python3 -m scripts.create_farming_area

    # list existing areas + their maps
    python3 -m scripts.create_farming_area --list

    # delete by name (no confirmation; CASCADE removes membership rows)
    python3 -m scripts.create_farming_area --delete "Tofu Plains"

Interactive create:

  1. Lists calibrated maps grouped by world cluster (for context).
  2. Asks for an area name (must be unique).
  3. Asks for the list of world coords, space-separated, e.g.
     "4,-8 5,-8 4,-7 5,-7". Each coord must already be calibrated.
  4. Validates strong connectivity inside the proposed subgraph. If a
     map can't reach (or be reached by) the rest, the script prints
     which ones and exits without writing.
"""
import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dofus import map_data as md


def parse_world(s):
    """Parse "x,y" -> (int(x), int(y)). Raises ValueError on bad input."""
    parts = s.replace(" ", "").split(",")
    if len(parts) != 2:
        raise ValueError(f"expected 'x,y', got {s!r}")
    return int(parts[0]), int(parts[1])


def cmd_list():
    """Print every area + its maps. Read-only."""
    areas = md.list_farming_areas()
    if not areas:
        print("(no farming areas)")
        return 0
    for a in areas:
        full = md.get_farming_area(a["area_id"])
        map_ids = sorted(full["map_ids"]) if full else []
        data = md.load_all()
        coords = []
        for mid in map_ids:
            world = (data.get(mid) or {}).get("world")
            if isinstance(world, (list, tuple)) and len(world) == 2:
                coords.append(f"({world[0]},{world[1]})")
            else:
                coords.append(f"<map_id={mid}>")
        print(f"#{a['area_id']:>3}  {a['name']!r}  -- {a['map_count']} map(s): "
              f"{' '.join(coords)}")
    return 0


def cmd_delete(name):
    """Delete by name. Returns 0 if removed, 1 if name not found."""
    area = md.get_farming_area_by_name(name)
    if area is None:
        print(f"no farming area named {name!r}")
        return 1
    ok = md.delete_farming_area(area["area_id"])
    print(f"deleted area_id={area['area_id']} name={name!r}: "
          f"{'ok' if ok else 'failed'}")
    return 0 if ok else 1


def cmd_create():
    """Interactive create. Returns 0 on success, 1 on user-input error
    or connectivity failure."""
    data = md.load_all()
    by_world = md.build_world_index(data)
    if not data:
        print("no calibrated maps in DB; calibrate some first with "
              "calibrate_map_cells.py")
        return 1

    # Existing areas, for context.
    existing = md.list_farming_areas()
    if existing:
        print("existing farming areas:")
        for a in existing:
            print(f"  - {a['name']!r} ({a['map_count']} map(s))")
        print()

    # Show calibrated maps grouped by approximate cluster (sort by world).
    print(f"calibrated maps ({len(data)}):")
    rows = sorted(data.values(),
                  key=lambda e: (e.get("world") or [0, 0]))
    for entry in rows:
        world = entry.get("world") or ["?", "?"]
        sw = list((entry.get("switch_cells") or {}).keys())
        print(f"  ({world[0]:>4},{world[1]:>4})  map_id={entry['map_id']:<6}  "
              f"exits={sw}")
    print()

    name = input("area name: ").strip()
    if not name:
        print("name is required")
        return 1
    if md.get_farming_area_by_name(name) is not None:
        print(f"area {name!r} already exists; pick a different name or "
              f"--delete it first")
        return 1

    raw = input("world coords (space-separated, e.g. '4,-8 5,-8 4,-7'): ").strip()
    if not raw:
        print("at least one coord is required")
        return 1
    try:
        worlds = [parse_world(s) for s in raw.split()]
    except ValueError as e:
        print(f"bad coord: {e}")
        return 1

    map_ids = []
    missing = []
    for wx, wy in worlds:
        entry = by_world.get((wx, wy))
        if entry is None:
            missing.append((wx, wy))
        else:
            map_ids.append(int(entry["map_id"]))
    if missing:
        print(f"these world coords are not calibrated: {missing}")
        print("calibrate them first with calibrate_map_cells.py")
        return 1
    map_ids = sorted(set(map_ids))

    ok, msg = md.is_strongly_connected(map_ids, data, by_world)
    if not ok:
        print(f"NOT strongly connected: {msg}")
        print("the bot needs every map in the area to be reachable from "
              "every other map (using only in-area switch_cells). Either "
              "calibrate the missing switch_cells, or shrink the area.")
        return 1

    try:
        area_id = md.create_farming_area(name, map_ids, data, by_world)
    except ValueError as e:
        print(f"create failed: {e}")
        return 1
    print(f"OK: created area_id={area_id} name={name!r} with "
          f"{len(map_ids)} map(s) ({msg})")
    return 0


def main():
    p = argparse.ArgumentParser(description="manage farming areas")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list existing areas")
    g.add_argument("--delete", metavar="NAME", help="delete area by name")
    args = p.parse_args()
    if args.list:
        sys.exit(cmd_list())
    if args.delete:
        sys.exit(cmd_delete(args.delete))
    sys.exit(cmd_create())


if __name__ == "__main__":
    main()
