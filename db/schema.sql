-- auto-fighter map calibration store.
--
-- One row per calibrated Dofus map; child tables hold the per-cell
-- annotations the bot reads (start cells for placement, switch cells
-- for navigation, obstacles for pathing) and the per-cell annotations
-- the gatherer/runner workflows will read (resources, POIs).
--
-- Replaces the per-map map_data/<world_x>_<world_y>.json files. The
-- public Python API in dofus/map_data.py rehydrates rows into the same
-- entry-dict shape callers already use, so consumers don't change.

CREATE TABLE IF NOT EXISTS maps (
    map_id   INTEGER PRIMARY KEY,
    world_x  INTEGER NOT NULL,
    world_y  INTEGER NOT NULL,
    saved_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (world_x, world_y)
);

-- Starting cells for placement phase. seq preserves click order — the
-- bot clicks them in this order at fight start (engager.place_starting_cells).
CREATE TABLE IF NOT EXISTS start_cells (
    map_id INTEGER NOT NULL REFERENCES maps(map_id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    cell   INTEGER NOT NULL,
    PRIMARY KEY (map_id, seq),
    UNIQUE (map_id, cell)
);

CREATE TABLE IF NOT EXISTS switch_cells (
    map_id    INTEGER NOT NULL REFERENCES maps(map_id) ON DELETE CASCADE,
    direction TEXT    NOT NULL CHECK (direction IN ('north','south','east','west')),
    cell      INTEGER NOT NULL,
    PRIMARY KEY (map_id, direction)
);

CREATE TABLE IF NOT EXISTS obstacles (
    map_id INTEGER NOT NULL REFERENCES maps(map_id) ON DELETE CASCADE,
    cell   INTEGER NOT NULL,
    PRIMARY KEY (map_id, cell)
);

-- Gatherable nodes (trees, ores, plants, ...). One per cell. name is
-- a human label so the user can tell two identical-type nodes apart.
CREATE TABLE IF NOT EXISTS map_resources (
    map_id   INTEGER NOT NULL REFERENCES maps(map_id) ON DELETE CASCADE,
    cell     INTEGER NOT NULL,
    res_type TEXT    NOT NULL,
    name     TEXT    NOT NULL,
    PRIMARY KEY (map_id, cell)
);
CREATE INDEX IF NOT EXISTS map_resources_by_type ON map_resources (res_type);

-- Points of interest the bot can click (bank entrance, zaap, phoenix,
-- dungeon door, NPC, ...). cell is the click target. name is optional;
-- include it when one map has multiple of the same type.
CREATE TABLE IF NOT EXISTS map_pois (
    map_id   INTEGER NOT NULL REFERENCES maps(map_id) ON DELETE CASCADE,
    poi_type TEXT    NOT NULL,
    cell     INTEGER NOT NULL,
    name     TEXT,
    PRIMARY KEY (map_id, poi_type, cell)
);
CREATE INDEX IF NOT EXISTS map_pois_by_type ON map_pois (poi_type);
