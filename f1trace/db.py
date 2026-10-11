"""SQLite storage. Samples are stored per lap as a zlib-compressed,
column-oriented JSON blob — small on disk and served to the frontend
with a single decompress."""

import json
import sqlite3
import zlib

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY,
    uid TEXT NOT NULL,
    started_at TEXT NOT NULL,
    packet_format INTEGER,
    game_year INTEGER,
    track_id INTEGER,
    track_name TEXT,
    session_type INTEGER,
    session_type_name TEXT,
    weather INTEGER,
    air_temp INTEGER,
    track_temp INTEGER,
    track_length INTEGER
);
CREATE TABLE IF NOT EXISTS laps (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    car_role TEXT NOT NULL,          -- player | guest (imported) | pb_ghost /
                                     -- rival (times-only pace references)
    car_index INTEGER,
    lap_num INTEGER,
    lap_time_ms INTEGER,
    s1_ms INTEGER, s2_ms INTEGER, s3_ms INTEGER,
    valid INTEGER NOT NULL DEFAULT 1,
    tyre_visual INTEGER,
    top_speed INTEGER,
    n_samples INTEGER,
    created_at TEXT NOT NULL,
    samples BLOB,
    setup TEXT,                      -- JSON car setup snapshot, if broadcast
    assists TEXT,                    -- JSON assist settings, if known
    team_id INTEGER                  -- constructor (Participants packet)
);
CREATE INDEX IF NOT EXISTS idx_laps_session ON laps(session_id);
"""


def connect(path):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    con.executescript(SCHEMA)
    _migrate(con)
    return con


def _migrate(con):
    """Add columns introduced after the first release to existing DBs."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(laps)")}
    for col, typ in (("setup", "TEXT"), ("assists", "TEXT"),
                     ("team_id", "INTEGER")):
        if col not in cols:
            con.execute("ALTER TABLE laps ADD COLUMN %s %s" % (col, typ))
    _strip_ghost_channels(con)
    con.commit()


def _strip_ghost_channels(con):
    """Ghost laps are times-only pace references since 0.1.4. Laps stored
    by the 0.1.x full-telemetry ghost capture carry fabricated channels
    (placeholder-poisoned inputs, derived speed) — strip them down to the
    genuine time-at-distance series. Idempotent: converted laps have only
    t and d left, so they are skipped on later runs."""
    rows = con.execute(
        "SELECT id, samples FROM laps"
        " WHERE car_role IN ('pb_ghost', 'rival') AND samples IS NOT NULL"
    ).fetchall()
    for row in rows:
        try:
            s = unpack_samples(row["samples"])
        except Exception:
            continue
        if set(s) <= {"t", "d"}:
            continue
        kept = {"t": s["t"], "d": s["d"]}
        con.execute(
            "UPDATE laps SET samples=?, top_speed=NULL, setup=NULL"
            " WHERE id=?", (pack_samples(kept), row["id"]))
        print("[f1trace] converted ghost lap %d to a times-only"
              " pace reference" % row["id"])


def pack_samples(columns):
    return zlib.compress(json.dumps(columns, separators=(",", ":")).encode(), 6)


def unpack_samples(blob):
    return json.loads(zlib.decompress(blob).decode())
