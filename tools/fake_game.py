"""Fake F1 25 (2026 Season Pack) game: replays the bundled Melbourne laps
from `f1trace/demo.db` as real UDP telemetry so the whole pipeline can be
tested without the game.

Player car (idx 0) drives the player lap; the rival ghost (idx 1) drives
the faster full-telemetry lap and reproduces the real game's shadow-car
quirks: it runs on the player's lap clock (parking at the line when it
finishes first, rewinding when the player starts a new lap), its LapData
sector fields are junk, and its CarTelemetry slot interleaves genuine
frames with a constant flat-out placeholder (~486 km/h) whose junk speed
also leaks into Motion's velocity vector. That fabricated stream is why
TRACE keeps only a times-only pace reference per ghost
(docs/design-notes.md) — the junk is simulated here so a test run proves
the recorder reads nothing but the ghost's lap clock, lapDistance and
TimeTrial packet: the expected output is the player's full laps plus one
rival pace reference with only t/d columns.

Usage:  python3 tools/fake_game.py [--speedup 20] [--port 20777]
"""

import argparse
import json
import math
import os
import sqlite3
import struct
import socket
import time
import zlib

HEADER = struct.Struct("<HBBBBBQfIIBB")
LAP_CAR = struct.Struct("<IIHBHBHBHBfffBBBBBBBBBBBBBBBHHBfB")
MOTION_CAR = struct.Struct("<ffffffhhhhhhhhhfff")
TELEM_CAR = struct.Struct("<HfffBbHBBHHHHHBBBBBBBBBffffBBBB")
STATUS_CAR = struct.Struct("<BBBBBfffHHBBHBBBbfffBffffB")
TELEM2_CAR = struct.Struct("<BBHBBHBB")
TT_SET = struct.Struct("<BHIIIIBBBBBB")
SESSION_LEAD = struct.Struct("<BbbBHBbB")

N_CARS = 24
UID = int(time.time())

TRACK_ID = 0        # Melbourne
LAP_LEN = 5276      # metres (sessions.track_length in demo.db)


def header(pid, sim_t, frame):
    return HEADER.pack(2026, 26, 1, 0, 1, pid, UID, sim_t, frame, frame, 0, 255)


# ------------------------------------------------------------ lap data model

class Lap:
    """Distance-indexed telemetry + the recorded time<->distance mapping."""

    def __init__(self, cols, lap_ms, s1_ms, s2_ms, setup):
        self.d = cols["d"]
        self.t = [ms / 1000.0 for ms in cols["t"]]
        self.spd, self.brk, self.thr = cols["spd"], cols["brk"], cols["thr"]
        self.gear, self.steer, self.drs = cols["gear"], cols["str"], cols["drs"]
        self.temps = [cols["tfl"], cols["tfr"], cols["trl"], cols["trr"]]
        self.x, self.z = cols["x"], cols["z"]
        self.lap_ms = lap_ms
        self.s1_ms, self.s2_ms = s1_ms, s2_ms
        self.setup = setup

    def dist_at(self, t_sec):
        return _interp(self.t, self.d, t_sec)

    def time_at(self, dist):
        return _interp(self.d, self.t, dist)

    def pos_at(self, dist):
        return _interp(self.d, self.x, dist), _interp(self.d, self.z, dist)

    def at(self, dist):
        g = _interp(self.d, self.gear, dist)
        return {
            "spd": _interp(self.d, self.spd, dist),
            "brk": _interp(self.d, self.brk, dist) / 100.0,
            "thr": _interp(self.d, self.thr, dist) / 100.0,
            "steer": _interp(self.d, self.steer, dist) / 100.0,
            "gear": int(round(g)),
            "drs": 1 if _interp(self.d, self.drs, dist) > 0.5 else 0,
            "temps": [int(_interp(self.d, col, dist)) for col in self.temps],
        }


def _interp(xs, ys, x):
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    f = (x - xs[lo]) / (xs[hi] - xs[lo])
    return ys[lo] + (ys[hi] - ys[lo]) * f


