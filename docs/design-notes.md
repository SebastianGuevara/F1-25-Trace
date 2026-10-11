# Design notes — decisions, quirks, and why

Companion to [architecture.md](architecture.md): that file says what the
pieces are; this one records the decisions inside them and the game
behaviour that forced each one. Everything here was verified against the
real game (F1 25: 2026 Season Pack, Melbourne Time Trial, 2026-07-11)
unless marked otherwise.

## Guiding constraints

- **Stdlib only, no build step.** The recorder must run on whatever
  Python the machine next to the console has; the viewer must be
  editable without a toolchain. This rules out numpy/pandas (column
  math is done in plain lists/typed arrays) and any JS framework.
- **The receiver stays simple.** Game-quirk rules are a liability: each
  one can misfire on data it wasn't written for. Every rule must map to
  a specific observed game behaviour, be scoped as narrowly as possible,
  and fail benignly. The [rule budget](#rule-budget) below is the
  enforcement mechanism.
- **Store data we believe.** Filtering happens at record time; the DB is
  not a raw packet log. Pushing junk to the viewer would just move the
  same complexity downstream to every consumer.

## Layering principle

**`packets.py` is a pure translator.** It unpacks bytes into dicts/tuples
and knows nothing about roles, quirks, or what the data is for. All
quirk handling lives in `recorder.py`, at the point where lap samples
are assembled. A parser with opinions is much harder to trust when a
struct layout changes.

Defensive parsing, where layouts are risky, follows the same spirit —
degrade instead of guessing: Participants derives record size from
packet length; Session-packet assists are rejected if any value is out
of documented range; an unknown `packetFormat` warns once and continues.

One field is format-dependent in a way length-derivation cannot catch:
Participants `teamId` is a uint8 at record offset +3 in the 2025 layout
but a uint16 at +5 in 2026 (driverId and networkId widened). Found the
hard way on 2026-07-13, when the in-game UDP Format setting had
reverted to base F1 25 mid-evening: the misread field labelled a
Mercedes session as Red Bull. The parser now switches the offset on the
per-packet format field.

## Ghost data: what is real, what the game fabricates

The Time Trial ghosts looked like the tool's most interesting data
source: the LapData packet trailer names the PB-ghost and rival car
indices, and a loaded leaderboard rival broadcasts on a normal car
slot — promising the complete lap of any faster driver, no exports
needed. Versions 0.1.0–0.1.3 recorded that full stream.

The promise did not survive contact with the data. What live sessions
showed, channel by channel:

| Source | For ghosts | Notes |
| --- | --- | --- |
| Motion position | genuine | in every observed session |
| Motion world velocity | **junk in some sessions** | mirrored the CarTelemetry placeholder throughout a Spa session, genuine in a Melbourne one — same install |
| LapData `lapDistance`, `currentLapTime` | genuine | updates at half the packet rate (freeze/double-step stutter) |
| LapData sector fields | **junk** | e.g. sector 1 = 1 ms |
| CarTelemetry (speed, inputs, gear, RPM) | **interleaved junk** | genuine frames alternating with a constant flat-out placeholder (~486 km/h, gear 8, 100 % throttle; 1 923 of 4 579 samples in one Melbourne lap) — and in the Spa session even the "genuine-looking" frames matched nothing physical |
| TimeTrial packet (id 14) | genuine | lap + sector times, assist flags |
| Participants | absent | ghost slots are left out, so ghost laps had no constructor |

Two generations of counter-measures tried to save the feature. The
first vetted CarTelemetry against Motion velocity — and failed, because
in some sessions both carry the same junk, so the check always passed.
The second derived ghost speed from the slope of `lapDistance` over the
lap clock (a quadratic least-squares fit wide enough to iron out the
update stutter) and held the last "genuine" input frame whenever
telemetry disagreed with the derived speed by more than 30 km/h. The
derived speed was solid; the inputs were not. Hold-last masks
placeholders but cannot recover data the game never sent, and in
sessions where most frames are junk the resulting throttle/brake traces
are effectively fiction.

Full ghost capture was therefore removed: **the only ghost channels
trustworthy in every session were position, the lap clock/distance and
the TimeTrial packet's times — and a reference lap with fabricated
inputs misleads exactly where the tool is supposed to be
authoritative.** The official spec agrees by omission: the words
"ghost" and "shadow car" appear nowhere in EA's UDP documentation, and
the only Time-Trial-specific data it defines is the TimeTrial packet —
times, team and assist flags. Everything else TRACE used to read from
the ghost's car slot was undocumented behaviour that happened to look
plausible in some sessions.

### Pace references (0.1.4): keep exactly the trustworthy subset

Which corner the ghost gains in doesn't need pedals — it needs
time-at-distance, and that is precisely the pair of channels genuine in
every observed session. So the same release (0.1.4) ships ghosts as a
**fundamentally different object**: a *pace reference*, not a lap.

- **Stored**: the `(currentLapTime, lapDistance)` series, and the
  TimeTrial packet's lap/sector times, team and assist flags. A pace
  reference's sample blob has only `t` and `d` columns; `top_speed`
  and setup stay NULL.
- **Not stored**: speed, throttle, brake, steering, gear, positions —
  the channels the game fabricates. Absence is the honest statement.
- **In the viewer** pace references are compare-only (clicking one
  sets it as the reference): the DELTA graph, per-corner badges,
  GAP-mode dominance and the sector table all derive from
  time-at-distance and are fully real; the map dot rides the circuit
  outline by lap distance; the telemetry charts simply show no
  reference line.
- **Old ghost laps are converted, not kept**: on first start after the
  upgrade a one-shot migration (`db._strip_ghost_channels`) strips
  every stored `pb_ghost`/`rival` lap down to `t`/`d`, so no
  fabricated brake trace from any era renders again. Times were
  always real; that is what remains.
- Complete laps to study inputs against still exist: the player's
  own, and `.trace` files exported by other TRACE users from their
  real laps (role `guest`).
- `tools/fake_game.py` keeps simulating the shadow car junk included,
  as a regression test that nothing beyond `t`/`d` and TimeTrial data
  leaks into the database.

### Rule budget

The recorder deliberately carries very few game-quirk rules, and each
one must map to a *specific, observed* game behaviour — that is the
guard against heuristic creep. Current inventory:

| Rule | Observed behaviour it answers |
| --- | --- |
| player flashback/rewind drops samples past the rewound position | flashbacks rewind the lap mid-flight; the lap must stay one lap (the game's own `invalid` flag is stored as-is) |
| ghost loop detection: clock/distance rewind ⇒ finalize | ghosts loop at the line without ever incrementing `lap_num`; ghosts never flashback |
| drop ghost samples past the final lap time | a ghost faster than the player parks at the line while the shared lap clock keeps counting — filler tail |
| drop ghost loops that end short of the line | a player restart rewinds the ghost mid-lap; a truncated loop would also poison dedupe |
| `(role, lap_time_ms)` dedupe | the same ghost lap replays every player lap |
| ghost sectors/team/assists only from a TimeTrial dataset matching car index and lap time | ghost LapData sector fields are junk; the TimeTrial packet is the official source |

The 0.1.x placeholder-detection and derived-speed rules are gone for
good: pace references never read the channels those rules defended.

If a rule ever ends up in this file without a concrete behaviour in
the right column, that is the signal the receiver is getting too
clever — remove or re-verify it.

## Storage choices

- **Column-oriented sample blobs** (`{"t": [...], "d": [...]}`, zlib):
  columns of similar numbers compress far better than row tuples, the
  viewer consumes columns anyway, and one decompress serves a whole lap.
  Values are pre-rounded at record time (distance 0.1 m, position 1 cm,
  pedals as 0–100 ints) — precision beyond that is sensor noise and
  costs compression.
- **Additive migrations only.** New sample columns just appear in new
  laps; the viewer treats a missing column as "not recorded" (e.g.
  assist badges on old laps). No blob rewrites, no schema versions.
- **WAL + single writer.** The recorder thread is the only writer; every
  HTTP thread opens its own read connection. No locks in application
  code.

## Viewer design decisions

### The map is the outline, not the telemetry

The track map is always drawn from the bundled real circuit outline
(`tracks.json`), never from lap coordinates. Reasons: every lap of a
track gets the identical shape, view and turn numbers (comparison is
the whole point of the tool); imported laps without coordinates still
get a real map; and the map doesn't wobble with the driven line.

To keep the *real* driven line visible despite that: a one-time per-track
registration finds driving direction + start-line offset by
cross-correlating curvature profiles (threshold 0.45, below it the
telemetry "doesn't look like this track" and calibration is refused),
then an affine game→outline fit. A lap's lateral offset from the
centerline is **high-pass filtered** (±110 m moving average removed,
clamped to ±5.5 m): the slow component is outline-vs-game geometry
mismatch, the fast component is actual line choice — only the latter is
re-applied around the outline. A sloppy affine fit (RMS > 45 m) is
rejected entirely: better no line offsets than wrong ones.

That synth line has a built-in geometric limit: a point is always
"centerline position at this lapDistance, swung sideways", so it can
only ever follow the corner's arc — a chicane cut straight across, or a
deep kerb ride past the ±5.5 m clamp, gets redrawn as hugging the
inside of the arc. So the map keeps **two lines** and switches by zoom:

- zoomed out (< 1 px/m): the synth line — stable, clean, comparable;
- zoomed in (≥ 1 px/m): the **true trajectory** — the registered raw
  coordinates with only the slow ±110 m 2D drift subtracted, nothing
  clamped. Same registration residual correction, real geometry: cuts,
  kerb rides and off-tracks appear exactly as driven.

The threshold is where the difference starts to matter (~1 px per
metre); dots, the racing line and click-to-seek all switch together so
the map never disagrees with itself.

### Corner numbering

Corners are detected from outline curvature. Tight corners are
unmistakable at a strict threshold; officially numbered fast sweeps (Eau
Rouge) are not. When the official turn count is known (`TURN_COUNT`),
detection relaxes the curvature threshold stepwise until the count is
filled — sharp corners can't drown, and named sweeps still get in.

### Per-corner time attribution

The corner badges on the track map answer "which corner is costing me"
(`cornerDeltas` + badge drawing in `static/app.js`):

- The lap is **segmented at each corner's braking point** (corner start
  − 80 m): corner *i* owns everything from its braking zone to the next
  corner's braking zone. A slow exit is charged to the corner that
  caused it — its cost materialises on the straight that follows — and
  the per-corner deltas **sum to the full-lap delta**, so significant
  time cannot hide between corners. (An earlier version measured only
  brake−80 m → exit+40 m per corner; badges then summed to a fraction of
  the real gap and losses on straights were invisible.)
- **Badges show every corner with |Δ| ≥ 0.10 s**, gains and losses
  alike, capped at the 8 largest purely for readability. No
  top-N-losses rule: a fixed significance threshold means "no badge"
  always reads as "this corner is fine", which a top-3 rule cannot
  promise.

### Color

- **Speed ramp is a fixed absolute scale** (60–340 km/h, viridis-like,
  lightness-monotonic): the same speed is the same color on every lap of
  every track, so laps stay comparable at a glance. Gamma 2.0 spends
  most of the ramp near the top, where most of a lap lives. The ramp
  deliberately avoids the red/blue/yellow reserved for S1/S2/S3 and the
  orange reserved for the reference lap.
- **Comparing adds a line instead of recoloring.** The viewed lap's
  chart traces keep their channel colors (speed cyan, throttle green,
  brake red, steer violet); the reference lap joins each panel as one
  neutral grey-white line, thinner and dimmer. Identity is carried by
  the line treatment rather than by hue, so no channel color ever has
  to double as "whose lap is this", and the pairing survives
  color-blindness. The ghost line is deliberately **solid, not dashed** —
  the local differences against the reference are exactly what the
  comparison exists to show, and dashes punch holes in them. On the
  map, the TIMING card and the dominance bar the reference stays orange.
- **Road surface vs background**: the road (`#1b202a`, edge `#3a4150`)
  sits one step above the near-black carbon-weave background — visible
  as a surface, but muted enough that the saturated racing line and the
  dark-violet "slow" end of the ramp still read on top of it.

## Operational notes

- The recorder is typically run as a detached process
  (`nohup python3 -u -m f1trace >> data/f1trace.log 2>&1 &`). Python loads
  modules once: **after editing recorder/packet code, restart the
  process**, or it keeps recording with stale logic. (This cost one
  evening of ghost laps recorded with placeholder speeds after the fix
  already existed on disk.)
- Quick staleness check: compare `ps` start time of `python … -m f1trace`
  against source mtimes.
- Safe-restart check: `GET /api/status` — `pps` (packets per second) is
  0 when the game is idle, so restarting loses nothing.
- Which code wrote a lap: samples written by current code include an
  `aero` column; older laps don't.
- Self-diagnostics live in `/api/status`: observed per-packet sizes
  (catches layout mismatches) and live lap-buffer stats.
