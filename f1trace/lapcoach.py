"""Corner-by-corner comparison of a lap against a reference lap.

Each corner of the track (apexes from tracks.json, as fractions of the
lap) owns the stretch from halfway after the previous apex to halfway
before the next one. For every stretch we compare the time spent, where
the driver starts braking, the minimum speed and where he's back to full
throttle — the three things a driving coach looks at.
"""

import bisect
import json
import os

_TRACKS = None


def corners(track_id):
    """Apex distances as fractions of the lap, or None if unknown."""
    global _TRACKS
    if _TRACKS is None:
        try:
            path = os.path.join(os.path.dirname(__file__), "static", "tracks.json")
            with open(path, encoding="utf-8") as f:
                _TRACKS = json.load(f)
        except (OSError, ValueError):
            _TRACKS = {}
    t = _TRACKS.get(str(track_id)) or {}
    return t.get("corners") or None


def _t_at(lap, d):
    """Lap time (ms) at distance d, interpolated."""
    ds, ts = lap["d"], lap["t"]
    i = bisect.bisect_left(ds, d)
    if i <= 0:
        return ts[0]
    if i >= len(ds):
        return ts[-1]
    d0, d1 = ds[i - 1], ds[i]
    f = (d - d0) / (d1 - d0) if d1 > d0 else 0
    return ts[i - 1] + f * (ts[i] - ts[i - 1])


def _zone(lap, a, b):
    """Braking point, minimum speed and full-throttle point in [a, b]."""
    ds = lap["d"]
    i0, i1 = bisect.bisect_left(ds, a), bisect.bisect_right(ds, b)
    if i1 - i0 < 3:
        return None
    idx = range(i0, i1)
    k = min(idx, key=lambda i: lap["spd"][i])
    brake = next((ds[i] for i in range(i0, k + 1) if lap["brk"][i] >= 30), None)
    throttle = next((ds[i] for i in range(k, i1) if lap["thr"][i] >= 95), None)
    return {"min_speed": lap["spd"][k], "apex_d": ds[k], "brake_d": brake,
            "throttle_d": throttle, "max_brake": max(lap["brk"][i] for i in idx),
            "gear": lap["gear"][k]}


def compare(lap, ref, track_id, length):
    """Corner zones of `lap` vs `ref` (both column dicts with t, d, spd,
    thr, brk, gear). Returns a list of zones, or None."""
    apex = corners(track_id)
    if not apex or not length or not ref or len(lap["d"]) < 50:
        return None
    pts = [f * length for f in apex]
    bounds = [0.0] + [(a + b) / 2 for a, b in zip(pts, pts[1:])] + [float(length)]
    zones = []
    for n in range(len(pts)):
        a, b = bounds[n], bounds[n + 1]
        dt = ((_t_at(lap, b) - _t_at(lap, a))
              - (_t_at(ref, b) - _t_at(ref, a)))
        me, rf = _zone(lap, a, b), _zone(ref, a, b)
        z = {"corner": n + 1, "delta_ms": int(round(dt))}
        if me and rf:
            z["min_speed"] = me["min_speed"]
            z["min_speed_ref"] = rf["min_speed"]
            z["gear"] = me["gear"]
            if me["brake_d"] is not None and rf["brake_d"] is not None:
                # + = brakes later than the reference
                z["brake_later_m"] = int(round(me["brake_d"] - rf["brake_d"]))
            if me["throttle_d"] is not None and rf["throttle_d"] is not None:
                # + = back on full throttle later than the reference
                z["throttle_later_m"] = int(round(me["throttle_d"] - rf["throttle_d"]))
        zones.append(z)
    return zones


def cause_es(z):
    """The main reason a corner was slow, in a few radio words."""
    bl, tl = z.get("brake_later_m"), z.get("throttle_later_m")
    dv = (z.get("min_speed") or 0) - (z.get("min_speed_ref") or 0)
    if bl is not None and bl <= -8:
        return "frenas %d metros antes" % -bl
    if dv <= -4:
        return "pasas %d km/h más lento por el vértice" % -dv
    if tl is not None and tl >= 10:
        return "aceleras a fondo %d metros más tarde" % tl
    if bl is not None and bl >= 8 and dv <= 0:
        return "frenas tarde y se te va el vértice"
    if dv >= 4:
        return "entras rápido pero sales peor"
    return "sales más lento de la curva"


def summary(zones, lap_ms, ref_ms, sectors=None, ref_sectors=None):
    """Compact analysis for Claude: totals, sectors, worst and best corners."""
    if not zones:
        return None
    worst = sorted([z for z in zones if z["delta_ms"] > 20],
                   key=lambda z: -z["delta_ms"])[:4]
    best = sorted([z for z in zones if z["delta_ms"] < -20],
                  key=lambda z: z["delta_ms"])[:3]
    out = {"lap_vs_ref_ms": lap_ms - ref_ms if lap_ms and ref_ms else None,
           "corners_lost": [dict(z, cause=cause_es(z)) for z in worst],
           "corners_gained": best}
    if sectors and ref_sectors and all(sectors) and all(ref_sectors):
        out["sectors_vs_ref_ms"] = [a - b for a, b in zip(sectors, ref_sectors)]
    return out
