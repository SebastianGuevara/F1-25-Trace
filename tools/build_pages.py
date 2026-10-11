"""Bake the browser demo into a static site (for GitHub Pages).

Copies the viewer (f1trace/static/) and pre-renders the JSON the viewer
fetches — the same responses server.py would give for demo.db — as plain
files under api/. The status response carries "static": true, which the
viewer reads as "read-only: hide the delete buttons".

Usage: python3 tools/build_pages.py [output-dir]   (default: _site)
"""

import json
import os
import shutil
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from f1trace import __version__, db, ids  # noqa: E402


def dump(out, path, obj):
    full = os.path.join(out, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else "_site"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "f1trace", "static"), out)

    con = sqlite3.connect(os.path.join(ROOT, "f1trace", "demo.db"))
    con.row_factory = sqlite3.Row

    dump(out, "api/status", {"listening": False, "demo": True,
                             "static": True, "version": __version__})

    tracks = [dict(r) for r in con.execute(
        "SELECT s.track_id, s.track_name, COUNT(l.id) AS n_laps,"
        " MIN(CASE WHEN l.car_role='player' AND l.valid=1"
        "     THEN l.lap_time_ms END) AS best_ms,"
        " MAX(s.started_at) AS last_at"
        " FROM sessions s JOIN laps l ON l.session_id = s.id"
        " GROUP BY s.track_id ORDER BY last_at DESC")]
    # a static path can't be both a file and a directory: /api/tracks is
    # also the parent of /api/tracks/0/laps, so the list goes into an
    # index.html the server returns for the bare directory request
    dump(out, "api/tracks/index.html", tracks)

    for t in tracks:
        rows = con.execute(
            "SELECT l.id, l.car_role, l.car_index, l.lap_num,"
            " l.lap_time_ms, l.s1_ms, l.s2_ms, l.s3_ms, l.valid,"
            " l.tyre_visual, l.top_speed, l.n_samples, l.created_at,"
            " l.assists, l.setup IS NOT NULL AS has_setup, l.team_id,"
            " l.session_id, s.started_at, s.session_type_name,"
            " s.packet_format"
            " FROM laps l JOIN sessions s ON s.id = l.session_id"
            " WHERE s.track_id=?"
            " ORDER BY s.id DESC, l.id", (t["track_id"],)).fetchall()
        laps = []
        for r in rows:
            d = dict(r)
            d["assists"] = json.loads(d["assists"]) if d["assists"] else None
            d["team_name"] = ids.team_name(d["team_id"])
            laps.append(d)
        dump(out, "api/tracks/%d/laps" % t["track_id"], laps)

    for r in con.execute(
            "SELECT l.*, s.track_id, s.track_name, s.track_length,"
            " s.session_type_name"
            " FROM laps l JOIN sessions s ON s.id = l.session_id"):
        meta = {k: r[k] for k in r.keys() if k != "samples"}
        for k in ("setup", "assists"):
            meta[k] = json.loads(meta[k]) if meta.get(k) else None
        meta["team_name"] = ids.team_name(meta.get("team_id"))
        meta["samples"] = db.unpack_samples(r["samples"])
        dump(out, "api/laps/%d" % r["id"], meta)

    n = sum(len(fs) for _, _, fs in os.walk(out))
    print("built %s: %d files" % (out, n))


if __name__ == "__main__":
    main()