def load_demo_db():
    """The bundled Melbourne laps: the player lap plus the fastest other
    lap that still has full telemetry (the times-only rival pace
    reference can't drive a simulated car)."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    con = sqlite3.connect(os.path.join(root, "f1trace", "demo.db"))
    player, others = None, []
    for role, lap_ms, s1, s2, setup, blob in con.execute(
            "SELECT car_role, lap_time_ms, s1_ms, s2_ms, setup, samples"
            " FROM laps"):
        cols = json.loads(zlib.decompress(blob))
        if "spd" not in cols:
            continue
        lap = Lap(cols, lap_ms, s1 or 0, s2 or 0,
                  json.loads(setup) if setup else None)
        if role == "player":
            player = lap
        else:
            others.append(lap)
    return player, min(others, key=lambda l: l.lap_ms)


# ------------------------------------------------------------ packet builders

def lap_data_packet(sim_t, frame, cars, secd):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * LAP_CAR.size
            continue
        # sector times appear on the timing screen once the sector is done
        s1 = c["s1"] if c["t_ms"] > c["s1"] else 0
        s2 = c["s2"] if c["t_ms"] > c["s1"] + c["s2"] else 0
        body += LAP_CAR.pack(
            c["last_ms"], c["t_ms"],
            s1 % 60000, s1 // 60000, s2 % 60000, s2 // 60000,
            0, 0, 0, 0,
            c["d"], c["total_d"], 0.0,
            1, c["lap_num"], 0, 0,
            0 if c["d"] < secd[0] else 1 if c["d"] < secd[1] else 2,
            0, 0, 0, 0, 0, 0, 1, 1, 2,
            0, 0, 0, 0, 0.0, 0)
    body += bytes([255, 1])  # TT PB ghost idx=none, rival idx=1
    return header(2, sim_t, frame) + body


def motion_packet(sim_t, frame, positions):
    body = b""
    for i in range(N_CARS):
        p = positions.get(i)          # (x, z, vel_x, vel_z)
        if p is None:
            body += b"\x00" * MOTION_CAR.size
        else:
            body += MOTION_CAR.pack(p[0], 0.0, p[1], p[2], 0.0, p[3],
                                    0, 0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0)
    return header(0, sim_t, frame) + body


def telem_packet(sim_t, frame, cars):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * TELEM_CAR.size
            continue
        t = c["temps"]
        body += TELEM_CAR.pack(
            int(c["spd"]), c["thr"], c["steer"], c["brk"], 0,
            max(-1, c["gear"]), int(4000 + c["thr"] * 8500), c["drs"], 0, 0,
            400, 400, 350, 350,
            t[2], t[3], t[0], t[1],      # surface: RL RR FL FR
            t[2], t[3], t[0], t[1], 95,
            26.0, 26.0, 24.0, 24.0, 0, 0, 0, 0)
    body += bytes([0, 0]) + struct.pack("<b", 0)
    return header(6, sim_t, frame) + body


def status_packet(sim_t, frame, active):
    body = b""
    for i in range(N_CARS):
        if i not in active:
            body += b"\x00" * STATUS_CAR.size
        else:
            # player: TC full + ABS (matches the demo lap's assists);
            # the ghost's CarStatus slot carries nothing useful
            tc, abs_ = (2, 1) if i == 0 else (0, 0)
            body += STATUS_CAR.pack(tc, abs_, 1, 57, 0, 10.0, 110.0, 20.0,
                                    13000, 3500, 8, 1, 0, 20, 16, 2, 0,
                                    460.0, 120.0, 4e6, 3, 0.0, 0.0, 2e6, 0.0, 0)
    return header(7, sim_t, frame) + body


def telem2_packet(sim_t, frame, cars):
    body = b""
    for i in range(N_CARS):
        c = cars.get(i)
        if c is None:
            body += b"\x00" * TELEM2_CAR.size
        else:
            ot = 1 if (c["drs"] and c["d"] > 4000) else 0
            body += TELEM2_CAR.pack(1 if c["thr"] > 0.9 else 0, 1, 0,
                                    1, ot, 0, 1, 0)
    return header(16, sim_t, frame) + body


SETUP_CAR = struct.Struct("<BBBBffffBBBBBBBBBffffBf")
SETUP_FIELDS = (
    "front_wing", "rear_wing", "on_throttle", "off_throttle",
    "front_camber", "rear_camber", "front_toe", "rear_toe",
    "front_susp", "rear_susp", "front_arb", "rear_arb",
    "front_height", "rear_height", "brake_pressure", "brake_bias",
    "engine_braking", "tp_rl", "tp_rr", "tp_fl", "tp_fr",
    "ballast", "fuel_load")
# aiControlled, driverId u16, networkId u16, teamId u16, myTeam, raceNumber,
# nationality, name[32], telemetryPublic, showNames, techLevel u16, platform
PART_CAR = struct.Struct("<BHHHBBB32sBBHB")


def session_packet(sim_t, frame):
    # Time Trial at Melbourne; weather/temps as in the recorded session
    lead = SESSION_LEAD.pack(0, 29, 21, 1, LAP_LEN, 18, TRACK_ID, 0)
    # steering..dynamicRacingLineType live at offset 656 after the header:
    # auto gearbox, ERS + DRS assists, full racing line (the demo lap's)
    assists = bytes([0, 0, 3, 0, 0, 1, 1, 2, 0])
    body = lead + b"\x00" * (656 - len(lead)) + assists
    body += b"\x00" * (679 - len(body)) + b"\x01"   # equalCarPerformance on
    return header(1, sim_t, frame) + body + b"\x00" * (909 - len(body))


def participants_packet(sim_t, frame):
    # only the player is listed — the real game leaves ghost slots out of
    # Participants, which is why recorded ghost laps have no team
    body = bytes([1])
    for i in range(N_CARS):
        if i == 0:
            body += PART_CAR.pack(0, 0, 0, 476, 0,   # Mercedes '26
                                  44, 0, b"FAKE DRIVER", 1, 1, 0, 1)
            body += bytes([1]) + b"\x00" * 12   # numColours + livery colours
        else:
            body += b"\x00" * (PART_CAR.size + 13)
    return header(4, sim_t, frame) + body


def setups_packet(sim_t, frame, laps):
    body = b""
    for i in range(N_CARS):
        lap = laps.get(i)
        if lap is None or lap.setup is None:
            body += b"\x00" * SETUP_CAR.size
        else:
            body += SETUP_CAR.pack(*(lap.setup[k] for k in SETUP_FIELDS))
    return header(5, sim_t, frame) + body + struct.pack("<f", 0.0)


def tt_packet(sim_t, frame, rival):
    # the TimeTrial packet is the only genuine source of ghost sectors
    z = TT_SET.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    s1, s2, s3 = rival["s1"], rival["s2"], rival["s3"]
    r = TT_SET.pack(1, 476, rival["lap_ms"], s1, s2, s3,
                    1, 1, 1, 0, 1, 1)   # TC medium, manual+hint, ABS on
    return header(14, sim_t, frame) + z + z + r


# ------------------------------------------------------------ main loop

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=20777)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--speedup", type=float, default=20.0)
    ap.add_argument("--laps", type=int, default=1, help="full laps to drive")
    args = ap.parse_args()

    player, ghost = load_demo_db()

    # sector boundaries as track distances, from the player's sector times
    secd = (player.dist_at(player.s1_ms / 1000.0),
            player.dist_at((player.s1_ms + player.s2_ms) / 1000.0))
    # the ghost lap has no stored sectors; derive them by clocking it
    # through the same boundaries so the TimeTrial packet carries real ones
    g1 = int(ghost.time_at(secd[0]) * 1000)
    g2 = int(ghost.time_at(secd[1]) * 1000) - g1
    rival_tt = {"lap_ms": ghost.lap_ms, "s1": g1, "s2": g2,
                "s3": ghost.lap_ms - g1 - g2}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    dest = (args.host, args.port)

    # the player drives the slower lap; the faster lap becomes the rival
    # ghost, so it finishes first and parks at the line (real-game
    # shadow-car behaviour)
    laps = {0: player, 1: ghost}
    end_t = max(player.t[-1], ghost.t[-1]) * args.laps + 8.0

    print("simulating %.0fs of Time Trial at %gx -> udp://%s:%d"
          % (end_t, args.speedup, *dest))
    dt = 1.0 / 30.0
    sim_t, frame = 0.0, 0
    next_session, next_status, next_tt = 0.0, 0.0, 0.0
    while sim_t < end_t:
        cars, positions = {}, {}
        player_dur = laps[0].t[-1]
        for idx, lap in laps.items():
            lap_dur = lap.t[-1]
            if idx == 1:
                # real TT rival ghost: it runs on the player's lap clock —
                # parking at the line once its own lap is done, rewinding
                # when the player crosses the line; lap_num never
                # increments and the LapData sector fields are junk
                t_in = sim_t % player_dur
                d = lap.dist_at(min(t_in, lap_dur))
                vals = lap.at(d)
                if t_in >= lap_dur:
                    vals.update(spd=0.0, thr=0.0, brk=0.0)  # parked
                cars[idx] = {
                    "t_ms": int(t_in * 1000), "d": d, "total_d": d,
                    "lap_num": 1,
                    "last_ms": lap.lap_ms if sim_t > lap_dur else 0,
                    "s1": 1, "s2": 44, **vals,
                }
            else:
                lap_num = int(sim_t // lap_dur) + 1
                t_in = sim_t % lap_dur
                if args.laps == 1 and lap_num > 1:
                    t_in = min(t_in, 8.0)  # cruise a bit into lap 2 then idle
                d = lap.dist_at(t_in)
                vals = lap.at(d)
                cars[idx] = {
                    "t_ms": int(t_in * 1000), "d": d,
                    "total_d": (lap_num - 1) * LAP_LEN + d,
                    "lap_num": lap_num,
                    "last_ms": lap.lap_ms if lap_num > 1 else 0,
                    "s1": lap.s1_ms, "s2": lap.s2_ms, **vals,
                }
            if idx == 1 and frame % 2:
                # the game interleaves a constant flat-out placeholder in
                # the shadow car's CarTelemetry slot
                cars[idx].update(spd=486, thr=1.0, brk=0.0, steer=0.0,
                                 gear=8)
            # real recorded world position along the lap's own line; the
            # velocity vector mirrors whatever the telemetry slot claims
            # (junk on placeholder frames) — as in the real game, only the
            # ghost's position is dependable
            p0, p1 = lap.pos_at(d), lap.pos_at(d + 3.0)
            hx, hz = p1[0] - p0[0], p1[1] - p0[1]
            hl = math.hypot(hx, hz) or 1.0
            v = cars[idx]["spd"] / 3.6
            positions[idx] = (p0[0], p0[1], hx / hl * v, hz / hl * v)

        sock.sendto(motion_packet(sim_t, frame, positions), dest)
        sock.sendto(telem_packet(sim_t, frame, cars), dest)
        sock.sendto(telem2_packet(sim_t, frame, cars), dest)
        sock.sendto(lap_data_packet(sim_t, frame, cars, secd), dest)
        if sim_t >= next_session:
            sock.sendto(session_packet(sim_t, frame), dest)
            sock.sendto(participants_packet(sim_t, frame), dest)
            next_session = sim_t + 1.0
        if sim_t >= next_status:
            sock.sendto(status_packet(sim_t, frame, set(laps)), dest)
            sock.sendto(setups_packet(sim_t, frame, laps), dest)
            next_status = sim_t + 0.2
        if sim_t >= next_tt:
            sock.sendto(tt_packet(sim_t, frame, rival_tt), dest)
            next_tt = sim_t + 0.5

        sim_t += dt
        frame += 1
        time.sleep(dt / args.speedup)
    print("done: sent %d frames" % frame)


if __name__ == "__main__":
    main()
