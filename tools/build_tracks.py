"""Build f1trace/static/tracks.json from the f1-circuits GeoJSON dataset
(github.com/bacinger/f1-circuits): real circuit outlines for every track
on the game's 2026 calendar, keyed by the game's trackId.

Each entry: {"name": str, "len": official length in metres,
             "pts": [[x, z], ...]}  -- metres, centred on the circuit,
starting at the dataset's first point (≈ start/finish), loop closed.

Usage: python3 tools/build_tracks.py <path-to-f1-circuits-repo>
"""

import json
import math
import os
import sys

# game trackId (2026 Season Pack enum) -> circuit file id
TRACK_FILES = {
    0: "au-1953",    # Melbourne
    2: "cn-2004",    # Shanghai
    3: "bh-2002",    # Bahrain
    4: "es-1991",    # Barcelona-Catalunya
    5: "mc-1929",    # Monaco
    6: "ca-1978",    # Montreal
    7: "gb-1948",    # Silverstone
    9: "hu-1986",    # Hungaroring
    10: "be-1925",   # Spa
    11: "it-1922",   # Monza
    12: "sg-2008",   # Singapore
    13: "jp-1962",   # Suzuka
    14: "ae-2009",   # Abu Dhabi
    15: "us-2012",   # Austin
    16: "br-1940",   # Interlagos
    17: "at-1969",   # Red Bull Ring
    19: "mx-1962",   # Mexico City
    20: "az-2016",   # Baku
    26: "nl-1948",   # Zandvoort
    27: "it-1953",   # Imola
    29: "sa-2021",   # Jeddah
    30: "us-2022",   # Miami
    31: "us-2023",   # Las Vegas
    32: "qa-2004",   # Qatar
    42: "es-2026",   # Madrid
}
# reverse-layout variants share the base shape
ALIASES = {39: 7, 40: 17, 41: 26}


def convert(path):
    d = json.load(open(path))
    feat = d["features"][0]
    props = feat["properties"]
    coords = feat["geometry"]["coordinates"]
    lat0 = sum(c[1] for c in coords) / len(coords)
    lon0 = sum(c[0] for c in coords) / len(coords)
    kx = 111320.0 * math.cos(math.radians(lat0))
    kz = 110540.0
    pts = [((c[0] - lon0) * kx, (c[1] - lat0) * kz) for c in coords]
    if pts[0] != pts[-1]:
        pts.append(pts[0])  # close the loop
    # scale so path length matches the official circuit length
    raw = sum(math.dist(pts[i - 1], pts[i]) for i in range(1, len(pts)))
    k = props["length"] / raw
    pts = [(round(x * k, 1), round(z * k, 1)) for x, z in pts]
    return {"name": props["Name"], "len": props["length"], "pts": pts}


def main():
    repo = sys.argv[1] if len(sys.argv) > 1 else "f1-circuits"
    dest = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "f1trace", "static", "tracks.json")
    try:
        old = json.load(open(dest))
    except OSError:
        old = {}
    out = {}
    for tid, fid in TRACK_FILES.items():
        path = os.path.join(repo, "circuits", fid + ".geojson")
        out[str(tid)] = convert(path)
        # official corner charts (tools/fetch_corners.py) survive a rebuild
        if "corners" in old.get(str(tid), {}):
            out[str(tid)]["corners"] = old[str(tid)]["corners"]
        print("track %2d  %-42s %5dm  %d pts" % (
            tid, out[str(tid)]["name"], out[str(tid)]["len"],
            len(out[str(tid)]["pts"])))
    for alias, base in ALIASES.items():
        # reverse layouts share the shape but not the corner chart
        out[str(alias)] = {k: v for k, v in out[str(base)].items()
                           if k != "corners"}
    with open(dest, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    print("wrote %s (%.0f KB)" % (dest, os.path.getsize(dest) / 1024))
    print("now run tools/fetch_corners.py — outlines are written raw and "
          "need re-aligning to the official start/finish")


if __name__ == "__main__":
    main()
