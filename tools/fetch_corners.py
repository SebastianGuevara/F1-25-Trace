"""Align f1trace/static/tracks.json outlines to the official start/finish
line and write official corner charts into them.

Source: the MultiViewer circuit API (api.multiviewer.app, the same data
FastF1 exposes as get_circuit_info()): the racing line as driven (start =
the real start/finish, order = driving direction) and, per corner, the
official number and its distance along that line.

Two things are written per track, both idempotent:
  pts      reversed / rotated so the outline starts at the official
           start/finish and runs in driving direction (curvature
           cross-correlation against the MultiViewer line; the source
           GeoJSON tracings start anywhere and run either way — Singapore
           is traced backwards, Silverstone starts 2.8 km from the line)
  corners  lap-distance fractions of the official corner chart, in
           driving order — the viewer numbers them 1..N and anchors each
           label to the nearest bend of its own outline, so numbering
           matches the published chart instead of trusting curvature
           detection alone

Lettered sub-corners (Hungaroring 1A, 12A) are dropped: they aren't part
of the plain 1..N numbering the viewer draws. Madrid (trackId 42) has no
MultiViewer data yet and keeps detection-based numbering.

Run after every tools/build_tracks.py rebuild. Saved viewer calibrations
are keyed to the outline point order — bump the localStorage key in
app.js (cal2 -> cal3 -> ...) whenever an alignment shifts existing data.

Usage: python3 tools/fetch_corners.py [trackId ...]   (default: all known)
"""

import json
import math
import os
import sys
import urllib.request

LIST_API = "https://api.multiviewer.app/api/v1/circuits"
API = "https://api.multiviewer.app/api/v1/circuits/%d/%d"

# game trackId (2026 Season Pack enum) -> MultiViewer circuit key
CIRCUIT_KEYS = {
    0: 10,    # Melbourne
    2: 49,    # Shanghai
    3: 63,    # Bahrain (Sakhir)
    4: 15,    # Barcelona (Catalunya)
    5: 22,    # Monaco (Monte Carlo)
    6: 23,    # Montreal
    7: 2,     # Silverstone
    9: 4,     # Hungaroring
    10: 7,    # Spa-Francorchamps
    11: 39,   # Monza
    12: 61,   # Singapore
    13: 46,   # Suzuka
    14: 70,   # Abu Dhabi (Yas Marina)
    15: 9,    # Austin
    16: 14,   # Interlagos
    17: 19,   # Red Bull Ring (Spielberg)
    19: 65,   # Mexico City
    20: 144,  # Baku
    26: 55,   # Zandvoort
    27: 6,    # Imola
    29: 149,  # Jeddah
    30: 151,  # Miami
    31: 152,  # Las Vegas
    32: 150,  # Qatar (Losail)
}
# reverse-layout variants share the base shape (kept in sync, no charts)
ALIASES = {39: 7, 40: 17, 41: 26}

GRID = 720    # correlation grid points per lap


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "f1trace"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def resample(pts, n):
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + math.dist(pts[i - 1], pts[i]))
    total = cum[-1]
    st = total / n
    out = []
    j = 0
    for i in range(n):
        s = i * st
        while j < len(cum) - 2 and cum[j + 1] < s:
            j += 1
        f = (s - cum[j]) / ((cum[j + 1] - cum[j]) or 1)
        out.append((pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f,
                    pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f))
    return out, total


def kappa(pts, st):
    n = len(pts)
    head = [math.atan2(pts[(i + 1) % n][1] - p[1], pts[(i + 1) % n][0] - p[0])
            for i, p in enumerate(pts)]
    k = []
    for i in range(n):
        dh = head[i] - head[i - 1]
        while dh > math.pi:
            dh -= 2 * math.pi
        while dh < -math.pi:
            dh += 2 * math.pi
        k.append(dh / st)
    return [sum(k[(i + w) % n] for w in range(-2, 3)) / 5 for i in range(n)]


