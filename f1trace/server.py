"""Local web server: JSON API + static viewer files. Stdlib only.

Each request thread opens its own read-only SQLite connection; the
recorder thread is the sole writer (WAL mode allows concurrent reads).
"""

import gzip
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, db, ids

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

_local = threading.local()

# columns of the sessions table that travel inside a .trace export
EXPORT_SESSION_FIELDS = (
    "uid", "started_at", "packet_format", "game_year", "track_id",
    "track_name", "session_type", "session_type_name",
    "weather", "air_temp", "track_temp", "track_length")


def _opt_int(v):
    return None if v is None else int(v)


def _clean_timestamp(v):
    """A timestamp string from an imported file, or now if it looks off."""
    v = str(v or "")
    if re.fullmatch(r"[0-9T:. -]{4,32}", v):
        return v
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _clean_meta(d):
    """Setup/assists dict from an imported file: numeric values only."""
    if not isinstance(d, dict):
        return None
    out = {str(k)[:48]: v for k, v in d.items()
           if isinstance(v, (int, float, bool))}
    return out or None


def make_handler(db_path, recorder, demo=False):

    def get_con():
        if getattr(_local, "con", None) is None:
            _local.con = sqlite3.connect(db_path)
            _local.con.row_factory = sqlite3.Row
        return _local.con

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        # ------------------------------------------------ responses

        def _send(self, code, body, ctype):
            raw = body if isinstance(body, bytes) else body.encode()
            use_gzip = (len(raw) > 1024 and
                        "gzip" in self.headers.get("Accept-Encoding", ""))
            if use_gzip:
                raw = gzip.compress(raw, 5)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            if use_gzip:
                self.send_header("Content-Encoding", "gzip")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, separators=(",", ":")),
                       "application/json")

        # ------------------------------------------------ routing

        def do_GET(self):
            path = self.path.split("?")[0]
            try:
                if path == "/api/status":
                    st = (recorder.get_status() if recorder
                          else {"listening": False})
                    st["version"] = __version__
                    if demo:
                        st["demo"] = True
                    return self._json(st)
                if path == "/api/tracks":
                    return self.tracks()
                m = re.fullmatch(r"/api/tracks/(-?\d+)/laps", path)
                if m:
                    return self.track_laps(int(m.group(1)))
                m = re.fullmatch(r"/api/laps/(\d+)/export", path)
                if m:
                    return self.export_lap(int(m.group(1)))
                m = re.fullmatch(r"/api/laps/(\d+)", path)
                if m:
                    return self.lap(int(m.group(1)))
                return self.static(path)
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": repr(e)}, 500)

        def do_DELETE(self):
            path = self.path.split("?")[0]
            if path == "/api/laps/invalid":
                con = get_con()
                cur = con.execute("DELETE FROM laps WHERE valid=0")
                con.commit()
                return self._json({"ok": True, "deleted": cur.rowcount})
            m = re.fullmatch(r"/api/laps/(\d+)", path)
            if m:
                con = get_con()
                con.execute("DELETE FROM laps WHERE id=?", (int(m.group(1)),))
                con.commit()
                return self._json({"ok": True})
            m = re.fullmatch(r"/api/sessions/(\d+)", self.path.split("?")[0])
            if m:
                con = get_con()
                con.execute("PRAGMA foreign_keys=ON")
                con.execute("DELETE FROM sessions WHERE id=?", (int(m.group(1)),))
                con.commit()
                return self._json({"ok": True})
            self._json({"error": "not found"}, 404)

        def do_POST(self):
            path = self.path.split("?")[0]
            try:
                if path == "/api/import":
                    n = int(self.headers.get("Content-Length") or 0)
                    if not 0 < n <= 32 * 1024 * 1024:
                        return self._json({"error": "file too large"}, 413)
                    return self.import_trace(self.rfile.read(n))
                self._json({"error": "not found"}, 404)
            except BrokenPipeError:
                pass
            except Exception as e:
                self._json({"error": repr(e)}, 500)

        # ------------------------------------------------ api

        def tracks(self):
            rows = get_con().execute(
                "SELECT s.track_id, s.track_name, COUNT(l.id) AS n_laps,"
                " MIN(CASE WHEN l.car_role='player' AND l.valid=1"
                "     THEN l.lap_time_ms END) AS best_ms,"
                " MAX(s.started_at) AS last_at"
                " FROM sessions s JOIN laps l ON l.session_id = s.id"
                " GROUP BY s.track_id ORDER BY last_at DESC").fetchall()
            self._json([dict(r) for r in rows])

        def track_laps(self, track_id):
            rows = get_con().execute(
                "SELECT l.id, l.car_role, l.car_index, l.lap_num,"
                " l.lap_time_ms, l.s1_ms, l.s2_ms, l.s3_ms, l.valid,"
                " l.tyre_visual, l.top_speed, l.n_samples, l.created_at,"
                " l.assists, l.setup IS NOT NULL AS has_setup, l.team_id,"
                " l.session_id, s.started_at, s.session_type_name,"
                " s.packet_format"
                " FROM laps l JOIN sessions s ON s.id = l.session_id"
                " WHERE s.track_id=?"
                " ORDER BY s.id DESC, l.id DESC", (track_id,)).fetchall()
            out = []
            for r in rows:
                d = dict(r)
                d["assists"] = json.loads(d["assists"]) if d["assists"] else None
                d["team_name"] = ids.team_name(d["team_id"])
                out.append(d)
            self._json(out)

        def lap(self, lap_id):
            row = get_con().execute(
                "SELECT l.*, s.track_id, s.track_name, s.track_length,"
                " s.session_type_name"
                " FROM laps l JOIN sessions s ON s.id = l.session_id"
                " WHERE l.id=?", (lap_id,)).fetchone()
            if row is None:
                return self._json({"error": "not found"}, 404)
            meta = {k: row[k] for k in row.keys() if k != "samples"}
            for k in ("setup", "assists"):
                meta[k] = json.loads(meta[k]) if meta.get(k) else None
            meta["team_name"] = ids.team_name(meta.get("team_id"))
            meta["samples"] = db.unpack_samples(row["samples"])
            self._json(meta)

        # ------------------------------------------------ export / import
        # A .trace file is one lap + its session metadata as gzipped JSON,
        # made to be sent to a friend and dropped onto their TRACE window.

        def export_lap(self, lap_id):
            con = get_con()
            lap = con.execute("SELECT * FROM laps WHERE id=?",
                              (lap_id,)).fetchone()
            if lap is None:
                return self._json({"error": "not found"}, 404)
            sess = con.execute("SELECT * FROM sessions WHERE id=?",
                               (lap["session_id"],)).fetchone()
            meta = {k: lap[k] for k in lap.keys()
                    if k not in ("id", "session_id", "samples")}
            for k in ("setup", "assists"):
                meta[k] = json.loads(meta[k]) if meta[k] else None
            doc = {
                "trace": 1,                       # export format version
                "exported_by": "TRACE " + __version__,
                "session": {k: sess[k] for k in EXPORT_SESSION_FIELDS},
                "lap": meta,
                "samples": db.unpack_samples(lap["samples"]),
            }
            raw = gzip.compress(
                json.dumps(doc, separators=(",", ":")).encode(), 9)
            track = re.sub(r"[^a-z0-9]+", "",
                           (sess["track_name"] or "").lower()) or "track"
            t = lap["lap_time_ms"] or 0
            fname = "%s-%d.%06.3f.trace" % (
                track, t // 60000, (t % 60000) / 1000.0)
            self.send_response(200)
            self.send_header("Content-Type", "application/gzip")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % fname)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        def import_trace(self, body):
            try:
                if body[:2] == b"\x1f\x8b":
                    body = gzip.decompress(body)
                doc = json.loads(body)
                ver = doc["trace"]
                sess, lap, samples = doc["session"], doc["lap"], doc["samples"]
            except Exception:
                return self._json({"error": "not a TRACE lap file"}, 400)
            if not isinstance(ver, int) or ver < 1:
                return self._json({"error": "not a TRACE lap file"}, 400)
            if ver > 1:
                return self._json(
                    {"error": "made by a newer TRACE — update this one"}, 400)
            if (not isinstance(samples, dict)
                    or "t" not in samples or "d" not in samples
                    or any(not isinstance(v, list) for v in samples.values())
                    or len({len(v) for v in samples.values()}) != 1
                    or len(samples["t"]) < 2
                    or any(x is not None and not isinstance(x, (int, float))
                           for v in samples.values() for x in v)):
                return self._json({"error": "corrupt lap file"}, 400)
            try:
                # names and team labels are re-derived from ids on our side;
                # nothing string-typed from the file reaches the viewer
                track_id = int(sess["track_id"])
                session_type = _opt_int(sess.get("session_type"))
                lap_time_ms = _opt_int(lap.get("lap_time_ms"))
                con = get_con()
                uid = str(sess.get("uid") or "")[:32]
                row = con.execute(
                    "SELECT id FROM sessions WHERE uid=? AND track_id=?",
                    (uid, track_id)).fetchone()
                if row:
                    sid = row["id"]
                else:
                    cur = con.execute(
                        "INSERT INTO sessions (uid, started_at, packet_format,"
                        " game_year, track_id, track_name, session_type,"
                        " session_type_name, weather, air_temp, track_temp,"
                        " track_length) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                        (uid, _clean_timestamp(sess.get("started_at")),
                         _opt_int(sess.get("packet_format")),
                         _opt_int(sess.get("game_year")), track_id,
                         ids.track_name(track_id), session_type,
                         ids.session_type_name(session_type),
                         _opt_int(sess.get("weather")),
                         _opt_int(sess.get("air_temp")),
                         _opt_int(sess.get("track_temp")),
                         _opt_int(sess.get("track_length"))))
                    sid = cur.lastrowid
                n = len(samples["t"])
                dup = con.execute(
                    "SELECT id FROM laps WHERE session_id=? AND"
                    " lap_time_ms IS ? AND n_samples=?",
                    (sid, lap_time_ms, n)).fetchone()
                if dup:
                    con.commit()   # the session row may be new
                    return self._json({"ok": True, "lap_id": dup["id"],
                                       "track_id": track_id,
                                       "duplicate": True})
                setup, assists = (_clean_meta(lap.get(k))
                                  for k in ("setup", "assists"))
                cur = con.execute(
                    "INSERT INTO laps (session_id, car_role, car_index,"
                    " lap_num, lap_time_ms, s1_ms, s2_ms, s3_ms, valid,"
                    " tyre_visual, top_speed, n_samples, created_at, samples,"
                    " setup, assists, team_id)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (sid, "guest", _opt_int(lap.get("car_index")),
                     _opt_int(lap.get("lap_num")), lap_time_ms,
                     _opt_int(lap.get("s1_ms")), _opt_int(lap.get("s2_ms")),
                     _opt_int(lap.get("s3_ms")), 1 if lap.get("valid") else 0,
                     _opt_int(lap.get("tyre_visual")),
                     _opt_int(lap.get("top_speed")), n,
                     _clean_timestamp(lap.get("created_at")),
                     db.pack_samples(samples),
                     json.dumps(setup) if setup else None,
                     json.dumps(assists) if assists else None,
                     _opt_int(lap.get("team_id"))))
                con.commit()
            except (KeyError, TypeError, ValueError):
                return self._json({"error": "corrupt lap file"}, 400)
            return self._json({"ok": True, "lap_id": cur.lastrowid,
                               "track_id": track_id, "duplicate": False})

        # ------------------------------------------------ static

        def static(self, path):
            if path in ("/", "/index.html"):
                path = "/index.html"
            # Resolve to a real path and confirm it stays inside STATIC_DIR:
            # normpath alone leaves a leading "../" in place, and the joined
            # string still starts with STATIC_DIR, so compare real paths.
            full = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
            root = os.path.realpath(STATIC_DIR)
            if os.path.commonpath([full, root]) != root or \
                    not os.path.isfile(full):
                return self._json({"error": "not found"}, 404)
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            with open(full, "rb") as f:
                self._send(200, f.read(), ctype)

    return Handler


def serve(db_path, recorder, http_port=8020, demo=False, open_browser=False,
          host="127.0.0.1"):
    server = ThreadingHTTPServer((host, http_port),
                                 make_handler(db_path, recorder, demo))
    print("[f1trace] viewer at http://localhost:%d" % http_port)
    if open_browser:
        webbrowser.open("http://localhost:%d" % http_port)
    server.serve_forever()
