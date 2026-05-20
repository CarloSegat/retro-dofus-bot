"""One-shot migration: load every map_data/*.json into the DB.

Idempotent — re-running upserts the same data (handy if you tweak a
JSON file then want to push it again). Leaves the JSON files on disk;
remove them manually once the DB is confirmed good.

Usage:

    docker compose up -d db   # or however you've brought Postgres up
    python3 -m scripts.import_map_data_to_db

    # alternate DB target:
    MAP_DB_URL=postgresql://... python3 -m scripts.import_map_data_to_db

Reads each file's saved_at and patches it onto the row after save()
runs (save() always stamps NOW(); we don't want to lose the original
calibration timestamps on first import).
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running as `python3 scripts/import_map_data_to_db.py`.
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dofus import map_data as md

MAP_DATA_DIR = REPO_ROOT / "map_data"


def parse_saved_at(s):
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def main():
    if not MAP_DATA_DIR.exists():
        print(f"[import] no map_data/ directory at {MAP_DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    files = sorted(MAP_DATA_DIR.glob("*.json"))
    if not files:
        print(f"[import] no JSON files in {MAP_DATA_DIR}", file=sys.stderr)
        sys.exit(1)

    print(f"[import] found {len(files)} map file(s) in {MAP_DATA_DIR}")

    conn = md._get_conn()
    print(f"[import] connected to {md.DB_URL_ENV}="
          f"{md.os.environ.get(md.DB_URL_ENV, md.DEFAULT_DB_URL)}")

    ok = 0
    skipped = 0
    for path in files:
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            print(f"  [skip] {path.name}: {exc}")
            skipped += 1
            continue

        if data.get("map_id") is None or not isinstance(data.get("world"), list):
            print(f"  [skip] {path.name}: missing map_id or world")
            skipped += 1
            continue

        # The on-disk shape only has cells/switch_cells/obstacles; resources
        # and pois don't exist in JSON yet, so we deliberately omit them
        # from the entry (save() won't touch those tables in that case —
        # re-running the import preserves any resources/pois the user has
        # added through the DB directly).
        entry = {
            "map_id": int(data["map_id"]),
            "world": [int(data["world"][0]), int(data["world"][1])],
            "cells": list(data.get("cells") or []),
            "switch_cells": dict(data.get("switch_cells") or {}),
            "obstacles": list(data.get("obstacles") or []),
        }
        md.save(entry)

        saved_at = parse_saved_at(data.get("saved_at"))
        if saved_at is not None:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE maps SET saved_at = %s WHERE map_id = %s",
                    (saved_at, entry["map_id"]),
                )

        ok += 1
        print(f"  [ok]   {path.name}: map_id={entry['map_id']} "
              f"world={entry['world']} "
              f"cells={len(entry['cells'])} "
              f"switches={len(entry['switch_cells'])} "
              f"obstacles={len(entry['obstacles'])}")

    print(f"\n[import] imported {ok} map(s), skipped {skipped}.")

    # Quick sanity readback.
    loaded = md.load_all()
    print(f"[import] readback via load_all(): {len(loaded)} entry(ies) in DB.")


if __name__ == "__main__":
    main()