def align(pts, d):
    """pts reversed/rotated into the MultiViewer line's frame (start =
    official start/finish, order = driving direction). Returns
    (pts, reversed?, shift metres, correlation score)."""
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    mv, mtot = resample(list(zip(d["x"], d["y"])), GRID)
    mk = kappa(mv, mtot / GRID)
    best = (0.0, 0, 0)
    for rev in (0, 1):
        cand = pts[::-1] if rev else pts
        rs, rtot = resample(cand + [cand[0]], GRID)
        ok = kappa(rs, rtot / GRID)
        norm = math.sqrt(sum(v * v for v in ok) * sum(v * v for v in mk)) or 1
        for off in range(GRID):
            c = sum(mk[i] * ok[(i + off) % GRID] for i in range(GRID))
            if abs(c) / norm > best[0]:
                best = (abs(c) / norm, rev, off)
    score, rev, off = best
    if rev:
        pts = pts[::-1]
    # rotate: interpolate the exact start point at arc position off*st
    cum = [0.0]
    for i in range(1, len(pts) + 1):
        cum.append(cum[-1] + math.dist(pts[i - 1], pts[i % len(pts)]))
    total = cum[-1]
    s0 = off * total / GRID
    j = next(i for i in range(len(pts)) if cum[i + 1] >= s0)
    f = (s0 - cum[j]) / ((cum[j + 1] - cum[j]) or 1)
    p0 = (round(pts[j][0] + (pts[(j + 1) % len(pts)][0] - pts[j][0]) * f, 1),
          round(pts[j][1] + (pts[(j + 1) % len(pts)][1] - pts[j][1]) * f, 1))
    out = [p0] + pts[j + 1:] + pts[:j + 1]
    if out[-1] != p0:
        out.append(p0)   # close the loop
    shift = s0 if s0 <= total / 2 else s0 - total
    return out, rev, shift, score


def fractions(d):
    """Corner lap-distance fractions from the circuit's racing line,
    or None (with a complaint) when the chart can't be a plain list."""
    xs, ys = d["x"], d["y"]
    total = sum(math.hypot(xs[i] - xs[i - 1], ys[i] - ys[i - 1])
                for i in range(1, len(xs)))
    total += math.hypot(xs[0] - xs[-1], ys[0] - ys[-1])  # close the loop
    cs = [c for c in d["corners"] if not c.get("letter")]
    ns = [c["number"] for c in cs]
    if ns != list(range(1, len(ns) + 1)):
        print("  !! %s numbers corners %s — skipped" % (d["circuitName"], ns))
        return None
    fr = [round(c["length"] / total, 4) for c in cs]
    if fr != sorted(fr) or fr[-1] >= 1:
        print("  !! %s corner distances not ordered — skipped"
              % d["circuitName"])
        return None
    return fr


def residual(tr):
    """Worst distance from a chart anchor to the nearest curvature peak of
    the aligned outline — how far the viewer's snapping has to reach."""
    pts, total = resample(tr["pts"], max(400, int(tr["len"] / 5)))
    st = total / len(pts)
    k = [abs(v) for v in kappa(pts, st)]
    n = len(k)
    worst = 0.0
    for f in tr["corners"]:
        c = int(f * n)
        d = next((w for w in range(0, n // 4)
                  if (k[(c + w) % n] > 1 / 500 and
                      k[(c + w) % n] >= k[(c + w - 1) % n] and
                      k[(c + w) % n] >= k[(c + w + 1) % n]) or
                     (k[(c - w) % n] > 1 / 500 and
                      k[(c - w) % n] >= k[(c - w - 1) % n] and
                      k[(c - w) % n] >= k[(c - w + 1) % n])), n // 4)
        worst = max(worst, d * st)
    return worst


def main():
    tids = [int(a) for a in sys.argv[1:]] or sorted(CIRCUIT_KEYS)
    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "f1trace", "static", "tracks.json")
    tracks = json.load(open(path))
    years = {int(k): max(v["years"]) for k, v in get(LIST_API).items()
             if v["years"]}
    for tid in tids:
        key = CIRCUIT_KEYS[tid]
        d = get(API % (key, years[key]))
        tr = tracks[str(tid)]
        tr["pts"], rev, shift, score = align(tr["pts"], d)
        fr = fractions(d)
        if fr:
            tr["corners"] = fr
        print("track %2d  %-20s %2d corners  %s%+5.0fm shift"
              "  corr %.2f  peak within %3.0fm" % (
                  tid, d["circuitName"], len(fr or []),
                  "REV " if rev else "    ", shift, score,
                  residual(tr) if fr else float("nan")))
    for alias, base in ALIASES.items():
        if str(base) in tracks:
            tracks[str(alias)] = {k: v for k, v in tracks[str(base)].items()
                                  if k != "corners"}
    with open(path, "w") as f:
        json.dump(tracks, f, separators=(",", ":"))
    print("wrote %s (%.0f KB)" % (path, os.path.getsize(path) / 1024))


if __name__ == "__main__":
    main()
