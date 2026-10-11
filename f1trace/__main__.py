"""Entry point: `python3 -m f1trace` starts the UDP recorder and the viewer."""

import argparse
import os
import shutil
import socket
import tempfile
import webbrowser

from . import __version__
from . import recorder as recorder_mod
from . import server


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # no traffic sent; just picks the route
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(
        prog="f1trace",
        description="TRACE — telemetry recorder + viewer for F1 25")
    ap.add_argument("--version", action="version",
                    version="TRACE %s" % __version__)
    ap.add_argument("--udp-port", type=int, default=20777,
                    help="UDP port the game broadcasts to (default 20777)")
    ap.add_argument("--http-port", type=int, default=8020,
                    help="viewer web port (default 8020)")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address the viewer listens on (default 127.0.0.1, "
                         "local only; use 0.0.0.0 to reach it from other "
                         "devices on the network)")
    ap.add_argument("--db", default=None, help="database file path")
    ap.add_argument("--demo", action="store_true",
                    help="browse the bundled example laps; no game or "
                         "recording involved")
    ap.add_argument("--no-browser", action="store_true",
                    help="don't open the viewer in a browser on startup")
    args = ap.parse_args()

    if args.demo:
        # Work on a throwaway copy so the bundled file is never written to
        # (WAL sidecars, deletes from the viewer).
        db_path = os.path.join(tempfile.mkdtemp(prefix="f1trace-demo-"),
                               "demo.db")
        shutil.copyfile(os.path.join(os.path.dirname(
            os.path.abspath(__file__)), "demo.db"), db_path)
    else:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "data")
        default = os.path.join(data_dir, "f1trace.db")
        legacy = os.path.join(data_dir, "f1lab.db")  # pre-rename databases
        if not os.path.exists(default) and os.path.exists(legacy):
            default = legacy
        db_path = args.db or default
        os.makedirs(os.path.dirname(db_path), exist_ok=True)

    print("=" * 62)
    if args.demo:
        print("  TRACE %s — demo: bundled Melbourne laps, no recording"
              % __version__)
    else:
        print("  TRACE %s — telemetry recorder + viewer" % __version__)
        print("  In the game set:  Settings > Telemetry")
        print("    UDP Telemetry: On    UDP Broadcast: Off")
        print("    UDP IP Address: %s   Port: %d" % (lan_ip(), args.udp_port))
        print("    UDP Send Rate: 60Hz  UDP Format: F1 25 2026 Season Pack")
    print("  Viewer:  http://localhost:%d" % args.http_port)
    print("=" * 62)

    rec = None
    if not args.demo:
        rec = recorder_mod.Recorder(db_path, udp_port=args.udp_port)
        rec.start()
    try:
        server.serve(db_path, rec, http_port=args.http_port, demo=args.demo,
                     open_browser=not args.no_browser, host=args.host)
    except KeyboardInterrupt:
        print("\n[f1trace] bye")
    except OSError:
        print("\n[f1trace] port %d is already in use — TRACE seems to be "
              "running already.\n  Open http://localhost:%d in your browser, "
              "or stop the other instance first,\n  or start with "
              "--http-port/--udp-port to use different ports."
              % (args.http_port, args.http_port))
        if not args.no_browser:
            webbrowser.open("http://localhost:%d" % args.http_port)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
