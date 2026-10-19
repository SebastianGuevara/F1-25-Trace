"""UDP listener + lap segmentation.

Runs in its own thread, owns the DB writer connection. Keeps a
latest-value cache for the player's car for motion/telemetry/status
packets and snapshots a sample row every time a LapData packet arrives
(LapData carries the two join keys: currentLapTime and lapDistance).

Full telemetry is recorded for the player's car only. The Time Trial
ghosts are captured as **pace references**: just their lapDistance/lap
clock series plus the TimeTrial packet's times — the only ghost data
the game broadcasts honestly. (Versions up to 0.1.3 stored full ghost
telemetry; most of it was fabricated — docs/design-notes.md.)
"""

import datetime
import json
import os
import socket
import threading
import time

from . import db, ids, packets
from . import engineer as engineer_mod


def _now():
    return datetime.datetime.now().isoformat(timespec="seconds")


class LapBuffer:
    __slots__ = ("lap_num", "samples", "invalid", "s1_ms", "s2_ms")

    def __init__(self, lap_num):
        self.lap_num = lap_num
        self.samples = []
        self.invalid = False
        self.s1_ms = 0
        self.s2_ms = 0


class Recorder(threading.Thread):
    def __init__(self, db_path, udp_port=20777, driver_override=None):
        super().__init__(daemon=True, name="f1trace-recorder")
        self.db_path = db_path
        self.udp_port = udp_port
        self.driver_override = driver_override
        self.last_sender_ip = None
        self.drivers_file = os.path.join(os.path.dirname(os.path.abspath(db_path)), "drivers.json")
        self.ip_to_driver = self._load_drivers()
        self.default_driver = self.driver_override or self.ip_to_driver.get("default") or "seb"
        self.status = {
            "listening": False, "udp_port": udp_port, "packets": 0, "pps": 0,
            "packet_format": None, "session": None, "live": None,
            "last_lap": None, "ghosts": None, "warnings": [],
            "packet_sizes": {},   # pid -> observed byte size (layout probe)
            "cars": {},           # tracked car idx -> live buffer stats
        }
        self._status_lock = threading.Lock()
        self.live_telemetry = {"active": False}
        self.live_players = {}   # sender "ip:port" -> latest live telemetry
        self.engineer = engineer_mod.RaceEngineer(os.path.join(
            os.path.dirname(os.path.abspath(db_path)), "usage.json"))
        self._reset_session_state(None)

    def _load_drivers(self):
        try:
            if os.path.exists(self.drivers_file):
                with open(self.drivers_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, dict):
                        return data
        except Exception:
            pass
        return {}

    def _save_drivers(self):
        try:
            os.makedirs(os.path.dirname(self.drivers_file), exist_ok=True)
            with open(self.drivers_file, "w", encoding="utf-8") as f:
                json.dump(self.ip_to_driver, f, indent=2)
        except Exception:
            pass

    def set_driver_for_ip(self, ip, name):
        if not name:
            return
        name = name.strip()
        if ip:
            self.ip_to_driver[ip] = name
        self.ip_to_driver["default"] = name
        self.default_driver = name
        self._save_drivers()

    def _resolve_player_name(self, raw_name):
        if self.driver_override:
            return self.driver_override
        # 1. IP mapping takes precedence (e.g. from Oracle Cloud client or localhost)
        if self.last_sender_ip and self.last_sender_ip in self.ip_to_driver:
            val = self.ip_to_driver[self.last_sender_ip]
            if val and not str(val).isdigit():
                return str(val)
        # 2. If game sent an official F1 / AI driver name (e.g. NORRIS, VERSTAPPEN in local GP)
        if ids.is_f1_driver(raw_name):
            if self.last_sender_ip in ("127.0.0.1", "localhost", None):
                return self.ip_to_driver.get("127.0.0.1") or self.default_driver or "seb"
            return f"Driver ({self.last_sender_ip})"
        # 3. If game sent a real human gamertag, use it and remember for this IP
        if isinstance(raw_name, str) and raw_name.strip() and not raw_name.strip().isdigit():
            clean = raw_name.strip()
            if self.last_sender_ip and self.last_sender_ip not in self.ip_to_driver:
                self.ip_to_driver[self.last_sender_ip] = clean
                self._save_drivers()
            return clean
        return self.ip_to_driver.get("default") or self.default_driver or "seb"

    # ---------------------------------------------------------- state

    def _reset_session_state(self, uid):
        self.session_uid = uid
        self.session_row_id = None
        self.session_info = None
        self.track_length = 0
        self.player_idx = None
        self.bufs = {}          # car_idx -> LapBuffer
        self.motion = {}        # car_idx -> (x, y, z, glat, glong)
        self.telem = {}         # car_idx -> dict
        self.car_status = {}    # car_idx -> dict
        self.telem2 = {}        # car_idx -> dict
        self.setups = {}        # car_idx -> setup dict
        self.teams = {}         # car_idx -> team id
        self.driver_names = {}  # car_idx -> gamertag / driver name
        self.sess_assists = None   # player assist settings (Session packet)
        self.pb_idx = 255          # Time Trial ghost car indices
        self.rival_idx = 255
        self.tt = None             # TimeTrial packet datasets
        self.ghost_bufs = {}       # car_idx -> [(t_ms, dist), ...]
        self.stored_ghost_times = set()  # (role, lap_time_ms) dedupe
        self.damage = {}           # car_idx -> CarDamage dict
        self.session_live = None   # latest Session packet (weather, SC, ...)
        self.race = {"lap": None, "start_fuel": None, "used": [], "best_ms": 0}
        self.rivals = {}           # car_idx -> (tyre_visual, tyre_age), every car
        self.extra_t = 0.0         # last time the engineer got the full field

    _CTX_ATTRS = (
        "session_uid", "session_row_id", "session_info", "track_length",
        "player_idx", "bufs", "motion", "telem", "car_status", "telem2",
        "setups", "teams", "driver_names", "sess_assists", "pb_idx",
        "rival_idx", "tt", "ghost_bufs", "stored_ghost_times",
        "damage", "session_live", "race", "rivals", "extra_t")

    def _switch_sender(self, addr):
        """Each sender (IP:port) has its own session/lap state. Without
        this, two players sending to one server reset each other's
        session on every packet and no lap ever completes."""
        if not hasattr(self, "_contexts"):
            self._contexts = {}
            self._cur_ctx = None
        if addr == self._cur_ctx:
            return
        if self._cur_ctx is not None:
            self._contexts[self._cur_ctx] = {
                a: getattr(self, a) for a in self._CTX_ATTRS}
        saved = self._contexts.get(addr)
        if saved is None:
            self._reset_session_state(None)
        else:
            for a, v in saved.items():
                setattr(self, a, v)
        self._cur_ctx = addr

    def _set_status(self, **kw):
        with self._status_lock:
            self.status.update(kw)

    def get_status(self):
        with self._status_lock:
            return dict(self.status)

    def _sender_key(self):
        c = getattr(self, "_cur_ctx", None)
        return "%s:%s" % (c[0], c[1]) if c else "local"

    def get_live(self, sender=None):
        """Full live telemetry (with chart trail) of one sender; with no
        sender, the most recently updated one."""
        now = time.time()
        with self._status_lock:
            cands = [v for v in self.live_players.values()
                     if now - v.get("updated_at", 0) <= 3.0
                     and (sender is None or v.get("sender") == sender)]
            if not cands:
                return {"active": False}
            return dict(max(cands, key=lambda v: v["updated_at"]))

    def get_live_all(self):
        """Light summary of every sender currently live (no trail)."""
        now = time.time()
        with self._status_lock:
            for k in [k for k, v in self.live_players.items()
                      if now - v.get("updated_at", 0) > 10.0]:
                del self.live_players[k]
            out = []
            for v in self.live_players.values():
                if now - v.get("updated_at", 0) > 3.0:
                    continue
                out.append({k: v.get(k) for k in (
                    "sender", "driver_name", "track_id", "track_name",
                    "lap_num", "lap_time_ms", "speed", "team_name")})
            return out

    def _warn_once(self, msg):
        with self._status_lock:
            if msg not in self.status["warnings"]:
                self.status["warnings"].append(msg)
                print("[f1trace] WARNING: %s" % msg)

    # ---------------------------------------------------------- main loop

    def run(self):
        self.con = db.connect(self.db_path)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        try:
            sock.bind(("0.0.0.0", self.udp_port))
        except OSError:
            msg = ("UDP port %d is already in use — another TRACE instance "
                   "is probably running. Not recording." % self.udp_port)
            self._warn_once(msg)
            return
        sock.settimeout(1.0)
        self._set_status(listening=True)
        print("[f1trace] listening for telemetry on UDP %d" % self.udp_port)

        n_packets = 0
        window_start = time.time()
        window_count = 0
        while True:
            try:
                data, addr = sock.recvfrom(4096)
                if addr:
                    self.last_sender_ip = addr[0]
                    self._switch_sender(addr)
            except socket.timeout:
                now = time.time()
                if now - window_start >= 2.0:
                    self._set_status(pps=0)
                    window_start, window_count = now, 0
                continue
            n_packets += 1
            window_count += 1
            now = time.time()
            if now - window_start >= 2.0:
                self._set_status(pps=int(window_count / (now - window_start)),
                                 packets=n_packets)
                window_start, window_count = now, 0
            try:
                self._handle(data)
            except Exception as e:  # never let one bad packet kill the thread
                self._warn_once("packet handling error: %r" % e)

    # ---------------------------------------------------------- dispatch

    def _handle(self, data):
        if len(data) < packets.HEADER.size:
            return
        h = packets.Header(data)
        if h.packet_format not in (2025, 2026):
            self._warn_once("unknown packetFormat %d — game may need the "
                            "'F1 25 2026 Season Pack' UDP setting; layouts "
                            "may not match" % h.packet_format)
        if h.session_uid != self.session_uid and h.session_uid != 0:
            self._reset_session_state(h.session_uid)
        self.player_idx = h.player_car_index
        fmt = h.packet_format
        self._set_status(packet_format=fmt)

        pid = h.packet_id
        sizes = self.status["packet_sizes"]
        if sizes.get(str(pid)) != len(data):
            with self._status_lock:
                sizes[str(pid)] = len(data)

        if pid == packets.LAP_DATA:
            self._on_lap_data(data, fmt)
        elif pid == packets.MOTION:
            self.motion.update(packets.parse_motion(data, fmt, self._wanted()))
        elif pid == packets.CAR_TELEMETRY:
            self.telem.update(packets.parse_car_telemetry(data, fmt, self._wanted()))
        elif pid == packets.CAR_STATUS:
            self.car_status.update(packets.parse_car_status(data, fmt, self._wanted()))
            # every car's tyres, for the engineer's view of the field (1 Hz)
            if time.time() - getattr(self, "_rivals_t", 0) >= 1.0:
                self._rivals_t = time.time()
                allst = packets.parse_car_status(
                    data, fmt, range(packets.num_cars(fmt)))
                self.rivals = {i: (v["tyre_visual"], v["tyre_age"])
                               for i, v in allst.items()}
        elif pid == packets.CAR_DAMAGE:
            self.damage.update(packets.parse_car_damage(data, fmt, self._wanted()))
        elif pid == packets.CAR_TELEMETRY2 and fmt >= 2026:
            self.telem2.update(packets.parse_car_telemetry2(data, self._wanted()))
        elif pid == packets.CAR_SETUPS:
            self.setups.update(packets.parse_car_setups(data, fmt, self._wanted()))
        elif pid == packets.PARTICIPANTS:
            teams, names = packets.parse_participants(data, fmt)
            self.teams.update(teams)
            self.driver_names.update(names)
            if self.player_idx is not None and self.player_idx in names:
                raw = names[self.player_idx]
                if raw and not ids.is_f1_driver(raw) and self.last_sender_ip:
                    if self.ip_to_driver.get(self.last_sender_ip) != raw:
                        self.ip_to_driver[self.last_sender_ip] = raw
                        self._save_drivers()
        elif pid == packets.TIME_TRIAL:
            self.tt = packets.parse_time_trial(data, fmt)
        elif pid == packets.SESSION:
            self.sess_assists = packets.parse_session_assists(data)
            self._on_session(data, h)

    def _wanted(self):
        return set() if self.player_idx is None else {self.player_idx}

    # ---------------------------------------------------------- session

    def _on_session(self, data, h):
        s = packets.parse_session(data)
        self.track_length = s["track_length"]
        self.session_live = s
        if self.session_info is None:
            self.session_info = s
            cur = self.con.execute(
                "INSERT INTO sessions (uid, started_at, packet_format, game_year,"
                " track_id, track_name, session_type, session_type_name,"
                " weather, air_temp, track_temp, track_length, network_game)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (str(h.session_uid), _now(), h.packet_format, h.game_year,
                 s["track_id"], ids.track_name(s["track_id"]),
                 s["session_type"], ids.session_type_name(s["session_type"]),
                 s["weather"], s["air_temp"], s["track_temp"], s["track_length"],
                 s.get("network_game", 0)))
            self.con.commit()
            self.session_row_id = cur.lastrowid
            mp_tag = " (Multiplayer)" if s.get("network_game") else ""
            print("[f1trace] new session: %s %s%s" % (
                ids.track_name(s["track_id"]),
                ids.session_type_name(s["session_type"]),
                mp_tag))
        elif self.session_row_id and s.get("network_game") == 1:
            self.con.execute("UPDATE sessions SET network_game=1 WHERE id=? AND network_game=0",
                             (self.session_row_id,))
            self.con.commit()
        st_name = ids.session_type_name(s["session_type"])
        if s.get("network_game"):
            st_name += " (MP)"
        self._set_status(session={
            "id": self.session_row_id,
            "track": ids.track_name(s["track_id"]),
            "type": st_name,
        })

    # ---------------------------------------------------------- lap data

    def _on_lap_data(self, data, fmt):
        cars, pb_idx, rival_idx = packets.parse_lap_data(data, fmt)
        self.pb_idx, self.rival_idx = pb_idx, rival_idx
        # Time Trial designates a rival slot even with ghosts switched off
        # (it's the datum the HUD compares against) — only claim a pace is
        # being captured when the slot is actually driving.
        def driving(i):
            cl = cars.get(i)
            return (cl is not None and cl.current_lap_ms > 0
                    and cl.lap_distance >= 0)
        self._set_status(ghosts={"pb": driving(pb_idx),
                                 "rival": driving(rival_idx)})
        for gidx in {pb_idx, rival_idx} - {255, self.player_idx}:
            self._ghost_sample(gidx, cars.get(gidx))

        idx = self.player_idx
        cl = cars.get(idx) if idx is not None else None
        if cl is None:
            return

        buf = self.bufs.get(idx)
        if buf is None:
            buf = self.bufs[idx] = LapBuffer(cl.lap_num)
        if cl.lap_num != buf.lap_num:
            self._finalize(idx, buf,
                           cl.last_lap_ms if cl.lap_num == buf.lap_num + 1 else 0)
            buf = self.bufs[idx] = LapBuffer(cl.lap_num)

        # Before the line (out lap from the garage: negative distance) or
        # with the lap timer stopped there's no lap to record, but the
        # live view and the engineer still need the car.
        recording = cl.lap_distance >= 0 and cl.current_lap_ms > 0

        samples = buf.samples
        if samples and recording:
            last = samples[-1]
            if cl.current_lap_ms == last[0]:
                return  # duplicate frame
            if cl.current_lap_ms < last[0] or cl.lap_distance < last[1] - 1.0:
                # flashback / rewind / reset: drop samples past the
                # new position, and re-seed validity from this frame —
                # "restart lap" wipes the game's invalid flag (same lap
                # number, fresh valid run), while a plain flashback
                # keeps it set, so the frame itself is the truth
                while samples and samples[-1][1] >= cl.lap_distance:
                    samples.pop()
                buf.invalid = bool(cl.invalid)

        if cl.invalid and recording:
            buf.invalid = True
        if cl.s1_ms:
            buf.s1_ms = cl.s1_ms
        if cl.s2_ms:
            buf.s2_ms = cl.s2_ms

        m = self.motion.get(idx)
        if m is None:
            return  # wait for the first motion packet
        t = self.telem.get(idx) or {}
        st = self.car_status.get(idx) or {}
        t2 = self.telem2.get(idx) or {}
        tt = t.get("tyre_temp") or (0, 0, 0, 0)
        if recording:
            samples.append((
                cl.current_lap_ms,
                round(cl.lap_distance, 1),
                round(m[0], 2), round(m[1], 2), round(m[2], 2),
                t.get("speed", 0),
                int(round((t.get("throttle") or 0.0) * 100)),
                int(round((t.get("brake") or 0.0) * 100)),
                int(round((t.get("steer") or 0.0) * 100)),
                t.get("gear", 0),
                1 if t.get("drs") else 0,
                1 if t2.get("overtake") else 0,
                t.get("rpm", 0),
                tt[2], tt[3], tt[0], tt[1],  # order RL,RR,FL,FR on wire; store FL,FR,RL,RR
                round(st.get("fuel", 0.0), 2),
                round((st.get("ers_store") or 0.0) / 1e6, 3),
                1 if t2.get("aero_mode") else 0,  # 2026: X-mode(1)/Z-mode(0)
                # ERS deploy mode: 0 none, 1 medium, 2 hotlap, 3 overtake (boost)
                int(st.get("ers_mode") or 0),
            ))

        # Downsample current lap samples to at most ~500 points for progressive chart painting
        trail = None
        if samples and len(samples) > 1:
            step = max(1, len(samples) // 500)
            sub = samples[::step]
            if sub[-1] != samples[-1]:
                sub.append(samples[-1])
            trail = {
                "d": [s[1] for s in sub],
                "spd": [s[5] for s in sub],
                "thr": [s[6] for s in sub],
                "brk": [s[7] for s in sub],
                "str": [s[8] for s in sub],
                "gear": [s[9] for s in sub],
                "ers": [int(round(s[18] * 25)) for s in sub],
                "ersm": [s[20] for s in sub],
            }

        player_name = self._resolve_player_name(self.driver_names.get(idx))
        track_id = self.session_info["track_id"] if self.session_info else None
        self.live_telemetry = {
            "active": True,
            "track_id": track_id,
            "track_name": ids.track_name(track_id) if track_id is not None else "Track",
            "driver_name": player_name,
            "team_name": ids.team_name(self.teams.get(idx)),
            "lap_num": cl.lap_num,
            "lap_time_ms": cl.current_lap_ms,
            "distance": round(max(0.0, cl.lap_distance), 1),
            "x": round(m[0], 2),
            "y": round(m[1], 2),
            "z": round(m[2], 2),
            "speed": t.get("speed", 0),
            "throttle": int(round((t.get("throttle") or 0.0) * 100)),
            "brake": int(round((t.get("brake") or 0.0) * 100)),
            "steer": int(round((t.get("steer") or 0.0) * 100)),
            "gear": t.get("gear", 0),
            "rpm": t.get("rpm", 0),
            "ers": int(round(((st.get("ers_store") or 0.0) / 1e6) * 25)),
            "ers_mode": int(st.get("ers_mode") or 0),
            "drs": 1 if t.get("drs") else 0,
            "overtake": 1 if t2.get("overtake") else 0,
            "aero": 1 if t2.get("aero_mode") else 0,
            "invalid": bool(buf.invalid),
            "race": self._race_view(cars, cl, t, st),
            "trail": trail,
            "updated_at": time.time(),
        }
        sender = self._sender_key()
        self.live_telemetry["sender"] = sender
        with self._status_lock:
            self.live_players[sender] = self.live_telemetry
        if self.session_live:
            self.engineer.observe(
                sender, self.live_telemetry,
                self.session_live.get("session_type"),
                self._neighbour_names(cars, cl), self._engineer_extra(cars))

        self._set_status(
            live={
                "lap_num": cl.lap_num,
                "lap_time_ms": cl.current_lap_ms,
                "distance": int(cl.lap_distance),
                "speed": t.get("speed", 0),
                "driver_name": player_name,
                "track_id": track_id,
                "x": round(m[0], 2),
                "z": round(m[2], 2),
            },
            cars={str(i): {"role": "player", "lap_num": b.lap_num,
                           "samples": len(b.samples)}
                  for i, b in self.bufs.items()})

    def _engineer_extra(self, cars):
        """Once a second: the whole field, the player's setup and assists,
        for the engineer's answers. None in between (it keeps the last)."""
        now = time.time()
        if now - self.extra_t < 1.0:
            return None
        self.extra_t = now
        field = []
        for i, c in cars.items():
            if not c.position or c.result_status < 2:
                continue
            n = self.driver_names.get(i)
            if i == self.player_idx:
                n = self._resolve_player_name(n)
            tyre = self.rivals.get(i)
            field.append({
                "pos": c.position, "name": n,
                "team": ids.team_name(self.teams.get(i)),
                "me": i == self.player_idx, "lap": c.lap_num,
                "last_lap_ms": c.last_lap_ms or None,
                "gap_leader_ms": c.gap_leader_ms if c.position > 1 else 0,
                "gap_front_ms": c.gap_front_ms if c.position > 1 else 0,
                "pit_stops": c.pit_stops, "in_pit": c.pit_status > 0,
                "penalties_s": c.penalties_s,
                "tyre_visual": tyre[0] if tyre else None,
                "tyre_age": tyre[1] if tyre else None,
                "finished": c.result_status == 3,
            })
        field.sort(key=lambda x: x["pos"])
        return {"field": field,
                "setup": self.setups.get(self.player_idx),
                "assists": self.sess_assists}

    def _neighbour_names(self, cars, cl):
        """Names of the cars just ahead / behind, when they look real."""
        out = {}
        for key, pos in (("ahead", cl.position - 1), ("behind", cl.position + 1)):
            i = next((i for i, c in cars.items() if c.position == pos
                      and c.result_status >= 2), None)
            n = self.driver_names.get(i) if i is not None else None
            if n and not str(n).isdigit() and str(n).lower() != "player":
                out[key] = str(n).title() if str(n).isupper() else str(n)
        return out

    def _race_view(self, cars, cl, t, st):
        """Race-engineer snapshot for the RACE page: tyres, fuel, gaps,
        damage. Corner arrays come off the wire as RL, RR, FL, FR and are
        reordered to FL, FR, RL, RR."""
        r = self.race
        fuel = st.get("fuel")
        if r["lap"] != cl.lap_num:
            if r["lap"] is not None and cl.lap_num == r["lap"] + 1:
                if r["start_fuel"] is not None and fuel is not None:
                    used = r["start_fuel"] - fuel
                    if 0 < used < 10:   # skip refuels / restarts
                        r["used"] = (r["used"] + [round(used, 3)])[-5:]
                if cl.last_lap_ms and (not r["best_ms"] or cl.last_lap_ms < r["best_ms"]):
                    r["best_ms"] = cl.last_lap_ms
            r["lap"], r["start_fuel"] = cl.lap_num, fuel

        def corners(v):
            return [v[2], v[3], v[0], v[1]] if v and len(v) == 4 else None

        behind = next((c for c in cars.values()
                       if c.position == cl.position + 1
                       and c.result_status >= 2), None)
        n_cars = sum(1 for c in cars.values()
                     if c.position and c.result_status >= 2)
        per_lap = (sum(r["used"]) / len(r["used"])) if r["used"] else None
        dmg = self.damage.get(self.player_idx)
        sl = self.session_live or {}
        return {
            "season": 2026 if (self.status.get("packet_format") or 0) >= 2026 else 2025,
            "position": cl.position, "num_cars": n_cars, "grid": cl.grid,
            "lap_num": cl.lap_num, "total_laps": sl.get("total_laps"),
            "last_lap_ms": cl.last_lap_ms, "best_lap_ms": r["best_ms"],
            "gap_front_ms": cl.gap_front_ms if cl.position > 1 else None,
            "gap_leader_ms": cl.gap_leader_ms if cl.position > 1 else None,
            "gap_behind_ms": behind.gap_front_ms if behind else None,
            "pit_status": cl.pit_status, "pit_stops": cl.pit_stops,
            "penalties_s": cl.penalties_s, "warnings": cl.warnings,
            "sector": cl.sector,
            "session_type": ids.session_type_name(sl["session_type"]) if sl else None,
            "weather": sl.get("weather"), "track_temp": sl.get("track_temp"),
            "air_temp": sl.get("air_temp"), "safety_car": sl.get("safety_car", 0),
            "time_left": sl.get("time_left"),
            "forecast": (sl.get("forecast") or [])[:8],
            "pit_window": sl.get("pit_window"),
            "current_lap_ms": cl.current_lap_ms,
            "lap_distance": round(cl.lap_distance, 1),
            "driver_status": cl.driver_status,
            "track_length": self.track_length,
            "s1_ms": cl.s1_ms, "s2_ms": cl.s2_ms,
            "lap_invalid": bool(cl.invalid),
            "tyres": {
                "actual": st.get("tyre_actual"), "visual": st.get("tyre_visual"),
                "age": st.get("tyre_age"),
                "surface": corners(t.get("tyre_temp")),
                "inner": corners(t.get("tyre_inner")),
                "pressure": [round(x, 1) for x in corners(t.get("tyre_press")) or []] or None,
                "brake": corners(t.get("brake_temp")),
                "wear": corners(dmg["tyre_wear"]) if dmg else None,
                "damage": corners(dmg["tyre_damage"]) if dmg else None,
                "blisters": corners(dmg["blisters"]) if dmg else None,
            },
            "fuel": {
                "kg": round(fuel, 2) if fuel is not None else None,
                "capacity": st.get("fuel_capacity"),
                "laps_surplus": round(st["fuel_laps"], 2) if st.get("fuel_laps") is not None else None,
                "per_lap": round(per_lap, 3) if per_lap else None,
                "laps_left": round(fuel / per_lap, 1) if per_lap and fuel else None,
                "mix": st.get("fuel_mix"),
            },
            "ers_pct": round((st.get("ers_store") or 0.0) / 4e4, 1),
            "ers_mode": int(st.get("ers_mode") or 0),
            "engine_temp": t.get("engine_temp"),
            "brake_bias": st.get("brake_bias"),
            "pit_limiter": st.get("pit_limiter"),
            "damage": {k: v for k, v in dmg.items()
                       if k not in ("tyre_wear", "tyre_damage", "blisters")} if dmg else None,
        }

    COLUMNS = ("t", "d", "x", "y", "z", "spd", "thr", "brk", "str",
               "gear", "drs", "ot", "rpm", "tfl", "tfr", "trl", "trr",
               "fuel", "ers", "aero", "ersm")

    # ------------------------------------------------- ghost pace reference

    def _ghost_sample(self, idx, cl):
        return  # Omit ghost laps completely

    def _finalize_ghost(self, idx, buf, lap_time_ms):
        return  # Omit ghost laps completely
        role = "pb_ghost" if idx == self.pb_idx else "rival"
        if lap_time_ms <= 0 or self.session_row_id is None:
            return
        # a ghost that finishes before the player parks at the line while
        # the shared lap clock keeps counting — cut that filler tail
        samples = [s for s in buf if s[0] <= lap_time_ms]
        if len(samples) < 50:
            return
        span = samples[-1][1] - samples[0][1]
        if self.track_length and (span < 0.9 * self.track_length or
                                  samples[-1][1] < self.track_length - 50):
            return  # partial loop (player restart rewinds the ghost mid-lap)
        key = (role, lap_time_ms)
        if key in self.stored_ghost_times:
            return  # the same ghost lap repeats every player lap
        self.stored_ghost_times.add(key)

        # times, team and assists come from the TimeTrial packet — the
        # only officially supported ghost data
        s1 = s2 = 0
        team = None
        assists = None
        ds = self.tt.get("rival" if role == "rival"
                         else "personal_best") if self.tt else None
        if ds and ds.get("car_idx") == idx and ds.get("lap_ms") == lap_time_ms:
            s1, s2 = ds["s1_ms"], ds["s2_ms"]
            team = ds["team"] or None
            assists = {"tc": ds["tc"], "abs": ds["abs"],
                       "gearbox": ds["gearbox"],
                       "custom_setup": ds["custom_setup"],
                       "equal_perf": ds["equal_perf"]}
        s3 = lap_time_ms - s1 - s2 if s1 and s2 else 0
        cols = {"t": [t for t, _ in samples], "d": [d for _, d in samples]}
        self.con.execute(
            "INSERT INTO laps (session_id, car_role, car_index, lap_num,"
            " lap_time_ms, s1_ms, s2_ms, s3_ms, valid, tyre_visual,"
            " top_speed, n_samples, created_at, samples, setup, assists,"
            " team_id)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_row_id, role, idx, 1, lap_time_ms,
             s1, s2, s3, 1, None, None, len(samples), _now(),
             db.pack_samples(cols),
             None, json.dumps(assists) if assists else None, team))
        self.con.commit()
        print("[f1trace] stored %s pace reference — %s"
              % (role, _fmt_time(lap_time_ms)))
        self._set_status(last_lap={
            "role": role, "lap_num": 1,
            "lap_time_ms": lap_time_ms, "valid": True,
        })

    # ------------------------------------------------- player lap finalize

    def _assists_for(self, st):
        """Assist settings for a finishing lap: per-car TC/ABS from
        CarStatus plus the Session-packet settings."""
        a = {}
        if "tc" in st:
            a["tc"] = st["tc"]
            a["abs"] = st["abs"]
        if self.sess_assists:
            a.update(self.sess_assists)
        return a

    def _finalize(self, idx, buf, lap_time_ms):
        samples = buf.samples
        if lap_time_ms <= 0:
            return  # lap counter jumped or time missing
        if len(samples) < 50:
            return
        if self.session_row_id is None:
            return
        span = samples[-1][1] - samples[0][1]
        if self.track_length and span < 0.9 * self.track_length:
            return  # partial lap

        s1, s2 = buf.s1_ms, buf.s2_ms
        s3 = lap_time_ms - s1 - s2 if s1 and s2 else 0
        cols = {name: [s[i] for s in samples]
                for i, name in enumerate(self.COLUMNS)}
        st = self.car_status.get(idx) or {}
        assists = self._assists_for(st)
        setup = self.setups.get(idx)
        raw_name = self.driver_names.get(idx)
        driver_name = self._resolve_player_name(raw_name)
        self.con.execute(
            "INSERT INTO laps (session_id, car_role, car_index, lap_num,"
            " lap_time_ms, s1_ms, s2_ms, s3_ms, valid, tyre_visual,"
            " top_speed, n_samples, created_at, samples, setup, assists,"
            " team_id, driver_name)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (self.session_row_id, "player", idx, buf.lap_num, lap_time_ms,
             s1, s2, s3, 0 if buf.invalid else 1,
             st.get("tyre_visual"), max(cols["spd"]) if cols["spd"] else 0,
             len(samples), _now(), db.pack_samples(cols),
             json.dumps(setup) if setup else None,
             json.dumps(assists) if assists else None,
             self.teams.get(idx), driver_name))
        self.con.commit()
        print("[f1trace] stored lap %d%s — %s%s" % (
            buf.lap_num,
            (" (%s)" % driver_name) if driver_name else "",
            _fmt_time(lap_time_ms),
            "" if not buf.invalid else " (invalid)"))
        self._set_status(last_lap={
            "role": "player", "lap_num": buf.lap_num,
            "lap_time_ms": lap_time_ms, "valid": not buf.invalid,
        })
        track_id = self.session_info["track_id"] if self.session_info else None
        try:
            self.engineer.lap_done(
                self._sender_key(), cols, lap_time_ms, not buf.invalid,
                [s1, s2, s3] if s1 and s2 else None, track_id,
                self.track_length, buf.lap_num,
                lambda: self._best_lap(track_id, driver_name, lap_time_ms))
        except Exception as e:      # coaching must never stop recording
            print("[engineer] lap_done: %r" % e)


    def _best_lap(self, track_id, driver_name, faster_than):
        """This driver's best valid lap ever at this track (earlier
        sessions included), as the engineer's reference when it has none
        in memory (first lap of a weekend, or after a restart)."""
        row = self.con.execute(
            "SELECT l.lap_time_ms, l.s1_ms, l.s2_ms, l.s3_ms, l.samples,"
            " l.created_at FROM laps l JOIN sessions s ON s.id = l.session_id"
            " WHERE l.car_role = 'player' AND l.valid = 1 AND s.track_id = ?"
            " AND l.driver_name = ? AND l.lap_time_ms != ?"
            " ORDER BY l.lap_time_ms LIMIT 1",
            (track_id, driver_name, faster_than)).fetchone()
        if not row:
            return None
        return {"ms": row[0], "sectors": list(row[1:4]) if row[1] else None,
                "cols": db.unpack_samples(row[4]),
                "session": "histórica en este circuito, %s" % (row[5] or "")[:10]}


def _fmt_time(ms):
    return "%d:%06.3f" % (ms // 60000, (ms % 60000) / 1000.0)
