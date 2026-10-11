# TRACE — Racing Sim Telemetry Workbench for F1 25

TRACE — the **T**elemetry **R**ecording **A**nd **C**omparison **E**ngine — records laps
driven in **F1 25, the EA / Codemasters racing game**, for the 2026
Season Pack. The game broadcasts live telemetry on track;
TRACE captures every completed player lap — and the Time Trial ghosts'
pace — keeps laps across sessions, and replays and compares any two:
track map, dashboard, input traces, time delta, and a badge on every
corner that costs time. The point is to show where a faster lap gains:
which corners, and whether it's braking or throttle.

*Unofficial fan project — not affiliated with Formula 1 or EA/Codemasters.*

**[Browser demo](https://lmc4s.github.io/F1-25-Trace/)** — the bundled
demo laps in the full viewer, nothing to install.

![Two laps compared: speed-colored racing line, green corner badges, telemetry charts and delta trace](docs/img/compare-speed.png)
*Two laps compared: racing line colored by speed, a badge on every
corner where the reference gains 0.1 s or more, input traces and the
time-delta graph.*

## Run

Requires Python 3. TRACE uses only the standard library, so there is
nothing to `pip install`.

**With a local copy of the project** — double-click `TRACE.bat`
(Windows) or `TRACE.command` (Mac), or run `python3 -m f1trace` from a
terminal in the project folder. The viewer opens in the browser;
closing the terminal window stops TRACE.

**Without one** — either command downloads the project and starts it.

Mac (Terminal):

```bash
curl -L https://github.com/LMC4S/F1-25-Trace/archive/refs/heads/main.tar.gz | tar xz && cd F1-25-Trace-main && python3 -m f1trace
```

Windows (PowerShell):

```powershell
iwr https://github.com/LMC4S/F1-25-Trace/archive/refs/heads/main.zip -OutFile F1-25-Trace.zip; Expand-Archive F1-25-Trace.zip . -Force; cd F1-25-Trace-main; python -m f1trace
```

The game can run on the same PC or a different one on the network — the
recorder receives the telemetry either way:

```
 F1 25 (PC or console)                             TRACE (laptop or desktop)
 ┌───────────────────────┐                         ┌──────────────────────────┐
 │ broadcasts telemetry  │ ────────  UDP  ───────▶ │ recorder → data/*.db     │
 │ over UDP port 20777   │                         │ viewer at localhost:8020 │
 └───────────────────────┘                         │ browser (same machine)   │
                                                   └──────────────────────────┘
```

Details:

- Recorder listens on UDP **20777** (all interfaces, so the game may be on
  another PC); viewer at **http://localhost:8020**, reachable only from
  this computer; laps stored in `data/f1trace.db` (SQLite)
- Options: `--udp-port`, `--http-port`, `--db`, `--no-browser`, `--host`
  (viewer bind address; defaults to `127.0.0.1` — set `0.0.0.0` to open the
  viewer to other devices on the network), `--version`
- `python3 -m f1trace --demo` — the bundled Melbourne laps in the
  local viewer, no game and no recording; the browser demo linked above,
  but offline
- Mac: if the project came as a zip download, the first double-click may
  be blocked — right-click the launcher and choose **Open** once
- The Windows launcher hasn't been tried yet; `python -m f1trace` from a
  terminal is the fallback

## Game settings (on the PC running the game)

`Settings → Telemetry`:

| Setting | Value |
| --- | --- |
| UDP Telemetry | On |
| UDP Broadcast Mode | Off |
| UDP IP Address | this machine's LAN IP (printed at startup) |
| UDP Port | 20777 |
| UDP Send Rate | 60 Hz |
| UDP Format | **F1 25: 2026 Season Pack** (2025 base format also supported) |

Every completed lap is stored automatically; in Time Trial the ghosts'
pace is captured alongside (see below).

One caveat: game updates have been seen to quietly reset **UDP Format**
to the base F1 25 value — worth re-checking after a patch.

### Connection status

The chip next to the logo in the viewer header shows whether telemetry
is arriving:

| Chip | Meaning |
| --- | --- |
| `LIVE … pps` | receiving — the number is packets per second, and the track / session / live lap time show next to it |
| `IDLE` | listening but nothing arriving. Normal in menus (the game only broadcasts on track); if it stays IDLE while driving, re-check the IP address and port above |
| `OFFLINE` | no recorder — usually another TRACE instance already holds the UDP port |

On track, the chip flips to `LIVE` within a couple of seconds.

## Ghost laps: pace references, times only

In Time Trial the game shows a ghost — the personal best, or any
leaderboard entry loaded as the rival. TRACE captures that ghost as a
**pace reference**: its lap and sector times, and its lap clock against
track distance. Pace rows carry a `RIVAL` or `PB·G` badge; clicking one
starts a comparison, which yields the corner badges, the DELTA graph,
GAP-mode track dominance and a dot on the map pulling ahead or falling
behind — which corner the faster driver gains in, and how much.

A pace reference deliberately carries no throttle, brake or steering.
The game does not broadcast honest telemetry for the shadow car — its
input channels interleave genuine-looking frames with flat-out
placeholder junk, varying by session — and the official UDP spec
supports exactly the data TRACE keeps: times (the TimeTrial packet)
and the ghost's progress along the lap. Versions up to 0.1.3 stored
the full fabricated stream; since 0.1.4 only the real subset is
stored, and previously recorded ghost laps are converted to pace
references on first start. The full story is in
[docs/design-notes.md](docs/design-notes.md).

Complete laps with input traces come from two sources: the player's
own laps, and laps other TRACE users share as `.trace` files (see
[Sharing laps](#sharing-laps)).

The ghost car must stay enabled in the game — an invisible ghost is
not broadcast at all. While driving, a `RIVAL PACE ✓` chip in the
header confirms the capture is live.

## Browsing and comparing laps

Everything the recorder has stored is on the web page it serves at
`http://localhost:8020`:

- Pick a **track** in the header dropdown: every lap ever recorded on it,
  from all sessions, in one list (grouped by session). Sort **RECENT** or
  **FASTEST** (ranked, with gaps), filter YOU / GHOSTS (pace references
  and imported laps) and assists on/off, hide invalid laps.
- **SETUP** shows the viewed lap's car setup and assist settings (TC, ABS,
  gearbox, racing line…) — side by side with the reference lap's.
- Click a lap to replay it: dot on the track map + instrument cluster
  (speed, gear, throttle/brake arcs, rev lights, steering wheel, DRS/OT).
- Mark any other lap as **VS** — from any session, any day: ghost dot,
  overlaid speed / throttle / brake / steering traces (the viewed lap's
  traces keep their channel colors, the reference joins as a grey ghost
  line), and a
  **DELTA** graph vs distance — green where the viewed lap gains time on
  the reference, red where it loses.
- Next to each chart: the **values at the playhead**, both laps side by
  side — the mid-corner speed difference as a number, not just a gap
  between curves.
- **Corner badges** on the map mark every corner gaining or losing
  0.1 s or more vs the reference. Time is attributed braking-point to
  braking-point, so a slow exit is charged to the corner that caused it and
  the badges account for the whole gap.
- **Scroll on the map — or on any chart — to zoom into a corner** (drag
  the map to pan, double-click or RESET to fit): map and charts stay in
  sync on that stretch of track — braking points in full detail.
- Space = play/pause, ←/→ = seek 1 s (Shift = 5 s), click charts or map to seek.

![GAP mode against a rival ghost's pace reference: dominance-colored racing line and a badge on every corner the rival gains](docs/img/compare-gap.png)
*GAP mode against a leaderboard rival's pace reference (times only, 1.8 s
faster): cyan where the viewed lap is faster, orange where the rival is —
with a badge on every corner that costs 0.1 s or more.*

![Zoomed into turn 3 with the lap tray collapsed: both racing lines and rescaled charts](docs/img/zoom-corner.png)
*Zoomed ×10 into one corner, lap tray collapsed to a rail: two nearly
identical laps, but the braking traces show exactly where the reference
brakes later.*

![Setup panel: car setup and assist settings of both laps side by side](docs/img/setup-panel.png)
*SETUP — the viewed lap's car setup and assists next to the reference
lap's, so a time difference can be traced to the car, not just the
driving.*

## Sharing laps

**EXPORT** (next to SETUP) saves the viewed lap as a `.trace` file — the
lap and its session info as compressed JSON, tens of kilobytes, small
enough for any chat. A `.trace` file from another player, dropped
anywhere on the TRACE window, is imported: the lap appears under its
original session with a **GUEST** badge, lives in the GHOSTS filter, and
compares like a locally recorded lap — corner badges, delta graph and
all. Re-importing a lap that is already in the database is detected and
skipped.

## Track maps

Laps recorded from the game are drawn from the car's **real world
coordinates**. Zoomed out, the line is lightly snapped around the
circuit outline so every lap sits on the identical map; zoom in past
roughly 1 pixel per metre and it switches to the exact driven
trajectory — chicane cuts and kerb rides appear as driven. For laps
without position
data, the viewer falls back to bundled **real circuit
outlines** for every 2026-calendar track including Madrid
(`f1trace/static/tracks.json`, built from the
[f1-circuits](https://github.com/bacinger/f1-circuits) dataset via
`tools/build_tracks.py`; such maps are labelled "approx.").

## Layout

```
f1trace/packets.py    packet structs (2025 + 2026 formats, header-switched)
f1trace/recorder.py   UDP listener, lap segmentation, ghost pace capture
f1trace/db.py         SQLite schema; per-lap compressed column blobs
f1trace/server.py     JSON API + static viewer
f1trace/static/       single-page viewer (no build step)
tools/fake_game.py  synthetic game for end-to-end testing
```

## Documentation

- [docs/architecture.md](docs/architecture.md) — how the pieces fit:
  threads, recording pipeline, storage, HTTP API, viewer subsystems.
- [docs/design-notes.md](docs/design-notes.md) — decisions and the game
  quirks behind them: which ghost data is real and which the game
  fabricates (why ghosts are times-only pace references), the recorder's
  rule budget, per-corner time attribution, color system.

## License

Copyright (c) 2026 lmc4s. [AGPL-3.0](LICENSE) — free to use, modify and
share; any modified version offered as a network service must publish its
source under the same license. Bundled
[Titillium Web](https://fonts.google.com/specimen/Titillium+Web) fonts are
licensed under the [SIL Open Font License 1.1](f1trace/static/fonts/OFL.txt).
Bundled circuit outlines (`f1trace/static/tracks.json`) are derived from the
[f1-circuits](https://github.com/bacinger/f1-circuits) dataset, © 2019–2025
Tomislav Bacinger, [MIT License](f1trace/static/tracks-LICENSE.txt).

## Disclaimer

TRACE is an unofficial fan project. It is not affiliated with, endorsed
by, or sponsored by the Formula 1 companies, the FIA, Electronic Arts
Inc., or Codemasters. F1, FORMULA 1, FORMULA ONE, FIA FORMULA ONE WORLD
CHAMPIONSHIP, GRAND PRIX and related marks are trademarks of Formula One
Licensing B.V.; EA, Codemasters and the F1 25 game title belong to
Electronic Arts Inc. and its subsidiaries. These names appear here only
to identify the game the tool reads telemetry from. TRACE does not
modify the game or its files — it listens to the telemetry stream the
game itself broadcasts over its documented UDP interface.
