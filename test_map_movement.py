"""One-off: walk through each calibrated NSEW switch on the current map
and back, verifying the bot can navigate maps round-trip.

Tests every direction in `safe_directions(entry, map_by_world)` -- i.e.
where both the outbound switch_cell and the inbound switch on the
target map are calibrated. Skips one-way exits.

Usage:
  # in another terminal:
  sudo go run ./proxy/cmd/proxy

  # then, with Dofus running, standing on a calibrated map, IDLE:
  python3 -u test_map_movement.py
"""
import sys
import time

from dofus.actions import click_cell
from dofus.map_data import (
    OPPOSITE_DIRECTION,
    build_world_index,
    load_all as load_map_data,
    safe_directions,
)
from dofus.proxy_client import ProxyState
from fighter.helpers import PROXY_ADDR, load_cal, wait_for


MAP_CHANGE_TIMEOUT = 20.0
INTER_TEST_SETTLE_SEC = 1.5
ARRIVAL_SETTLE_SEC = 1.0


def main():
    cal = load_cal()
    map_data = load_map_data()
    map_by_world = build_world_index(map_data)

    state = ProxyState(PROXY_ADDR)
    state.start()
    print(f"[test] connecting to proxy at {PROXY_ADDR}...")
    if not wait_for(state, lambda s: s.connected and s.map_id != 0, 10.0):
        snap = state.snapshot()
        print(f"[test] proxy not ready: connected={snap.connected} "
              f"map_id={snap.map_id}")
        sys.exit(1)

    start_snap = state.snapshot()
    if start_snap.in_fight:
        print(f"[test] currently in_fight; finish the fight then re-run")
        sys.exit(1)
    start_map_id = start_snap.map_id
    entry = map_data.get(start_map_id)
    if entry is None:
        print(f"[test] map_id={start_map_id} is not calibrated; "
              f"calibrate it first or move to a calibrated map")
        sys.exit(1)
    safe = safe_directions(entry, map_by_world)
    print(f"[test] starting on map_id={start_map_id} world={entry.get('world')}")
    print(f"[test] switch_cells: {entry.get('switch_cells') or {}}")
    print(f"[test] return-safe directions: {safe or '(none)'}")
    if not safe:
        print(f"[test] no return-safe direction from this map; nothing to test")
        sys.exit(1)

    results = {}
    for direction in safe:
        print(f"\n=== Testing {direction} ===")
        results[direction] = test_round_trip(
            state, cal, map_data, start_map_id, direction)
        # Sanity: confirm we're back on start before next test
        if state.snapshot().map_id != start_map_id:
            print(f"[test] not back on start map (map_id="
                  f"{state.snapshot().map_id}); aborting suite")
            break
        time.sleep(INTER_TEST_SETTLE_SEC)

    print(f"\n=== Summary ===")
    for d, ok in results.items():
        print(f"  {d:5s} {'OK' if ok else 'FAIL'}")
    bad = [d for d, ok in results.items() if not ok]
    sys.exit(1 if bad else 0)


def test_round_trip(state, cal, map_data, start_map_id, direction):
    """Walk out `direction` and back. Returns True iff we end up
    back on start_map_id. False on map-change timeout, accidental
    fight engage, or missing return calibration on the target map."""
    entry = map_data[start_map_id]
    switch_cell = entry["switch_cells"][direction]

    print(f"[test] walking {direction} to switch cell {switch_cell}")
    click_cell(switch_cell, cal)
    if not wait_for(
        state,
        lambda s: (s.map_id != start_map_id and s.map_id != 0) or s.in_fight,
        MAP_CHANGE_TIMEOUT,
    ):
        print(f"[test] map did not change in {MAP_CHANGE_TIMEOUT}s")
        return False
    arrival = state.snapshot()
    if arrival.in_fight:
        print(f"[test] aggroed while walking {direction} -- can't continue. "
              f"Finish the fight, walk back to start, re-run.")
        return False
    new_map_id = arrival.map_id
    print(f"[test] arrived at map_id={new_map_id}")

    # Let my_cell repopulate and the new map settle.
    wait_for(state, lambda s: s.my_cell != 0, 2.0, poll=0.05)
    time.sleep(ARRIVAL_SETTLE_SEC)

    new_entry = map_data.get(new_map_id)
    if new_entry is None:
        print(f"[test] arrival map_id={new_map_id} is not in map_data")
        return False
    return_dir = OPPOSITE_DIRECTION.get(direction)
    return_switch = (new_entry.get("switch_cells") or {}).get(return_dir)
    if return_switch is None:
        print(f"[test] no return {return_dir} switch on arrival map; can't go back")
        return False

    print(f"[test] returning {return_dir} via switch cell {return_switch}")
    click_cell(return_switch, cal)
    if not wait_for(
        state,
        lambda s: s.map_id == start_map_id or s.in_fight,
        MAP_CHANGE_TIMEOUT,
    ):
        cur = state.snapshot().map_id
        print(f"[test] did not return to start map (now on map_id={cur})")
        return False
    if state.snapshot().in_fight:
        print(f"[test] aggroed on the return trip")
        return False
    print(f"[test] returned to map_id={start_map_id}; {direction} OK")
    return True


if __name__ == "__main__":
    main()
