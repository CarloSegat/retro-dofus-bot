"""Per-map empirical obstacle store.

We can't read the static walkability of a Dofus map off the wire (it's
sealed in client-side .dlm files). Instead we learn it the hard way:
each time we click a neighbor cell and `my_cell` fails to update to it
within the walk timeout, we mark that (map_id, cell) as blocked and
never click it again on that map.

Persisted as JSON at ~/.auto-fighter/blocked.json so the knowledge
survives runs. On-disk schema (sorted lists for git-friendly diffs):

    { "<map_id>": { "blocked": [cell, cell, ...] } }

Each cell listed under "blocked" is a tile the bot clicked and FAILED
to land on -- treat it as unwalkable on that map. Older versions of
this file stored the bare list (no "blocked" key); load() accepts
either shape.

Public API:
  load() -> {map_id: set[int]}
  save(store)
  is_blocked(store, map_id, cell) -> bool
  add(store, map_id, cell)      # mutates + persists
"""
import json
from pathlib import Path

STORE_PATH = Path.home() / ".auto-fighter" / "blocked.json"


def load():
    if not STORE_PATH.exists():
        return {}
    try:
        raw = json.loads(STORE_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    out = {}
    for k, v in raw.items():
        try:
            map_id = int(k)
        except ValueError:
            continue
        # New shape: {"blocked": [...]}. Old shape (pre-schema): bare list.
        cells = v.get("blocked", ()) if isinstance(v, dict) else v
        out[map_id] = {int(c) for c in cells}
    return out


def save(store):
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    serializable = {
        str(k): {"blocked": sorted(v)} for k, v in store.items() if v
    }
    STORE_PATH.write_text(json.dumps(serializable, indent=2, sort_keys=True))


def is_blocked(store, map_id, cell):
    return cell in store.get(map_id, ())


def add(store, map_id, cell):
    store.setdefault(map_id, set()).add(cell)
    save(store)
