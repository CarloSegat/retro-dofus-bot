"""One-shot: walk the bot from the current map to a target world (x, y).

Pathfinds via BFS over the calibrated map_data DB. Fails *upfront* with a
non-zero exit if no path exists in the calibrated graph -- no clicks
are issued in that case, so the bot stays put.

On aggro mid-walk, runs the same combat loop as `main.py`, then
re-pathfinds from wherever the fight left us and resumes.

Usage:
    # in another terminal: proxy must be running, Dofus logged in
    python3 -u walk_to.py <world_x> <world_y>

Examples:
    python3 -u walk_to.py -46 13      # walk to Astrub bank-ish area
    python3 -u walk_to.py 5 -27

Inspect the calibrated graph with `python3 nav_graph.py` if a path
fails -- it lists missing edges and isolated maps.
"""
import argparse
import sys

from fighter.logging_setup import setup_logging
from fighter.orchestrator import Orchestrator


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("world_x", type=int)
    parser.add_argument("world_y", type=int)
    parser.add_argument("--screen", default=None,
                        help="calibration key in config.json[cell_calibrations]")
    args = parser.parse_args()
    target = (args.world_x, args.world_y)

    setup_logging()
    orch = Orchestrator(screen_name=args.screen)
    print(f"[walk_to] target world {target}")
    ok = orch.navigator.walk_to_world(target, on_aggro=orch.combat.run)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
