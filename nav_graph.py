"""Debug-print the navigation graph the bot will traverse.

Reads every map_data/*.json and reports which NSEW exits are
bi-directionally calibrated (= the bot will walk through them).
Run any time map_data/ changes:

    python3 nav_graph.py

Sections:
  - Connected edges: bi-directional, bot will traverse.
  - One-way edges:   target map exists but lacks the return switch cell.
  - Dangling edges:  target world coord has no map_data file.
  - Per-map summary: NSEW status per map.
  - Isolated maps:   no safe exit -- bot would be stuck on these.
"""
from map_data import (
    DIRECTION_WORLD_DELTA,
    OPPOSITE_DIRECTION,
    build_world_index,
    load_all,
    safe_directions,
)

DIRS = ("north", "east", "south", "west")


def _world_key(entry):
    w = entry.get("world") or [0, 0]
    return (int(w[0]), int(w[1]))


def main():
    data = load_all()
    if not data:
        print("no map_data files found.")
        return
    by_world = build_world_index(data)

    connected = []   # (aw, amid, ad, bw, bmid, bd)
    one_way = []     # (aw, ad, bw, opp_missing)
    dangling = []    # (aw, ad, target_world)
    seen_edges = set()

    for entry in data.values():
        aw = _world_key(entry)
        amid = entry.get("map_id")
        switches = entry.get("switch_cells") or {}
        for direction in switches:
            delta = DIRECTION_WORLD_DELTA.get(direction)
            if delta is None:
                continue
            tw = (aw[0] + delta[0], aw[1] + delta[1])
            target = by_world.get(tw)
            if target is None:
                dangling.append((aw, direction, tw))
                continue
            opp = OPPOSITE_DIRECTION[direction]
            if opp not in (target.get("switch_cells") or {}):
                one_way.append((aw, direction, tw, opp))
                continue
            edge_key = tuple(sorted([aw, tw]))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            connected.append((aw, amid, direction, tw, target.get("map_id"), opp))

    print("== Connected edges (bi-directional, bot will traverse) ==")
    if not connected:
        print("  (none)")
    for aw, amid, ad, bw, bmid, bd in sorted(connected):
        print(f"  {aw} [{amid}]  <-{ad}/{bd}->  {bw} [{bmid}]")

    print("\n== One-way edges (bot will NOT traverse) ==")
    if not one_way:
        print("  (none)")
    for aw, ad, bw, opp in sorted(one_way):
        print(f"  {aw} {ad} -> {bw}  [target has no '{opp}' switch_cell]")

    print("\n== Dangling edges (no calibration for target) ==")
    if not dangling:
        print("  (none)")
    for aw, ad, tw in sorted(dangling):
        print(f"  {aw} {ad} -> {tw}  [no map_data file]")

    print("\n== Per-map summary ==")
    isolated = []
    for entry in sorted(data.values(), key=_world_key):
        world = _world_key(entry)
        switches = entry.get("switch_cells") or {}
        safe = set(safe_directions(entry, by_world))
        parts = []
        for d in DIRS:
            label = d[0].upper()
            if d not in switches:
                parts.append(f"{label}:none")
            elif d in safe:
                parts.append(f"{label}:safe")
            else:
                delta = DIRECTION_WORLD_DELTA[d]
                tgt = (world[0] + delta[0], world[1] + delta[1])
                parts.append(f"{label}:dangling" if tgt not in by_world else f"{label}:one-way")
        print(f"  {world}  " + "  ".join(parts)
              + f"   ({len(safe)}/{len(switches) or 0} safe)")
        if not safe:
            isolated.append(world)

    print("\n== Isolated maps (no safe exit) ==")
    if not isolated:
        print("  (none)")
    for world in sorted(isolated):
        print(f"  {world}")


if __name__ == "__main__":
    main()
