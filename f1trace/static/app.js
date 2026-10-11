/* TRACE viewer: lap browser, track-map replay, halo HUD, comparison. */
"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  tracks: [],
  trackId: null,
  laps: [],          // all laps of the selected track, across sessions
  lapA: null,        // viewed lap (loaded, with samples + lookups)
  lapB: null,        // reference lap
  t: 0,              // playhead ms (lap A clock)
  playing: false,
  speed: 1,
  lastFrame: 0,
  mode: "speed",     // racing-line color mode: speed | gap
  mapZoom: null,     // {k, wx, wz}: zoom factor + world-space view centre
  viewD: null,       // [d0, d1] focused distance range (map zoom -> charts)
  sort: "recent",    // lap list ordering: recent | fastest
  roleFilter: "all", // all | player | ghost
  assistFilter: "all", // all | on | off
  hideInvalid: false,
  setupOpen: false,  // setup/assists panel visible
  timingOpen: true,  // TIMING card expanded (click its tag to fold)
  folded: new Set(), // collapsed session ids in the lap list
  hudOpen: true,     // TELEMETRY card expanded (click its tag to fold)
  hudSteer: true,    // steering section of the telemetry card visible
  hudPos: null,      // dragged card position {x, y} as stage fractions
  readonly: false,   // static hosting (GitHub Pages demo): nothing to delete
};

/* ---------------------------------------------------------------- color */

const SEC_COLORS = ["#f87171", "#60a5fa", "#fbbf24"];      // S1 / S2 / S3
// slow -> fast: viridis body (violet -> blue -> teal -> green) but topped
// with bright aqua instead of yellow; lightness-monotonic, clear of S1/S2/S3
const SPEED_RAMP = ["#440154", "#3f4795", "#2a788e", "#27ad82", "#52e6bb", "#b3fff0"];
// fixed scale: the same speed is the same color on every lap of every
// track; below SPEED_MIN everything reads as "slow" in the same violet
const SPEED_MIN = 60, SPEED_MAX = 340;
// most of a lap sits near top speed; gamma spends more of the ramp up there
const SPEED_GAMMA = 2.0;
// track dominance: your color <- neutral -> reference's color
const GAP_RAMP = ["#22d3ee", "#39414f", "#fb923c"];

function hex2rgb(h) {
  return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16),
          parseInt(h.slice(5, 7), 16)];
}
function ramp(stops, f) {
  f = Math.min(1, Math.max(0, f));
  const seg = f * (stops.length - 1);
  const i = Math.min(stops.length - 2, Math.floor(seg)), g = seg - i;
  const a = hex2rgb(stops[i]), b = hex2rgb(stops[i + 1]);
  return "rgb(" + Math.round(a[0] + (b[0] - a[0]) * g) + "," +
    Math.round(a[1] + (b[1] - a[1]) * g) + "," +
    Math.round(a[2] + (b[2] - a[2]) * g) + ")";
}

/* ---------------------------------------------------------------- utils */

function fmtTime(ms, forceMin) {
  if (ms == null || ms <= 0) return "—";
  const m = Math.floor(ms / 60000), s = (ms % 60000) / 1000;
  if (!m && !forceMin) return s.toFixed(3);
  return m + ":" + s.toFixed(3).padStart(6, "0");
}
function fmtDelta(sec) {
  return (sec >= 0 ? "+" : "−") + Math.abs(sec).toFixed(3);
}

/* Binary search: index of last element <= v in sorted arr. */
function lowerIdx(arr, v) {
  let lo = 0, hi = arr.length - 1;
  if (v <= arr[0]) return 0;
  if (v >= arr[hi]) return hi;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (arr[mid] <= v) lo = mid; else hi = mid;
  }
  return lo;
}
/* Linear interpolation of column col at key value v over key array keys. */
function interp(keys, col, v) {
  const i = lowerIdx(keys, v);
  if (i >= keys.length - 1) return col[keys.length - 1];
  const k0 = keys[i], k1 = keys[i + 1];
  if (k1 === k0) return col[i];
  const f = (v - k0) / (k1 - k0);
  return col[i] + (col[i + 1] - col[i]) * f;
}

/* ---------------------------------------------------------------- api */

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(path + " -> " + r.status);
  return r.json();
}

/* Real circuit outlines (bundled from the f1-circuits dataset). */
let TRACKS = null;
const tracksReady = fetch("tracks.json").then((r) => r.json())
  .then((t) => { TRACKS = t; }).catch(() => {});

/* ------------------------------------------------ track geometry
   The map is always drawn from the real circuit outline (tracks.json),
   never from lap telemetry, so the shape, the view and the turn numbers
   are identical for every lap of a track. A lap's samples are placed on
   the outline by lap distance. Telemetry is used only once per track, to
   register the outline against the game: driving direction and where the
   start line sits on the dataset's loop (curvature cross-correlation). */

const GEOM_STEP = 5;      // metres between resampled outline points
const geomCache = {};     // track_id -> geom

// official corner counts per game trackId; corner detection tunes its
// curvature threshold so the numbering matches the published turn count
const TURN_COUNT = {
  0: 14, 2: 16, 3: 15, 4: 14, 5: 19, 6: 14, 7: 18, 9: 14, 10: 19, 11: 11,
  12: 19, 13: 18, 14: 16, 15: 20, 16: 15, 17: 10, 19: 17, 20: 20, 26: 14,
  27: 19, 29: 27, 30: 19, 31: 17, 32: 16, 42: 22,
  39: 18, 40: 10, 41: 14,   // reverse layouts
};

function trackGeom(tid) {
  if (geomCache[tid]) return geomCache[tid];
  const tr = TRACKS && TRACKS[tid];
  if (!tr) return null;
  const geom = resampleLoop(tr.pts, GEOM_STEP);
  // fixed per-track rotation: lay the long axis horizontally so the
  // track fills the wide stage instead of a sliver of it (north-up is
  // sacrificed; every lap of a track still shares the same frame)
  rotateGeom(geom, bestRotation(geom.xs, geom.zs));
  geom.version = 0;
  geom.xform = null;   // affine game->outline map (set by calibration)
  try {
    // "cal3": fitted against outlines aligned to the official start/finish
    // (tools/fetch_corners.py); older keys indexed differently-ordered
    // points and are left to expire.
    const cal = JSON.parse(localStorage.getItem("f1trace.cal3." + tid));
    if (cal && "m" in cal) {
      applyCal(geom, cal);
      geom.xform = cal.m;
      geom.calibrated = true;
    }
  } catch (e) { /* no saved calibration */ }
  geomNormals(geom);
  geom.corners = outlineCorners(geom, TURN_COUNT[tid], tr.corners);
  geomCache[tid] = geom;
  return geom;
}

/* Angle (±90°) that best fills a typical wide stage, judged by the
   fitted scale min(aspect/width, 1/height); ties prefer least rotation. */
function bestRotation(xs, zs) {
  const ASPECT = 2.2;
  let best = 0, bestScore = -1;
  for (let d = 0; d <= 90; d += 2) {
    for (const deg of d === 0 ? [0] : [d, -d]) {
      const a = (deg * Math.PI) / 180, c = Math.cos(a), s = Math.sin(a);
      let minX = 1e9, maxX = -1e9, minZ = 1e9, maxZ = -1e9;
      for (let i = 0; i < xs.length; i += 3) {
        const x = xs[i] * c - zs[i] * s;
        const z = xs[i] * s + zs[i] * c;
        if (x < minX) minX = x; if (x > maxX) maxX = x;
        if (z < minZ) minZ = z; if (z > maxZ) maxZ = z;
      }
      const score = Math.min(ASPECT / (maxX - minX), 1 / (maxZ - minZ));
      if (score > bestScore) { bestScore = score; best = a; }
    }
  }
  return best;
}

function rotateGeom(geom, a) {
  const c = Math.cos(a), s = Math.sin(a), { xs, zs, n } = geom;
  for (let i = 0; i < n; i++) {
    const x = xs[i], z = zs[i];
    xs[i] = x * c - z * s;
    zs[i] = x * s + z * c;
  }
}

/* Unit normals of the outline at every grid point. */
function geomNormals(geom) {
  const { xs, zs, n } = geom;
  const nx = new Float64Array(n), nz = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const a = (i + n - 1) % n, b = (i + 1) % n;
    const tx = xs[b] - xs[a], tz = zs[b] - zs[a];
    const l = Math.hypot(tx, tz) || 1;
    nx[i] = -tz / l; nz[i] = tx / l;
  }
  geom.nx = nx; geom.nz = nz;
}

/* Outline points -> uniform grid, one point every ~GEOM_STEP metres. */
function resampleLoop(pts, step) {
  const cum = [0];
  for (let i = 1; i < pts.length; i++)
    cum.push(cum[i - 1] + Math.hypot(pts[i][0] - pts[i - 1][0],
                                     pts[i][1] - pts[i - 1][1]));
  const total = cum[cum.length - 1];
  const n = Math.max(16, Math.round(total / step));
  const st = total / n;
  const xs = new Float64Array(n), zs = new Float64Array(n);
  let j = 0;
  for (let i = 0; i < n; i++) {
    const s = i * st;
    while (j < cum.length - 2 && cum[j + 1] < s) j++;
    const f = (s - cum[j]) / (cum[j + 1] - cum[j] || 1);
    xs[i] = pts[j][0] + (pts[j + 1][0] - pts[j][0]) * f;
    zs[i] = pts[j][1] + (pts[j + 1][1] - pts[j][1]) * f;
  }
  return { xs, zs, n, step: st, total };
}

/* Signed curvature (rad/m) at every grid point, lightly smoothed. */
function loopKappa(xs, zs, step) {
  const n = xs.length, head = new Float64Array(n), kap = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    const j = (i + 1) % n;
    head[i] = Math.atan2(zs[j] - zs[i], xs[j] - xs[i]);
  }
  for (let i = 0; i < n; i++) {
    let dh = head[i] - head[(i + n - 1) % n];
    while (dh > Math.PI) dh -= 2 * Math.PI;
    while (dh < -Math.PI) dh += 2 * Math.PI;
    kap[i] = dh / step;
  }
  const sm = new Float64Array(n);
  for (let i = 0; i < n; i++) {
    let a = 0;
    for (let w = -2; w <= 2; w++) a += kap[(i + w + n) % n];
    sm[i] = a / 5;
  }
  return sm;
}

/* Corner segments at curvature threshold th: contiguous same-sign runs
   (a chicane = two corners), short gaps bridged, tiny kinks dropped. */
function segmentCorners(kap, step, th) {
  const n = kap.length;
  let anchor = 0;               // start the circular walk on a straight
  for (let i = 0; i < n; i++) if (Math.abs(kap[i]) <= th) { anchor = i; break; }
  const raw = [];
  let q0 = -1, sign = 0;
  for (let q = 1; q <= n; q++) {
    const v = kap[(anchor + q) % n];
    const sg = Math.abs(v) > th ? (v > 0 ? 1 : -1) : 0;
    if (sg === sign) continue;
    if (sign !== 0) raw.push({ q0, q1: q - 1, sign });
    q0 = q; sign = sg;
  }
  if (sign !== 0) raw.push({ q0, q1: n, sign });
  const merged = [];            // bridge short straights inside one corner
  for (const s of raw) {
    const p = merged[merged.length - 1];
    if (p && p.sign === s.sign && (s.q0 - p.q1) * step < 25) p.q1 = s.q1;
    else merged.push({ q0: s.q0, q1: s.q1, sign: s.sign });
  }
  const out = [];
  const kq = (q) => kap[(anchor + q) % n];
  const emit = (a, b) => {
    let ang = 0, apexQ = a;
    for (let q = a; q <= b; q++) {
      ang += kq(q) * step;
      if (Math.abs(kq(q)) > Math.abs(kq(apexQ))) apexQ = q;
    }
    if ((b - a + 1) * step >= 15 && Math.abs(ang) >= 0.12)
      out.push({ i0: (anchor + a) % n, i1: (anchor + b) % n,
                 apex: (anchor + apexQ) % n, ang });
  };
  // a long same-sign sweep can be several numbered turns (Shanghai T1-T2);
  // split recursively at a clear interior curvature dip well away from
  // both apexes, with a real corner's worth of angle on each side
  const split = (a, b) => {
    let dip = -1;
    for (let q = a + 8; q <= b - 8; q++)
      if (dip < 0 || Math.abs(kq(q)) < Math.abs(kq(dip))) dip = q;
    if (dip >= 0) {
      let pl = 0, pr = 0, al = 0, ar = 0;
      for (let q = a; q <= dip; q++) {
        pl = Math.max(pl, Math.abs(kq(q))); al += kq(q) * step;
      }
      for (let q = dip + 1; q <= b; q++) {
        pr = Math.max(pr, Math.abs(kq(q))); ar += kq(q) * step;
      }
      if (Math.abs(kq(dip)) < 0.5 * Math.min(pl, pr) &&
          Math.abs(al) >= 0.2 && Math.abs(ar) >= 0.2) {
        split(a, dip); split(dip + 1, b);
        return;
      }
    }
    emit(a, b);
  };
  for (const s of merged) split(s.q0, s.q1);
  return out;
}

/* Corners of the outline, numbered in driving order from the start line.
   With an official corner chart (lap-distance fractions of each published
   corner, tools/fetch_corners.py) the numbering is authoritative: every
   anchor snaps to the nearest curvature apex of the drawn outline, held
   short of its neighbours. Without one, detection has to guess which
   bends the official numbering counts: unmistakable corners (tight
   radius) are found first; when the official turn count is known,
   progressively weaker thresholds fill the remaining numbering slots —
   that's how fast officially-numbered sweeps like Eau Rouge get in
   without sharp corners drowning in noise. */
function outlineCorners(geom, target, chart) {
  const st = geom.step, n = geom.n, L = geom.total;
  const kap = loopKappa(geom.xs, geom.zs, st);
  const kq = (q) => kap[((q % n) + n) % n];
  if (chart && chart.length) {
    const m = chart.length;
    const anchors = chart.map((f) => (f * L) / st);   // grid units, sorted
    let pa = anchors[m - 1] - n;      // previous corner's snapped apex
    return anchors.map((a, i) => {
      // snap to the local curvature peak nearest the anchor — nearest, not
      // strongest, so a corner never grabs its neighbour's tighter apex —
      // among peaks with at least 0.3× the window's top curvature (tracing
      // noise makes weaker ripples). ±100 m window (chart vs outline
      // tracing drifts that far), held between the previous snapped apex
      // and the midpoint to the next anchor: numbering order is preserved
      // by construction
      const next = anchors[(i + 1) % m] + (i === m - 1 ? n : 0);
      const lo = Math.max(a - 100 / st, pa + 2);
      const hi = Math.min(a + 100 / st, (a + next) / 2);
      let peak = 0;
      for (let q = Math.ceil(lo); q <= Math.floor(hi); q++)
        peak = Math.max(peak, Math.abs(kq(q)));
      let apex = Math.round(a);
      let bestD = Infinity;
      for (let q = Math.ceil(lo); q <= Math.floor(hi); q++) {
        const v = Math.abs(kq(q));
        if (v < Math.max(peak * 0.3, 1 / 800) ||
            v < Math.abs(kq(q - 1)) || v < Math.abs(kq(q + 1)))
          continue;
        if (Math.abs(q - a) < bestD) { bestD = Math.abs(q - a); apex = q; }
      }
      if (bestD === Infinity)   // no interior peak (apex just past the
        for (let q = Math.ceil(lo); q <= Math.floor(hi); q++)  // window)
          if (Math.abs(kq(q)) > Math.abs(kq(apex))) apex = q;
      pa = apex;
      // extent: walk out while the bend keeps the apex's sign and heft
      const sg = kq(apex) >= 0 ? 1 : -1;
      const th = Math.max(Math.abs(kq(apex)) * 0.3, 1 / 800);
      let q0 = apex, q1 = apex;
      while (q0 - 1 > lo && sg * kq(q0 - 1) > th) q0--;
      while (q1 + 1 < hi && sg * kq(q1 + 1) > th) q1++;
      return { n: i + 1, s0: q0 * st, s1: q1 * st, apexS: apex * st,
               ang: kq(apex) };
    });
  }
  const arcs = (s) => ({
    s0: s.i0 * st,
    s1: (s.i1 >= s.i0 ? s.i1 : s.i1 + n) * st,
    apexS: (s.apex >= s.i0 ? s.apex : s.apex + n) * st,
    ang: s.ang,
  });
  const finalize = (cs) => {
    cs.sort((a, b) => (a.apexS % L) - (b.apexS % L));
    cs.forEach((c, i) => { c.n = i + 1; });
    return cs;
  };
  if (!target) return finalize(segmentCorners(kap, st, 1 / 220).map(arcs));
  const inSeg = (p, s) => {   // does segment s cover arc position p?
    let q = p % L;
    if (q < s.s0) q += L;
    return q <= s.s1;
  };
  const chosen = [];
  for (const th of [1 / 60, 1 / 110, 1 / 180, 1 / 300, 1 / 500, 1 / 800]) {
    if (chosen.length >= target) break;
    const cands = segmentCorners(kap, st, th).map(arcs)
      .sort((a, b) => Math.abs(b.ang) - Math.abs(a.ang));
    for (const c of cands) {
      if (chosen.length >= target) break;
      const dup = chosen.some((x) => {
        let d = Math.abs((x.apexS % L) - (c.apexS % L));
        d = Math.min(d, L - d);
        return inSeg(x.apexS, c) || inSeg(c.apexS, x) ||
               (d < 50 && x.ang * c.ang > 0);
      });
      if (!dup) chosen.push(c);
    }
  }
  return finalize(chosen);
}

/* One-time registration of the outline against game telemetry: find the
   driving direction and start-line offset whose curvature profile best
   matches the lap's. Result is saved, so the map never shifts again. */
function calibrateGeom(geom, lap, tid) {
  const s = lap.samples, n = geom.n, step = geom.step;
  const k = geom.total / (lap.track_length || s.d[s.d.length - 1]);
  const maxD = s.d[s.d.length - 1];
  const tk = new Float64Array(n);
  let prev = null, tn2 = 0;
  for (let i = 0; i < n; i++) {
    const d1 = ((i + 1) * step) / k;
    if (d1 > maxD) break;
    const d0 = (i * step) / k;
    const h = Math.atan2(interp(s.d, s.z, d1) - interp(s.d, s.z, d0),
                         interp(s.d, s.x, d1) - interp(s.d, s.x, d0));
    if (prev !== null) {
      let dh = h - prev;
      while (dh > Math.PI) dh -= 2 * Math.PI;
      while (dh < -Math.PI) dh += 2 * Math.PI;
      tk[i] = dh / step; tn2 += tk[i] * tk[i];
    }
    prev = h;
  }
  let best = { score: 0, rev: 0, off: 0 };
  for (const rev of [0, 1]) {
    let xs = geom.xs, zs = geom.zs;
    if (rev) { xs = xs.slice().reverse(); zs = zs.slice().reverse(); }
    const ok = loopKappa(xs, zs, step);
    let on2 = 0;
    for (let i = 0; i < n; i++) on2 += ok[i] * ok[i];
    const okD = new Float64Array(2 * n);
    okD.set(ok); okD.set(ok, n);
    const norm = Math.sqrt(on2 * tn2) || 1;
    for (let off = 0; off < n; off++) {
      let c = 0;
      for (let i = 0; i < n; i++) c += okD[off + i] * tk[i];
      c = Math.abs(c) / norm;
      if (c > best.score) best = { score: c, rev, off };
    }
  }
  geom.calibrated = true;        // don't retry on every lap load
  if (best.score < 0.45) return; // telemetry doesn't look like this track
  applyCal(geom, best);
  geomNormals(geom);
  geom.version++;
  geom.corners = outlineCorners(geom, TURN_COUNT[tid],
                                TRACKS && TRACKS[tid] && TRACKS[tid].corners);
  // affine registration game -> outline: with it, the lap's genuine
  // lateral line can be drawn around the fixed centerline
  const pairs = [];
  for (let i = 0; i < n; i += 4) {
    const d = (i * step) / k;
    if (d > maxD) break;
    pairs.push([interp(s.d, s.x, d), interp(s.d, s.z, d),
                geom.xs[i], geom.zs[i]]);
  }
  geom.xform = affineFit(pairs);
  try {
    localStorage.setItem("f1trace.cal3." + tid, JSON.stringify(
      { rev: best.rev, off: best.off, m: geom.xform }));
  } catch (e) { /* private mode etc. */ }
}

/* Least-squares affine map [gx, gz] -> [a b c; d e f] applied to (x, z),
   or null when the fit is too loose to trust. */
function affineFit(pairs) {
  const N = pairs.length;
  if (N < 40) return null;
  let mx = 0, mz = 0, mu = 0, mv = 0;
  for (const p of pairs) { mx += p[0]; mz += p[1]; mu += p[2]; mv += p[3]; }
  mx /= N; mz /= N; mu /= N; mv /= N;
  let sxx = 0, sxz = 0, szz = 0, sxu = 0, szu = 0, sxv = 0, szv = 0;
  for (const p of pairs) {
    const x = p[0] - mx, z = p[1] - mz, u = p[2] - mu, v = p[3] - mv;
    sxx += x * x; sxz += x * z; szz += z * z;
    sxu += x * u; szu += z * u; sxv += x * v; szv += z * v;
  }
  const det = sxx * szz - sxz * sxz;
  if (Math.abs(det) < 1e-6) return null;
  const a = (sxu * szz - szu * sxz) / det, b = (szu * sxx - sxu * sxz) / det;
  const d = (sxv * szz - szv * sxz) / det, e = (szv * sxx - sxv * sxz) / det;
  const c = mu - a * mx - b * mz, f = mv - d * mx - e * mz;
  let err = 0;
  for (const p of pairs) {
    const ru = a * p[0] + b * p[1] + c - p[2];
    const rv = d * p[0] + e * p[1] + f - p[3];
    err += ru * ru + rv * rv;
  }
  // reject a sloppy registration: better no line offsets than wrong ones
  if (Math.sqrt(err / N) > 45) return null;
  return [a, b, c, d, e, f];
}

function applyCal(geom, cal) {
  let xs = geom.xs, zs = geom.zs;
  if (cal.rev) { xs = xs.slice().reverse(); zs = zs.slice().reverse(); }
  const n = geom.n, o = ((cal.off % n) + n) % n;
  const rot = (a) => {
    const out = new Float64Array(n);
    for (let i = 0; i < n; i++) out[i] = a[(i + o) % n];
    return out;
  };
  geom.xs = rot(xs); geom.zs = rot(zs);
}

/* Place a lap's samples on the outline by lap distance. When the track is
   registered (affine game->outline fit), the lap's genuine lateral line is
   kept: the high-frequency part of its offset from the centerline is
   re-applied around the outline, so a zoomed-in map shows the real line. */
function synthCoords(lap, geom) {
  const s = lap.samples, n = geom.n;
  const k = geom.total / (lap.track_length || geom.total);
  if (!lap.rawX) { lap.rawX = s.x; lap.rawZ = s.z; }  // pre-synth coords
  lap.trueX = lap.trueZ = null;   // set by lineOffsets when computable
  const off = lineOffsets(lap, geom, k);
  const N = s.d.length, xs = new Array(N), zs = new Array(N);
  for (let i = 0; i < N; i++) {
    const a = (s.d[i] * k) / geom.step;
    const i0 = Math.floor(a) % n, f = a - Math.floor(a), i1 = (i0 + 1) % n;
    const o = off ? off[i] : 0;
    xs[i] = geom.xs[i0] + (geom.xs[i1] - geom.xs[i0]) * f +
            o * (geom.nx[i0] + (geom.nx[i1] - geom.nx[i0]) * f);
    zs[i] = geom.zs[i0] + (geom.zs[i1] - geom.zs[i0]) * f +
            o * (geom.nz[i0] + (geom.nz[i1] - geom.nz[i0]) * f);
  }
  s.x = xs; s.z = zs;
  lap.geomVersion = geom.version;
}

/* Signed lateral offset of the driven line vs the outline centerline,
   high-pass filtered along lap distance: the slow component is outline-vs-
   game geometry mismatch, the fast component is actual line choice.
   Side product: lap.trueX/trueZ — the registered raw trajectory with only
   that slow 2D drift subtracted, nothing clamped. Its geometry is the real
   one (corner cuts, kerb rides), unlike the synth line, which can only
   swing laterally around the centerline arc; the map switches to it once
   zoomed in far enough for the difference to matter. */
function lineOffsets(lap, geom, k) {
  if (!geom.xform) return null;
  const s = lap.samples, X = lap.rawX, Z = lap.rawZ, N = s.d.length;
  let minX = 1e9, maxX = -1e9;
  for (let i = 0; i < N; i++) {
    if (X[i] < minX) minX = X[i]; if (X[i] > maxX) maxX = X[i];
  }
  if (maxX - minX < 50) return null;   // no position data (imports)
  const [a, b, c, d, e, f] = geom.xform, n = geom.n;
  const raw = new Float64Array(N);
  const gxs = new Float64Array(N), gzs = new Float64Array(N);
  const dxs = new Float64Array(N), dzs = new Float64Array(N);
  for (let i = 0; i < N; i++) {
    const gx = a * X[i] + b * Z[i] + c;
    const gz = d * X[i] + e * Z[i] + f;
    const t = (s.d[i] * k) / geom.step;
    const i0 = Math.floor(t) % n, ff = t - Math.floor(t), i1 = (i0 + 1) % n;
    const cx = geom.xs[i0] + (geom.xs[i1] - geom.xs[i0]) * ff;
    const cz = geom.zs[i0] + (geom.zs[i1] - geom.zs[i0]) * ff;
    const nx = geom.nx[i0] + (geom.nx[i1] - geom.nx[i0]) * ff;
    const nz = geom.nz[i0] + (geom.nz[i1] - geom.nz[i0]) * ff;
    gxs[i] = gx; gzs[i] = gz;
    dxs[i] = gx - cx; dzs[i] = gz - cz;
    raw[i] = dxs[i] * nx + dzs[i] * nz;
  }
  // high-pass: subtract the moving average over ±110 m, clamp to the road
  const out = new Float64Array(N);
  const tX = new Float64Array(N), tZ = new Float64Array(N);
  let lo = 0, hi = 0, sum = 0, sumX = 0, sumZ = 0;
  for (let i = 0; i < N; i++) {
    while (hi < N && s.d[hi] <= s.d[i] + 110) {
      sum += raw[hi]; sumX += dxs[hi]; sumZ += dzs[hi]; hi++;
    }
    while (s.d[lo] < s.d[i] - 110) {
      sum -= raw[lo]; sumX -= dxs[lo]; sumZ -= dzs[lo]; lo++;
    }
    const m = hi - lo;
    out[i] = Math.max(-5.5, Math.min(5.5, raw[i] - sum / m));
    tX[i] = gxs[i] - sumX / m;
    tZ[i] = gzs[i] - sumZ / m;
  }
  lap.trueX = tX; lap.trueZ = tZ;
  return out;
}

/* Prepare a loaded lap: clean samples, build lookups. */
function prepLap(lap) {
  const s = lap.samples;
  // keep only strictly increasing time AND distance: interp() binary-
  // searches both, so flashback residue or a ghost's wrapped clock at the
  // end of a stored lap would otherwise derail every lookup
  const keep = [];
  let lastD = -1, lastT = -1;
  for (let i = 0; i < s.d.length; i++) {
    if (s.d[i] > lastD && s.t[i] > lastT) {
      keep.push(i); lastD = s.d[i]; lastT = s.t[i];
    }
  }
  if (keep.length !== s.d.length) {
    for (const k of Object.keys(s)) s[k] = keep.map((i) => s[k][i]);
  }
  // pace references (ghosts) carry no coordinates — flat zeros make
  // synthCoords place them on the circuit outline by lap distance, the
  // same fallback imported laps without position data use
  if (!s.x) {
    s.x = new Array(s.d.length).fill(0);
    s.z = new Array(s.d.length).fill(0);
  }
  const geom = trackGeom(lap.track_id);
  if (geom) {
    // a lap with real position data registers the outline, once per track
    let minX = 1e9, maxX = -1e9, minZ = 1e9, maxZ = -1e9;
    for (let i = 0; i < s.x.length; i++) {
      if (s.x[i] < minX) minX = s.x[i]; if (s.x[i] > maxX) maxX = s.x[i];
      if (s.z[i] < minZ) minZ = s.z[i]; if (s.z[i] > maxZ) maxZ = s.z[i];
    }
    if (Math.max(maxX - minX, maxZ - minZ) >= 50 && !geom.calibrated)
      calibrateGeom(geom, lap, lap.track_id);
    synthCoords(lap, geom);   // every lap sits on the same outline
  }
  lap.duration = lap.lap_time_ms || s.t[s.t.length - 1];
  lap.maxD = s.d[s.d.length - 1];
  // sector boundary distances, derived from sector times
  if (lap.s1_ms && lap.s2_ms)
    lap.secD = [interp(s.t, s.d, lap.s1_ms),
                interp(s.t, s.d, lap.s1_ms + lap.s2_ms)];
  return lap;
}

/* ---------------------------------------------------------------- corners */

/* Detect corners from path curvature; number them in driving order. */
function computeCorners(lap) {
  const s = lap.samples, step = 8;
  const grid = [], gx = [], gz = [];
  for (let d = 0; d <= lap.maxD; d += step) {
    grid.push(d);
    gx.push(interp(s.d, s.x, d));
    gz.push(interp(s.d, s.z, d));
  }
  const n = grid.length, kap = new Array(n).fill(0);
  let prevHead = null;
  for (let i = 1; i < n; i++) {
    const head = Math.atan2(gz[i] - gz[i - 1], gx[i] - gx[i - 1]);
    if (prevHead !== null) {
      let dh = head - prevHead;
      while (dh > Math.PI) dh -= 2 * Math.PI;
      while (dh < -Math.PI) dh += 2 * Math.PI;
      kap[i] = Math.abs(dh) / step;
    }
    prevHead = head;
  }
  const sm = kap.map((_, i) => ((kap[i - 1] || 0) + kap[i] + (kap[i + 1] || 0)) / 3);
  const TH = 1 / 220;   // corner = local radius under ~220 m
  const corners = [];
  let start = -1;
  for (let i = 0; i <= n; i++) {
    const inC = i < n && sm[i] > TH;
    if (inC && start < 0) start = i;
    if (!inC && start >= 0) {
      if ((i - start) * step >= 24) {
        let apex = start;
        for (let j = start; j < i; j++) if (sm[j] > sm[apex]) apex = j;
        corners.push({ startD: grid[start], endD: grid[Math.min(i, n - 1)],
                       apexD: grid[apex], x: gx[apex], z: gz[apex] });
      }
      start = -1;
    }
  }
  corners.forEach((c, i) => { c.n = i + 1; });
  return corners;
}

/* Per-corner time gained/lost vs the reference. The lap is segmented at
   each corner's braking point: corner i owns brake_i -> brake_(i+1), so a
   slow exit is charged to the corner that caused it (its cost lives on
   the straight that follows) and the per-corner deltas add up to the
   full-lap delta — nothing significant can hide between corners. */
function cornerDeltas() {
  const A = state.lapA, B = state.lapB;
  const maxD = Math.min(A.maxD, B.maxD);
  const del = (d) => {
    d = Math.min(d, maxD);
    return interp(A.samples.d, A.samples.t, d) - interp(B.samples.d, B.samples.t, d);
  };
  const cs = A.corners;
  for (let i = 0; i < cs.length; i++) {
    const d0 = Math.max(0, cs[i].startD - 80);
    const d1 = i + 1 < cs.length ? Math.max(0, cs[i + 1].startD - 80) : A.maxD;
    cs[i].loss = (del(d1) - del(d0)) / 1000;   // s: + = lost time, - = gained
  }
}

/* ------------------------------------------------- themed dropdowns
   Wraps a native <select> in a styled button + menu. The select stays in
   the DOM (hidden) as the source of truth, so existing code that rebuilds
   its options or reads/sets .value keeps working unchanged. */
function styleSelect(sel) {
  const wrap = document.createElement("div");
  wrap.className = "dd";
  sel.parentNode.insertBefore(wrap, sel);
  wrap.appendChild(sel);
  const btn = document.createElement("button");
  btn.type = "button"; btn.className = "dd-btn";
  const menu = document.createElement("div");
  menu.className = "dd-menu";
  wrap.append(btn, menu);
  const sync = () => {
    const o = sel.options[sel.selectedIndex];
    btn.innerHTML = `<span>${o ? o.text : "—"}</span><b class="dd-arr">▾</b>`;
  };
  const close = () => wrap.classList.remove("open");
  const open = () => {
    menu.innerHTML = "";
    for (const o of sel.options) {
      const it = document.createElement("div");
      it.className = "dd-item" + (o.index === sel.selectedIndex ? " sel" : "");
      it.textContent = o.text;
      it.addEventListener("click", () => {
        sel.value = o.value;
        sel.dispatchEvent(new Event("change"));
        close(); sync();
      });
      menu.appendChild(it);
    }
    wrap.classList.add("open");
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (wrap.classList.contains("open")) close(); else open();
  });
  document.addEventListener("click", close);
  sel.addEventListener("change", sync);
  // options rebuilt / value set programmatically (e.g. loadTracks)
  new MutationObserver(sync).observe(sel, { childList: true });
  sync();
}

/* ---------------------------------------------------------------- header / status */

/* Demo mode: once the lap list is in, open the driven lap vs the bundled
   reference lap so the first thing on screen is a full comparison.
   Prefer a full-telemetry reference (guest) over a times-only pace ref —
   the chart overlay is the richer first impression. */
let demoLoaded = false;
async function autoloadDemo() {
  if (demoLoaded || state.lapA || !state.laps || !state.laps.length) return;
  demoLoaded = true;
  const best = (pick) => state.laps
    .filter((l) => l.valid && l.lap_time_ms && pick(l))
    .sort((a, b) => a.lap_time_ms - b.lap_time_ms)[0];
  const you = best((l) => l.car_role === "player");
  const ref = best((l) => l.car_role === "guest") ||
              best((l) => l.car_role !== "player");
  if (you) await viewLap(you.id);
  if (ref) await toggleRef(ref.id);
}

let lastLapStamp = "";
async function pollStatus() {
  try {
    const st = await api("api/status");
    if (st.version) $("brand").title = "TRACE " + st.version;
    state.readonly = !!st.static;
    if (state.readonly) {   // static hosting: no server to write or export from
      $("list-foot").style.display = "none";
      $("export-btn").style.display = "none";
    }
    const chip = $("status-chip");
    if (st.demo) {
      chip.className = "chip idle"; chip.textContent = "DEMO";
      autoloadDemo();
    } else if (st.pps > 0) {
      chip.className = "chip live"; chip.textContent = "LIVE " + st.pps + " pps";
    } else {
      chip.className = "chip idle";
      chip.textContent = st.listening ? "IDLE" : "OFFLINE";
    }
    let detail = "";
    if (st.session) detail = st.session.track + " · " + st.session.type;
    if (st.live && st.pps > 0)
      detail += " · lap " + st.live.lap_num + " · " + fmtTime(st.live.lap_time_ms, 1);
    $("status-detail").textContent = detail;

    // a ghost on track means its pace is being captured — show which one
    const gc = $("pace-chip");
    if (st.pps > 0 && st.ghosts && (st.ghosts.rival || st.ghosts.pb)) {
      gc.style.display = "";
      gc.textContent = st.ghosts.rival ? "RIVAL PACE ✓" : "PB PACE ✓";
    } else gc.style.display = "none";

    const stamp = JSON.stringify(st.last_lap);
    if (stamp !== lastLapStamp) {
      lastLapStamp = stamp;
      await loadTracks(true);   // new lap stored -> refresh lists
    }
  } catch (e) { /* server briefly away; retry next tick */ }
  setTimeout(pollStatus, 2000);
}

/* ---------------------------------------------------------------- tracks & laps */

async function loadTracks(keepSelection) {
  state.tracks = await api("api/tracks");
  const sel = $("track-select");
  const prev = state.trackId;
  sel.innerHTML = "";
  for (const t of state.tracks) {
    const o = document.createElement("option");
    o.value = t.track_id;
    o.textContent = `${t.track_name} — ${t.n_laps} lap${t.n_laps === 1 ? "" : "s"}` +
      (t.best_ms ? ` · best ${fmtTime(t.best_ms, 1)}` : "");
    sel.appendChild(o);
  }
  if (!state.tracks.length) { $("lap-list").innerHTML =
    '<div class="empty-list">No laps recorded yet.</div>'; return; }
  const want = keepSelection && prev != null &&
    state.tracks.some((t) => t.track_id === prev) ? prev : state.tracks[0].track_id;
  sel.value = want;
  await selectTrack(want);
}

async function selectTrack(id) {
  state.trackId = id;
  state.laps = await api(`api/tracks/${id}/laps`);
  renderLapList();
}

function fmtSession(lap) {
  // "2026-07-11T15:19:09" -> "07-11 15:19"
  const at = (lap.started_at || "").slice(5, 16).replace("T", " ");
  return `${at} · ${lap.session_type_name || "?"} · session #${lap.session_id}`;
}

function roleBadge(role) {
  // your own laps are the default case — only other cars' laps get a badge
  if (role === "player") return "";
  const label = { rival: "RIVAL", pb_ghost: "PB·G", guest: "GUEST" }[role] || role;
  return `<span class="badge ${role}">${label}</span>`;
}

/* Ghost laps are times-only pace references: real lap clock vs distance
   (and TimeTrial-packet sector times), none of the fabricated channels.
   They can be compared against, not viewed. */
function isPaceRef(lap) {
  return lap.car_role === "pb_ghost" || lap.car_role === "rival";
}

/* Any driving aid active? true / false / null (not recorded, older laps). */
function assistsOn(a) {
  if (!a) return null;
  return !!(a.tc || a.abs || a.gearbox >= 3 || a.racing_line ||
            a.brake_assist || a.steer_assist);
}

function visibleLaps() {
  let laps = state.laps.slice();
  if (state.roleFilter === "player")
    laps = laps.filter((l) => l.car_role === "player");
  else if (state.roleFilter === "ghost")
    laps = laps.filter((l) => l.car_role !== "player");
  if (state.assistFilter !== "all")
    laps = laps.filter((l) =>
      assistsOn(l.assists) === (state.assistFilter === "on"));
  if (state.hideInvalid) laps = laps.filter((l) => l.valid);
  if (state.sort === "fastest")
    laps.sort((a, b) => (a.lap_time_ms || 1e12) - (b.lap_time_ms || 1e12));
  return laps;
}

function renderLapList() {
  const box = $("lap-list");
  box.innerHTML = "";
  if (!state.laps.length) {
    box.innerHTML = '<div class="empty-list">No complete laps on this track yet — finish a full lap and it appears here automatically.</div>';
    return;
  }
  const laps = visibleLaps();
  if (!laps.length) {
    box.innerHTML = '<div class="empty-list">All laps are hidden by the current filters.</div>';
    return;
  }
  const ranked = state.sort === "fastest";
  const baseMs = ranked ? laps[0].lap_time_ms : null;
  let lastSession = null;
  const bestId = state.laps.reduce((b, l) =>
    l.car_role === "player" && l.valid &&
    (!b || l.lap_time_ms < b.lap_time_ms) ? l : b, null)?.id;
  laps.forEach((lap, i) => {
    if (!ranked && lap.session_id !== lastSession) {
      lastSession = lap.session_id;
      const sid = lap.session_id;
      const folded = state.folded.has(sid);
      const head = document.createElement("div");
      head.className = "sess-head" + (folded ? " folded" : "");
      const n = laps.filter((l) => l.session_id === sid).length;
      head.innerHTML = `<span class="tri">${folded ? "▸" : "▾"}</span>` +
        fmtSession(lap) + (folded ? ` · ${n} lap${n === 1 ? "" : "s"}` : "");
      head.addEventListener("click", () => {
        if (state.folded.has(sid)) state.folded.delete(sid);
        else state.folded.add(sid);
        renderLapList();
      });
      box.appendChild(head);
    }
    if (!ranked && state.folded.has(lap.session_id)) return;
    const row = document.createElement("div");
    row.className = "lap-row";
    if (state.lapA && lap.id === state.lapA.id) row.classList.add("sel");
    if (state.lapB && lap.id === state.lapB.id) row.classList.add("ref-sel");
    const isRef = state.lapB && lap.id === state.lapB.id;
    const gap = ranked && i > 0 && lap.lap_time_ms && baseMs
      ? `<span class="gap">+${((lap.lap_time_ms - baseMs) / 1000).toFixed(3)}</span>` : "";
    const team = lap.team_name ? lap.team_name + " · " : "";
    const sub = isPaceRef(lap)
      ? `${team}pace — times only`
      : ranked
        ? `${team}${fmtSession(lap).split(" · ")[0]} · L${lap.lap_num}`
        : `${team}L${lap.lap_num}`;
    if (isPaceRef(lap)) {
      row.classList.add("pace");
      row.title = "a ghost's pace: lap clock vs distance and sector times" +
        " — no telemetry to view, so clicking compares against it";
    }
    row.innerHTML = `
      ${ranked ? `<span class="rank">${i + 1}</span>` : ""}
      ${roleBadge(lap.car_role)}
      <div class="lap-main">
        <div class="lap-time">${fmtTime(lap.lap_time_ms, 1)} ${gap}
          ${lap.id === bestId ? '<span class="pb">BEST</span>' : ""}
          ${lap.valid ? "" : '<span class="inv">INV</span>'}</div>
        <div class="lap-sub">${sub}</div>
      </div>
      <button class="refbtn ${isRef ? "on" : ""}"
        title="${isRef ? "stop comparing against this lap" : "compare the viewed lap against this one"}">VS</button>
      ${state.readonly ? "" : '<button class="del" title="delete lap">✕</button>'}`;
    row.addEventListener("click", () =>
      isPaceRef(lap) ? toggleRef(lap.id) : viewLap(lap.id));
    row.querySelector(".refbtn").addEventListener("click", (e) => {
      e.stopPropagation(); toggleRef(lap.id);
    });
    row.querySelector(".del")?.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this lap?")) return;
      await api(`api/laps/${lap.id}`, { method: "DELETE" });
      if (state.lapA && state.lapA.id === lap.id) state.lapA = null;
      if (state.lapB && state.lapB.id === lap.id) state.lapB = null;
      await loadTracks(true);
      rebuildScene();
    });
    box.appendChild(row);
  });
}

async function viewLap(id) {
  await tracksReady;
  const lap = prepLap(await api(`api/laps/${id}`));
  if (!lap.samples.spd) {   // times-only (pace ref, or an imported one):
    state.lapB = lap;       // nothing to replay — compare against it
    state.mode = "gap";
    renderLapList(); rebuildScene();
    return;
  }
  state.lapA = lap;
  state.t = 0; state.playing = true;
  renderLapList(); rebuildScene();
}
async function toggleRef(id) {
  await tracksReady;
  if (state.lapB && state.lapB.id === id) state.lapB = null;
  else {
    state.lapB = prepLap(await api(`api/laps/${id}`));
    state.mode = "gap";   // comparing: show where time is gained/lost
  }
  renderLapList(); rebuildScene();
}

/* ------------------------------------------------- import (.trace files)
   No import button: drop an exported .trace file anywhere on the window.
   Imported laps are stored as role "guest" and live in the GHOSTS filter. */

async function importTraces(files) {
  let last = null, dupes = 0;
  const errs = [];
  for (const f of files) {
    try {
      const r = await fetch("api/import",
                            { method: "POST", body: await f.arrayBuffer() });
      const res = await r.json();
      if (!r.ok) throw new Error(res.error || "server error " + r.status);
      if (res.duplicate) dupes++;
      last = res;
    } catch (e) { errs.push(`${f.name}: ${e.message}`); }
  }
  if (last) {
    await loadTracks(true);
    if (state.trackId !== last.track_id) {
      const sel = $("track-select");
      sel.value = last.track_id;
      sel.dispatchEvent(new Event("change"));   // switches track, syncs label
    }
    await viewLap(last.lap_id);
  }
  const msg = [];
  if (dupes) msg.push(dupes === 1 ? "That lap is already in your lab."
                                  : `${dupes} of those laps are already in your lab.`);
  if (errs.length) msg.push("Couldn't import:\n" + errs.join("\n"));
  if (msg.length) alert(msg.join("\n\n"));
}

let dragDepth = 0;
window.addEventListener("dragenter", (e) => {
  if (state.readonly || ![...e.dataTransfer.types].includes("Files")) return;
  e.preventDefault();
  if (++dragDepth === 1) $("drop-veil").style.display = "";
});
window.addEventListener("dragover", (e) => { if (dragDepth) e.preventDefault(); });
window.addEventListener("dragleave", () => {
  if (dragDepth && --dragDepth === 0) $("drop-veil").style.display = "none";
});
window.addEventListener("drop", (e) => {
  if (!dragDepth) return;
  e.preventDefault();
  dragDepth = 0;
  $("drop-veil").style.display = "none";
  importTraces([...e.dataTransfer.files]);
});

/* ---------------------------------------------------------------- track map */

const map = $("map");
const mctx = map.getContext("2d");
let view = null;   // {sx, sy, scale} world->px

function fitMap() {
  const laps = [state.lapA, state.lapB].filter(Boolean);
  if (!laps.length) { view = null; return; }
  const dpr = window.devicePixelRatio || 1;
  map.width = map.clientWidth * dpr; map.height = map.clientHeight * dpr;
  let minX = 1e9, maxX = -1e9, minZ = 1e9, maxZ = -1e9;
  const geom = trackGeom(laps[0].track_id);
  if (geom) {   // frame the fixed outline: identical view for every lap
    for (let i = 0; i < geom.n; i++) {
      if (geom.xs[i] < minX) minX = geom.xs[i];
      if (geom.xs[i] > maxX) maxX = geom.xs[i];
      if (geom.zs[i] < minZ) minZ = geom.zs[i];
      if (geom.zs[i] > maxZ) maxZ = geom.zs[i];
    }
  } else for (const lap of laps) {
    const s = lap.samples;
    for (let i = 0; i < s.x.length; i++) {
      if (s.x[i] < minX) minX = s.x[i]; if (s.x[i] > maxX) maxX = s.x[i];
      if (s.z[i] < minZ) minZ = s.z[i]; if (s.z[i] > maxZ) maxZ = s.z[i];
    }
  }
  // padding leaves room for corner badges, the legend row and the toolbar;
  // the TIMING/TELEMETRY cards float over the map's (mostly empty) corners
  const pad = 58 * dpr, padT = 64 * dpr, padB = 78 * dpr;
  const w = map.width - 2 * pad, h = map.height - padT - padB;
  const scale = Math.min(w / (maxX - minX || 1), h / (maxZ - minZ || 1));
  view = {
    scale, dpr, baseScale: scale,
    ox: pad + (w - (maxX - minX) * scale) / 2 - minX * scale,
    // z axis flipped so "north" is up
    oy: padT + (h - (maxZ - minZ) * scale) / 2 + maxZ * scale,
  };
  // reapply a saved zoom (world-space, so it survives lap/ref switches)
  if (state.mapZoom) {
    const { k, wx, wz } = state.mapZoom;
    view.scale = view.baseScale * k;
    view.ox = map.width / 2 - wx * view.scale;
    view.oy = map.height / 2 + wz * view.scale;
  }
}
const W2X = (x) => view.ox + x * view.scale;
const W2Y = (z) => view.oy - z * view.scale;
const X2W = (px) => (px - view.ox) / view.scale;
const Y2W = (py) => (view.oy - py) / view.scale;

/* Remember the current zoom as factor + world centre; null when fitted. */
function saveZoom() {
  const k = view.scale / view.baseScale;
  state.mapZoom = k <= 1.02 ? null
    : { k, wx: X2W(map.width / 2), wz: Y2W(map.height / 2) };
  if (!state.mapZoom) fitMap();
}

/* The static map layer (ribbon, racing line, markers, badges) is expensive,
   so it renders once into an offscreen canvas; per-frame we blit + draw dots. */
let mapStatic = null;
let dctx = null;   // context the path helpers draw into

/* Map coordinates for a lap: the clean synth line normally, the real
   (drift-corrected raw) trajectory once zoomed past ~1 px/m — at that
   scale actual corner-cut geometry matters more than tidiness. */
function mapXZ(lap) {
  if (lap.trueX && view && view.scale / view.dpr >= 1)
    return { x: lap.trueX, z: lap.trueZ };
  return { x: lap.samples.x, z: lap.samples.z };
}

function pathStroke(lap, i0, i1, color, width, alpha) {
  const p = mapXZ(lap);
  dctx.beginPath();
  dctx.moveTo(W2X(p.x[i0]), W2Y(p.z[i0]));
  for (let i = i0 + 1; i <= i1; i++) dctx.lineTo(W2X(p.x[i]), W2Y(p.z[i]));
  dctx.globalAlpha = alpha == null ? 1 : alpha;
  dctx.strokeStyle = color; dctx.lineWidth = width * view.dpr;
  dctx.lineJoin = "round"; dctx.lineCap = "round";
  dctx.stroke(); dctx.globalAlpha = 1;
}
function fullStroke(lap, color, width, alpha) {
  pathStroke(lap, 0, lap.samples.x.length - 1, color, width, alpha);
}
function outlineStroke(geom, color, width, alpha) {
  dctx.beginPath();
  dctx.moveTo(W2X(geom.xs[0]), W2Y(geom.zs[0]));
  for (let i = 1; i < geom.n; i++) dctx.lineTo(W2X(geom.xs[i]), W2Y(geom.zs[i]));
  dctx.closePath();
  dctx.globalAlpha = alpha == null ? 1 : alpha;
  dctx.strokeStyle = color; dctx.lineWidth = width * view.dpr;
  dctx.lineJoin = "round"; dctx.lineCap = "round";
  dctx.stroke(); dctx.globalAlpha = 1;
}
/* Racing-line colors per sample, for the active mode. */
function computeLineColors(A) {
  const s = A.samples, n = s.d.length, colors = new Array(n);
  if (state.mode === "gap" && state.lapB) {
    const B = state.lapB, lim = Math.min(A.maxD, B.maxD);
    for (let i = 0; i < n; i++) {
      const d0 = Math.max(0, Math.min(s.d[i], lim) - 40);
      const d1 = Math.min(lim, s.d[i] + 40);
      const dA = interp(s.d, s.t, d1) - interp(s.d, s.t, d0);
      const dB = interp(B.samples.d, B.samples.t, d1) -
                 interp(B.samples.d, B.samples.t, d0);
      const slope = (dA - dB) / Math.max(1, d1 - d0);  // ms lost per metre
      colors[i] = ramp(GAP_RAMP, 0.5 + slope / 4);
    }
  } else {
    for (let i = 0; i < n; i++)
      colors[i] = ramp(SPEED_RAMP, Math.pow(
        Math.max(0, s.spd[i] - SPEED_MIN) / (SPEED_MAX - SPEED_MIN),
        SPEED_GAMMA));
  }
  A.lineColors = colors;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function renderMapStatic() {
  if (!view || !map.width || !map.height) { mapStatic = null; return; }
  mapStatic = document.createElement("canvas");
  mapStatic.width = map.width; mapStatic.height = map.height;
  dctx = mapStatic.getContext("2d");
  const A = state.lapA, dpr = view.dpr, s = A.samples;

  // ribbon: neutral edge under a dark road surface, drawn from the fixed
  // circuit outline when we have one; the colored S2/S3 gates alone mark
  // the sectors. Widths switch to true scale (a ~14 m road) once zoomed
  // far enough that that is wider than the stylised base width.
  const mpx = view.scale / dpr;   // logical px per metre
  const geom = trackGeom(A.track_id);
  if (geom) {
    outlineStroke(geom, "#3a4150", Math.max(12.5, 15 * mpx), 0.9);
    outlineStroke(geom, "#1b202a", Math.max(9, 13 * mpx));
  } else {
    fullStroke(A, "#3a4150", Math.max(12.5, 15 * mpx), 0.9);
    fullStroke(A, "#1b202a", Math.max(9, 13 * mpx));
    if (state.lapB) fullStroke(state.lapB, "rgba(251,146,60,.25)", 1.5);
  }

  // reference lap's line under the viewed lap's, for line comparison —
  // except pace references: they have no driven line, only a position
  // along the centerline, so drawing one would invent geometry
  if (geom && state.lapB && !isPaceRef(state.lapB))
    fullStroke(state.lapB, "rgba(251,146,60,.55)", Math.max(1.5, 1.2 * mpx));

  // racing line colored by speed / gap; true car width when zoomed
  const lineW = Math.max(2.6, 1.9 * mpx) * dpr;
  const P = mapXZ(A);
  const stepN = Math.max(1, Math.floor(P.x.length / 900));
  for (let i = stepN; i < P.x.length; i += stepN) {
    dctx.beginPath();
    dctx.moveTo(W2X(P.x[i - stepN]), W2Y(P.z[i - stepN]));
    dctx.lineTo(W2X(P.x[i]), W2Y(P.z[i]));
    dctx.strokeStyle = A.lineColors[i];
    dctx.lineWidth = lineW; dctx.lineCap = "round";
    dctx.stroke();
  }

  // sector boundary notches
  const centX = (Math.min(...s.x) + Math.max(...s.x)) / 2;
  const centZ = (Math.min(...s.z) + Math.max(...s.z)) / 2;
  function outward(d) {
    const x0 = interp(s.d, s.x, Math.max(0, d - 10));
    const z0 = interp(s.d, s.z, Math.max(0, d - 10));
    const x1 = interp(s.d, s.x, Math.min(A.maxD, d + 10));
    const z1 = interp(s.d, s.z, Math.min(A.maxD, d + 10));
    let nx = -(z1 - z0), nz = x1 - x0;
    const len = Math.hypot(nx, nz) || 1;
    nx /= len; nz /= len;
    const px = interp(s.d, s.x, d), pz = interp(s.d, s.z, d);
    if (nx * (px - centX) + nz * (pz - centZ) < 0) { nx = -nx; nz = -nz; }
    // to screen space (z axis flips)
    return { x: W2X(px), y: W2Y(pz), nx: nx, ny: -nz };
  }
  if (A.secD) {
    A.secD.forEach((d, i) => {
      const o = outward(d);
      dctx.beginPath();
      dctx.moveTo(o.x - o.nx * 8 * dpr, o.y - o.ny * 8 * dpr);
      dctx.lineTo(o.x + o.nx * 10 * dpr, o.y + o.ny * 10 * dpr);
      dctx.strokeStyle = SEC_COLORS[i + 1]; dctx.lineWidth = 2.5 * dpr;
      dctx.stroke();
      dctx.fillStyle = SEC_COLORS[i + 1];
      dctx.font = "700 " + 10 * dpr + "px sans-serif"; dctx.textAlign = "center";
      dctx.fillText("S" + (i + 2), o.x + o.nx * 20 * dpr, o.y + o.ny * 20 * dpr + 3 * dpr);
    });
  }

  // start/finish
  dctx.fillStyle = "#dbe2ee";
  dctx.save();
  dctx.translate(W2X(s.x[0]), W2Y(s.z[0]));
  dctx.rotate(Math.atan2(-(s.z[1] - s.z[0]), s.x[1] - s.x[0]) + Math.PI / 2);
  dctx.fillRect(-8 * dpr, -2 * dpr, 16 * dpr, 4 * dpr);
  dctx.restore();

  // corner numbers
  dctx.font = "700 " + 9.5 * dpr + "px sans-serif"; dctx.textAlign = "center";
  for (const c of A.corners || []) {
    const o = outward(c.apexD);
    dctx.fillStyle = "#4d5a70";
    dctx.fillText(c.n, o.x + o.nx * 17 * dpr, o.y + o.ny * 17 * dpr + 3 * dpr);
  }

  // biggest time losses / gains vs reference -> badges at the corners
  if (state.lapB && A.corners && A.corners.length) {
    // badge every corner that moves the needle, losses and gains alike;
    // the cap only guards readability on a wreck of a lap
    const ranked = A.corners.filter((c) => c.loss != null)
      .sort((a, b) => Math.abs(b.loss) - Math.abs(a.loss));
    const badges = ranked.filter((c) => Math.abs(c.loss) >= 0.1).slice(0, 8);
    dctx.font = "800 " + 10 * dpr + "px sans-serif";
    for (const c of badges) {
      const o = outward(c.apexD);
      const lost = c.loss > 0;
      const bx = o.x + o.nx * 40 * dpr, by = o.y + o.ny * 40 * dpr;
      dctx.strokeStyle = lost ? "rgba(248,113,113,.5)" : "rgba(52,211,153,.5)";
      dctx.lineWidth = 1 * dpr;
      dctx.beginPath(); dctx.moveTo(o.x + o.nx * 6 * dpr, o.y + o.ny * 6 * dpr);
      dctx.lineTo(bx, by); dctx.stroke();
      const label = "T" + c.n + "  " + (lost ? "+" : "−") +
        Math.abs(c.loss).toFixed(2);
      const w = dctx.measureText(label).width + 14 * dpr;
      roundRect(dctx, bx - w / 2, by - 9 * dpr, w, 18 * dpr, 5 * dpr);
      dctx.fillStyle = lost ? "rgba(84,17,17,.92)" : "rgba(4,55,38,.92)";
      dctx.fill();
      dctx.strokeStyle = lost ? "#7f2626" : "#0f5c40";
      dctx.stroke();
      dctx.fillStyle = lost ? "#fda4a4" : "#6ee7b7";
      dctx.fillText(label, bx, by + 3.5 * dpr);
    }
  }
  dctx = null;
}

function drawMap() {
  mctx.clearRect(0, 0, map.width, map.height);
  if (!view || !state.lapA) return;
  const A = state.lapA, dpr = view.dpr, s = A.samples;
  if (mapStatic && mapStatic.width) mctx.drawImage(mapStatic, 0, 0);

  // dots grow to roughly car size once the zoom makes that bigger
  const rA = Math.max(8 * dpr, 1.6 * view.scale);
  const rB = Math.max(7 * dpr, 1.4 * view.scale);

  // ghost dot (reference lap at same elapsed time)
  if (state.lapB) {
    const B = state.lapB, tB = Math.min(state.t, B.duration);
    const pB = mapXZ(B);
    const bx = interp(B.samples.t, pB.x, tB);
    const bz = interp(B.samples.t, pB.z, tB);
    mctx.beginPath();
    mctx.arc(W2X(bx), W2Y(bz), rB, 0, 7);
    mctx.fillStyle = "#fb923c"; mctx.globalAlpha = 0.85; mctx.fill();
    mctx.globalAlpha = 1;
  }

  // player dot
  const pA = mapXZ(A);
  const ax = interp(s.t, pA.x, state.t), az = interp(s.t, pA.z, state.t);
  mctx.beginPath();
  mctx.arc(W2X(ax), W2Y(az), rA, 0, 7);
  mctx.fillStyle = "#22d3ee";
  mctx.strokeStyle = "#0b0e13"; mctx.lineWidth = 2 * dpr;
  mctx.shadowColor = "#22d3ee"; mctx.shadowBlur = 14 * dpr;
  mctx.fill(); mctx.shadowBlur = 0; mctx.stroke();
}

function mapSeek(px, py) {
  const s = state.lapA.samples, p = mapXZ(state.lapA);
  let best = -1, bestDist = 1e18;
  const step = Math.max(1, Math.floor(p.x.length / 2000));
  for (let i = 0; i < p.x.length; i += step) {
    const dx = W2X(p.x[i]) - px, dy = W2Y(p.z[i]) - py;
    const d2 = dx * dx + dy * dy;
    if (d2 < bestDist) { bestDist = d2; best = i; }
  }
  if (best >= 0 && bestDist < (40 * view.dpr) ** 2) seek(s.t[best]);
}

/* ------------------------------------------------- map zoom & pan
   Wheel = zoom at cursor (on the map or on any chart). Drag = pan (when
   zoomed). Plain click = seek. Double-click / RESET = back to full track.
   While zoomed, the charts focus on the visible stretch of track
   (state.viewD); the map view is the single source of truth. */

let zoomRaf = 0, zoomSettle = 0;

function afterZoomGesture() {
  saveZoom();
  if (!zoomRaf) zoomRaf = requestAnimationFrame(() => {
    zoomRaf = 0;
    renderMapStatic();
    drawMap();
  });
  clearTimeout(zoomSettle);   // charts rebuild once the gesture settles
  zoomSettle = setTimeout(() => { updateViewD(); }, 140);
}

/* Focused distance range = longest contiguous visible run of lap A. */
function computeViewD() {
  if (!view || !state.lapA || !state.mapZoom) return null;
  const s = state.lapA.samples, w = map.width, h = map.height;
  let bestD0 = 0, bestD1 = 0, runStart = -1;
  const flush = (endI) => {
    if (runStart < 0) return;
    const d0 = s.d[runStart], d1 = s.d[endI];
    if (d1 - d0 > bestD1 - bestD0) { bestD0 = d0; bestD1 = d1; }
    runStart = -1;
  };
  for (let i = 0; i < s.x.length; i++) {
    const px = W2X(s.x[i]), py = W2Y(s.z[i]);
    const vis = px >= 0 && px <= w && py >= 0 && py <= h;
    if (vis && runStart < 0) runStart = i;
    if (!vis) flush(i - 1);
  }
  flush(s.x.length - 1);
  if (bestD1 - bestD0 < 30) return null;              // degenerate
  if (bestD1 - bestD0 > state.lapA.maxD * 0.96) return null;  // ~whole lap
  return [bestD0, bestD1];
}

function updateViewD() {
  state.viewD = computeViewD();
  for (const cfg of charts) buildChart(cfg);
  buildDeltaChart();
  updateZoomChip();
  updateScrubTint();
  drawFrame();
}

function updateZoomChip() {
  const chip = $("zoom-chip");
  if (!state.mapZoom) { chip.style.display = "none"; return; }
  chip.style.display = "";
  let txt = "×" + state.mapZoom.k.toFixed(1);
  if (state.viewD) {
    const [d0, d1] = state.viewD;
    const cs = (state.lapA.corners || [])
      .filter((c) => c.apexD >= d0 && c.apexD <= d1).map((c) => c.n);
    txt += cs.length ? ` · T${cs[0]}${cs.length > 1 ? "–T" + cs[cs.length - 1] : ""}`
                     : ` · ${Math.round(d0)}–${Math.round(d1)} m`;
  }
  $("zoom-txt").textContent = txt;
}

function resetZoom() {
  if (!state.mapZoom) return;
  state.mapZoom = null;
  fitMap();
  renderMapStatic();
  updateViewD();
}
$("zoom-reset").addEventListener("click", resetZoom);
map.addEventListener("dblclick", resetZoom);

/* Trackpad pinch arrives as ctrl+wheel with small deltas (Chrome,
   Firefox, Edge), so it needs a higher sensitivity than a real wheel. */
function wheelZoomFactor(e) {
  return Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015));
}

function mapZoomAt(clientX, clientY, f) {
  const r = map.getBoundingClientRect();
  const px = (clientX - r.left) * view.dpr, py = (clientY - r.top) * view.dpr;
  const k = Math.min(40, Math.max(1, (view.scale / view.baseScale) * f));
  const scale = view.baseScale * k;
  // keep the world point under the cursor fixed
  view.ox = px - X2W(px) * scale;
  view.oy = py + Y2W(py) * scale;
  view.scale = scale;
  afterZoomGesture();
}

map.addEventListener("wheel", (e) => {
  if (!view || !state.lapA) return;
  e.preventDefault();
  mapZoomAt(e.clientX, e.clientY, wheelZoomFactor(e));
}, { passive: false });

/* Safari reports trackpad pinch as gesture events, not ctrl+wheel. */
function onPinch(el, zoomAt) {
  let last = 1;
  el.addEventListener("gesturestart", (e) => {
    e.preventDefault();
    last = e.scale;
  });
  el.addEventListener("gesturechange", (e) => {
    e.preventDefault();
    if (!view || !state.lapA) return;
    zoomAt(e.clientX, e.clientY, e.scale / last);
    last = e.scale;
  });
  el.addEventListener("gestureend", (e) => e.preventDefault());
}
onPinch(map, mapZoomAt);

{
  let downX = 0, downY = 0, panning = false, dragged = false;
  map.addEventListener("pointerdown", (e) => {
    if (!view || !state.lapA) return;
    downX = e.clientX; downY = e.clientY;
    panning = true; dragged = false;
    map.setPointerCapture(e.pointerId);
  });
  map.addEventListener("pointermove", (e) => {
    if (!panning) return;
    const dx = e.clientX - downX, dy = e.clientY - downY;
    if (!dragged && Math.hypot(dx, dy) < 4) return;
    dragged = true;
    if (!state.mapZoom) return;   // fitted: nothing to pan
    view.ox += dx * view.dpr; view.oy += dy * view.dpr;
    downX = e.clientX; downY = e.clientY;
    afterZoomGesture();
  });
  map.addEventListener("pointerup", (e) => {
    if (!panning) return;
    panning = false;
    if (!dragged) {   // plain click -> seek to nearest point on the line
      const r = map.getBoundingClientRect();
      mapSeek((e.clientX - r.left) * view.dpr, (e.clientY - r.top) * view.dpr);
    }
  });
  map.addEventListener("pointercancel", () => { panning = false; });
}

/* ---------------------------------------------------------------- halo hud */

/* Original halo cluster (arcs around the speed dial), with the steering
   section stacked on top; the whole card floats and can be dragged. */
const HALO = { cx: 155, cy: 300, r: 84 };
const THR = { from: 230, to: 130 };   // left side, fills bottom -> top
const BRK = { from: -50, to: 50 };    // right side, fills bottom -> top
const N_REV = 15;

function polar(cx, cy, r, deg) {
  const rad = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(rad), cy - r * Math.sin(rad)];
}
function arcPath(a0, a1) {
  const { cx, cy, r } = HALO;
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const sweep = a1 > a0 ? 0 : 1;
  return `M ${x0.toFixed(1)} ${y0.toFixed(1)} A ${r} ${r} 0 0 ${sweep} ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}
const SVG = "http://www.w3.org/2000/svg";

function initHalo() {
  $("thr-bg").setAttribute("d", arcPath(THR.from, THR.to));
  $("brk-bg").setAttribute("d", arcPath(BRK.from, BRK.to));
  // ticks at 0/25/50/75/100% on both arcs
  const ticks = $("arc-ticks");
  for (const cfg of [THR, BRK]) {
    for (const f of [0, 0.25, 0.5, 0.75, 1]) {
      const a = cfg.from + (cfg.to - cfg.from) * f;
      const [x0, y0] = polar(HALO.cx, HALO.cy, HALO.r - 9, a);
      const [x1, y1] = polar(HALO.cx, HALO.cy, HALO.r + 9, a);
      const ln = document.createElementNS(SVG, "line");
      ln.setAttribute("x1", x0); ln.setAttribute("y1", y0);
      ln.setAttribute("x2", x1); ln.setAttribute("y2", y1);
      ln.setAttribute("class", "arc-tick");
      ticks.appendChild(ln);
    }
  }
  // rev-light strip, just above the arcs
  const strip = $("rev-strip");
  for (let i = 0; i < N_REV; i++) {
    const seg = document.createElementNS(SVG, "rect");
    seg.setAttribute("x", 65 + i * 12.4);
    seg.setAttribute("y", 195);
    seg.setAttribute("width", 9); seg.setAttribute("height", 8);
    seg.setAttribute("rx", 2);
    seg.setAttribute("class", "rev-seg");
    seg.id = "rev-" + i;
    strip.appendChild(seg);
  }
}

/* Fold/unfold the whole TELEMETRY card down to its tag — the red card
   tags are the fold toggles (same interaction as the TIMING card), the
   grey pills (DRAG, STEER) are controls within an open card. */
function applyHudOpen() {
  $("hud").classList.toggle("closed", !state.hudOpen);
  $("hud-tag").textContent = `TELEMETRY ${state.hudOpen ? "▾" : "▸"}`;
}

/* Show/hide the steering section; the cluster slides up to fill the gap. */
function applyHudSteer() {
  const on = state.hudSteer;
  $("hud-steer-btn").classList.toggle("on", on);
  $("steer-block").style.display = on ? "" : "none";
  $("cluster-block").setAttribute("transform",
    on ? "" : "translate(0,-176)");
  $("halo").setAttribute("viewBox", on ? "0 0 310 400" : "0 0 310 224");
}

const REV_COLORS = (i) => i < 5 ? "#34d399" : i < 10 ? "#f87171" : "#c4b5fd";

/* aero: 1 = X-mode (straight), 0 = Z-mode (corner), null = not recorded. */
function setHalo(thr, brk, speed, gear, aero, boost, steer, rpm) {
  $("thr-arc").setAttribute("d",
    thr <= 0.5 ? "M 0 0" : arcPath(THR.from, THR.from + (THR.to - THR.from) * (thr / 100)));
  $("brk-arc").setAttribute("d",
    brk <= 0.5 ? "M 0 0" : arcPath(BRK.from, BRK.from + (BRK.to - BRK.from) * (brk / 100)));
  $("hud-speed").textContent = Math.round(speed);
  $("hud-gear").textContent = gear > 0 ? gear : gear === 0 ? "N" : "R";

  // 2026 systems: overtake mode (boost button) + active aero X/Z
  const bp = $("boost-pill"), bt = $("boost-txt");
  bp.setAttribute("class", boost ? "pill on-ot" : "pill");
  bt.style.fill = boost ? "#d8b4fe" : "";
  const ap = $("aero-pill"), at = $("aero-txt");
  ap.setAttribute("class", aero ? "pill on-x" : "pill");
  at.textContent = aero == null ? "AERO" : aero ? "X-MODE" : "Z-MODE";
  at.style.fill = aero ? "#6ee7b7" : "";

  // steering wheel: ±100% steer -> ±120° rotation
  $("wheel-rot").setAttribute("transform", "rotate(" + (steer * 1.2).toFixed(1) + ")");
  const sv = $("steer-val");
  sv.textContent = steer <= -3 ? "L " + Math.round(-steer)
    : steer >= 3 ? "R " + Math.round(steer) : "0";

  // rev lights
  const frac = Math.max(0, Math.min(1, (rpm - 4000) / 8600));
  const lit = Math.round(frac * N_REV);
  for (let i = 0; i < N_REV; i++)
    $("rev-" + i).style.fill = i < lit ? REV_COLORS(i) : "";
}

/* ---------------------------------------------------------------- charts */

const charts = [
  { id: "ch-speed", col: "spd", min: 0, max: null, corners: true },
  { id: "ch-thr", col: "thr", min: 0, max: 100, color: "#34d399" },   // HUD green
  { id: "ch-brk", col: "brk", min: 0, max: 100, color: "#f87171" },   // HUD red
  { id: "ch-steer", col: "str", min: -100, max: 100, zero: true,
    color: "#a78bfa" },                                             // violet
  { id: "ch-gear", col: "gear", step: "gear" },                     // gear N..8
  { id: "ch-ersstore", col: "ers", min: 0, max: 100, scale: 25, color: "#f59e0b" }, // ERS store % (0..4 MJ -> 0..100%)
  { id: "ch-ers", col: "ersm", step: "ers" },                       // ERS deploy mode
];
const chartCache = {};   // id -> {img, xOf, w, h}

/* ERS deploy mode (CarStatus ersDeployMode): 0 none, 1 medium,
   2 hotlap, 3 overtake (boost). Laps recorded before this channel
   existed simply lack the "ersm" column. */
const ERS_MODES = [
  { name: "NONE", color: "#6b7280" },
  { name: "MEDIUM", color: "#facc15" },
  { name: "HOTLAP", color: "#fb923c" },
  { name: "BOOST", color: "#c084fc" },
];
function ersModeName(v) {
  const m = ERS_MODES[Math.max(0, Math.min(3, Math.round(v)))];
  return m ? m.name : "";
}
const GEAR_COLOR = "#38bdf8";
const GEAR_LEVELS = ["N", "1", "2", "3", "4", "5", "6", "7", "8"].map(
  (name) => ({ name, color: GEAR_COLOR }));
function gearName(v) {
  const g = Math.round(v);
  return g < 0 ? "R" : g === 0 ? "N" : String(g);
}
/* Step-chart definitions: discrete levels, each with a label and the
   color lap A's line takes while on that level. */
const STEP_DEFS = {
  ers: { levels: ERS_MODES, missing: "ERS mode not recorded for this lap",
         labelColors: true },
  gear: { levels: GEAR_LEVELS, missing: "gear not recorded for this lap" },
};

/* Discrete channels (gear, ERS mode) as a stepped line, no fill. */
function buildStepChart(cfg) {
  const def = STEP_DEFS[cfg.step], L = def.levels, top = L.length - 1;
  const cv = $(cfg.id), dpr = window.devicePixelRatio || 1;
  if (!cv.clientWidth) { delete chartCache[cfg.id]; return; }
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d");
  const A = state.lapA, B = state.lapB;
  const maxD = Math.max(A.maxD, B ? B.maxD : 0);
  const w = cv.width, h = cv.height, padT = 3 * dpr, padB = 3 * dpr;
  const d0 = state.viewD ? state.viewD[0] : 0;
  const d1 = state.viewD ? state.viewD[1] : maxD;
  const X = (d) => ((d - d0) / (d1 - d0)) * w;
  const laneH = (h - padT - padB) / L.length;
  const lvl = (v) => Math.max(0, Math.min(top, Math.round(v || 0)));
  const Y = (v) => padT + (top - lvl(v)) * laneH + laneH / 2;   // lane center

  ctx.fillStyle = "#0e0f13"; ctx.fillRect(0, 0, w, h);
  drawSectorBands(ctx, A, X, w, h);
  // lane guides + labels (thinned out when the row is too short)
  const fontPx = 7 * dpr, every = Math.max(1, Math.ceil((fontPx + dpr) / laneH));
  ctx.font = fontPx + "px sans-serif"; ctx.textBaseline = "middle";
  for (let m = 0; m <= top; m++) {
    ctx.strokeStyle = "#1d1f26"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(0, Y(m)); ctx.lineTo(w, Y(m)); ctx.stroke();
    if (m % every) continue;
    ctx.fillStyle = def.labelColors ? L[m].color : "#7d8798";
    ctx.globalAlpha = 0.6;
    ctx.fillText(L[m].name, 3 * dpr, Y(m));
    ctx.globalAlpha = 1;
  }
  ctx.textBaseline = "alphabetic";

  const colA = A.samples[cfg.col];
  const colB = B ? B.samples[cfg.col] : null;
  // which lap(s) lack the channel (old recordings / pace references)
  const notes = [];
  if (!colA) notes.push(def.missing);
  if (B && !colB) notes.push(def.missing.replace("this lap", "the reference lap"));
  if (notes.length) {
    ctx.fillStyle = "#7d8798"; ctx.font = 10 * dpr + "px sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(notes.join("  ·  "), w / 2, h / 2 + 4 * dpr);
    ctx.textAlign = "left";
  }

  function range(s) {
    return [Math.max(0, lowerIdx(s.d, d0) - 1),
            Math.min(s.d.length - 1, lowerIdx(s.d, d1) + 2)];
  }
  // Comparing: lap A rides a bit above each lane center and the reference
  // a bit below, so two laps in the same mode stay visible side by side.
  const both = !!(colA && colB);
  const off = both ? Math.min(laneH * 0.22, 4 * dpr) : 0;

  // step line, each run stroked in its level's color — no fill
  function steps(s, col, dy, width, dash, alpha) {
    const [i0, i1] = range(s);
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    ctx.lineWidth = width * dpr; ctx.lineJoin = "miter";
    ctx.setLineDash(dash.map((v) => v * dpr)); ctx.globalAlpha = alpha;
    let i = i0;
    while (i < i1) {
      const m = lvl(col[i]);
      let j = i + 1;
      while (j < i1 && lvl(col[j]) === m) j++;
      ctx.beginPath();
      ctx.moveTo(X(s.d[i]), Y(m) + dy);
      ctx.lineTo(X(s.d[j]), Y(m) + dy);
      ctx.strokeStyle = L[m].color; ctx.stroke();
      // vertical transition into the next level, in the next level's color
      const n = lvl(col[j]);
      if (n !== m) {
        ctx.beginPath();
        ctx.moveTo(X(s.d[j]), Y(m) + dy); ctx.lineTo(X(s.d[j]), Y(n) + dy);
        ctx.strokeStyle = L[n].color; ctx.stroke();
      }
      i = j;
    }
    ctx.restore();
  }
  // reference lap: thinner, dashed, same level colors (ghost style)
  if (colB) steps(B.samples, colB, off, 1.2, [4, 3], 0.8);
  if (colA) steps(A.samples, colA, -off, 1.6, [], 1);

  chartCache[cfg.id] = { img: ctx.getImageData(0, 0, w, h), d0, d1, w, h };
}

function drawSectorBands(ctx, lap, X, w, h) {
  if (!lap.secD) return;
  const ranges = [[0, lap.secD[0]], [lap.secD[0], lap.secD[1]],
                  [lap.secD[1], lap.maxD]];
  ranges.forEach((r, i) => {
    ctx.fillStyle = SEC_COLORS[i]; ctx.globalAlpha = 0.05;
    ctx.fillRect(X(r[0]), 0, X(r[1]) - X(r[0]), h);
    ctx.globalAlpha = 1;
  });
  ctx.strokeStyle = "rgba(219,226,238,.14)"; ctx.lineWidth = 1;
  for (const d of lap.secD) {
    ctx.beginPath(); ctx.moveTo(X(d), 0); ctx.lineTo(X(d), h); ctx.stroke();
  }
}

function buildChart(cfg) {
  if (cfg.step) return buildStepChart(cfg);
  const cv = $(cfg.id), dpr = window.devicePixelRatio || 1;
  if (!cv.clientWidth) { delete chartCache[cfg.id]; return; }
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d");
  const A = state.lapA;
  const maxD = Math.max(A.maxD, state.lapB ? state.lapB.maxD : 0);
  let max = cfg.max, min = cfg.min;
  if (max == null) {
    max = 0;
    for (const lap of [A, state.lapB]) if (lap && lap.samples[cfg.col])
      max = Math.max(max, Math.max.apply(null, lap.samples[cfg.col]));
    max = Math.ceil(max / 50) * 50 || 50;
  }
  const w = cv.width, h = cv.height, padT = 3 * dpr, padB = 3 * dpr;
  // x domain: full lap, or the track section the map is zoomed into
  const d0 = state.viewD ? state.viewD[0] : 0;
  const d1 = state.viewD ? state.viewD[1] : maxD;
  const X = (d) => ((d - d0) / (d1 - d0)) * w;
  const Y = (v) => padT + (1 - (v - min) / (max - min)) * (h - padT - padB);

  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = "#0e0f13"; ctx.fillRect(0, 0, w, h);
  drawSectorBands(ctx, A, X, w, h);
  ctx.strokeStyle = "#1d1f26"; ctx.lineWidth = 1;
  for (const f of [0.25, 0.5, 0.75]) {
    ctx.beginPath(); ctx.moveTo(0, Y(min + (max - min) * f));
    ctx.lineTo(w, Y(min + (max - min) * f)); ctx.stroke();
  }
  if (cfg.zero) {
    ctx.strokeStyle = "#2b2e37";
    ctx.beginPath(); ctx.moveTo(0, Y(0)); ctx.lineTo(w, Y(0)); ctx.stroke();
  }
  // corner numbers along the top of the speed chart
  if (cfg.corners && A.corners) {
    ctx.fillStyle = "#445064";
    ctx.font = 8 * dpr + "px sans-serif"; ctx.textAlign = "center";
    for (const c of A.corners)
      if (c.apexD >= d0 && c.apexD <= d1) ctx.fillText(c.n, X(c.apexD), 9 * dpr);
    ctx.textAlign = "left";
  }

  function trace(lap, color, alpha, width) {
    const s = lap.samples, rawCol = s[cfg.col];
    if (!rawCol) return;
    const mult = cfg.scale || 1;
    const col = mult === 1 ? rawCol : rawCol.map((v) => (v != null ? v * mult : 0));
    const i0 = Math.max(0, lowerIdx(s.d, d0) - 1);
    const i1 = Math.min(s.d.length - 1, lowerIdx(s.d, d1) + 2);
    ctx.save();
    ctx.beginPath(); ctx.rect(0, 0, w, h); ctx.clip();
    ctx.beginPath();
    ctx.moveTo(X(s.d[i0]), Y(col[i0]));
    for (let i = i0 + 1; i <= i1; i++) ctx.lineTo(X(s.d[i]), Y(col[i]));
    ctx.globalAlpha = alpha; ctx.strokeStyle = color;
    ctx.lineWidth = width * dpr; ctx.stroke();
    ctx.restore(); ctx.globalAlpha = 1;
  }
  // comparing: your lines keep their channel colors; the reference lap is
  // a "ghost" — one neutral grey-white in every panel. Solid, not dashed:
  // the local differences against the reference are the whole point of
  // the comparison, so the line must be continuous — thinner and dimmer
  // is what says "the other lap". A pace reference has no channels, so
  // no line: its comparison lives in the DELTA panel and the map badges.
  if (state.lapB && state.lapB.samples[cfg.col])
    trace(state.lapB, "#b9c2d0", 0.75, 1);
  trace(A, cfg.color || "#22d3ee", 1, 1.5);

  chartCache[cfg.id] = { img: ctx.getImageData(0, 0, w, h), d0, d1, w, h };
}

function buildDeltaChart() {
  const row = $("delta-row");
  if (!state.lapB || !state.lapA) { row.style.display = "none"; delete chartCache["ch-delta"]; return; }
  row.style.display = "";
  const cv = $("ch-delta"), dpr = window.devicePixelRatio || 1;
  if (!cv.clientWidth) { delete chartCache["ch-delta"]; return; }
  cv.width = cv.clientWidth * dpr; cv.height = cv.clientHeight * dpr;
  const ctx = cv.getContext("2d");
  const A = state.lapA, B = state.lapB;
  const maxD = Math.min(A.maxD, B.maxD);
  const chartMaxD = Math.max(A.maxD, B.maxD);
  const d0 = state.viewD ? state.viewD[0] : 0;
  const d1 = state.viewD ? Math.min(state.viewD[1], chartMaxD) : chartMaxD;
  const grid = [], delta = [];
  let dmax = 0.05;
  for (let d = Math.min(d0, maxD); d <= Math.min(d1, maxD); d += 8) {
    const dt = (interp(A.samples.d, A.samples.t, d) -
                interp(B.samples.d, B.samples.t, d)) / 1000;
    grid.push(d); delta.push(dt);
    if (Math.abs(dt) > dmax) dmax = Math.abs(dt);
  }
  dmax *= 1.1;
  const w = cv.width, h = cv.height;
  const X = (d) => ((d - d0) / (d1 - d0)) * w;
  const Y = (v) => (1 - (v + dmax) / (2 * dmax)) * (h - 6 * dpr) + 3 * dpr;
  ctx.fillStyle = "#0e0f13"; ctx.fillRect(0, 0, w, h);
  drawSectorBands(ctx, A, X, w, h);
  ctx.strokeStyle = "#2b2e37";
  ctx.beginPath(); ctx.moveTo(X(Math.min(d0, maxD)), Y(0));
  ctx.lineTo(X(Math.min(maxD, d1)), Y(0)); ctx.stroke();

  // fill: above zero = losing time (red), below = gaining (green)
  for (let i = 1; i < grid.length; i++) {
    ctx.fillStyle = delta[i] >= 0 ? "rgba(248,113,113,.25)" : "rgba(52,211,153,.25)";
    ctx.fillRect(X(grid[i - 1]), Math.min(Y(0), Y(delta[i])),
                 X(grid[i]) - X(grid[i - 1]), Math.abs(Y(delta[i]) - Y(0)));
  }
  if (grid.length > 1) {
    ctx.beginPath();
    ctx.moveTo(X(grid[0]), Y(delta[0]));
    for (let i = 1; i < grid.length; i++) ctx.lineTo(X(grid[i]), Y(delta[i]));
    ctx.strokeStyle = "#dbe2ee"; ctx.lineWidth = 1.2 * dpr; ctx.stroke();
  }

  // scale label
  ctx.fillStyle = "#7d8798"; ctx.font = `${10 * dpr}px sans-serif`;
  ctx.fillText("±" + dmax.toFixed(2) + "s", 6 * dpr, 12 * dpr);

  chartCache["ch-delta"] = { img: ctx.getImageData(0, 0, w, h), d0, d1, w, h };
}

function drawChartCursors() {
  if (!state.lapA) return;
  const dA = interp(state.lapA.samples.t, state.lapA.samples.d, state.t);
  for (const id of Object.keys(chartCache)) {
    const c = chartCache[id];
    const ctx = $(id).getContext("2d");
    ctx.putImageData(c.img, 0, 0);
    if (dA < c.d0 || dA > c.d1) continue;   // playhead outside the zoomed section
    const x = ((dA - c.d0) / (c.d1 - c.d0)) * c.w;
    ctx.strokeStyle = "rgba(219,226,238,.7)"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, c.h); ctx.stroke();
  }
}

function chartSeek(e, cv) {
  if (!state.lapA) return;
  const cache = chartCache[cv.id];
  if (!cache) return;
  const r = cv.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
  const d = cache.d0 + frac * (cache.d1 - cache.d0);
  seek(interp(state.lapA.samples.d, state.lapA.samples.t,
              Math.min(d, state.lapA.maxD)));
}
/* Wheel on a chart = the same map zoom, anchored at the track point under
   the cursor's lap distance; state.viewD then brings the charts along. */
function chartZoomAt(cv, clientX, f) {
  const cache = chartCache[cv.id];
  if (!cache) return;
  const r = cv.getBoundingClientRect();
  const frac = Math.min(1, Math.max(0, (clientX - r.left) / r.width));
  const s = state.lapA.samples;
  const d = Math.min(cache.d0 + frac * (cache.d1 - cache.d0), state.lapA.maxD);
  // keep that point's map pixel fixed while the scale changes, exactly
  // like the map's own wheel handler does with the point under the cursor
  const px = W2X(interp(s.d, s.x, d)), py = W2Y(interp(s.d, s.z, d));
  const k = Math.min(40, Math.max(1, (view.scale / view.baseScale) * f));
  const scale = view.baseScale * k;
  view.ox = px - X2W(px) * scale;
  view.oy = py + Y2W(py) * scale;
  view.scale = scale;
  afterZoomGesture();
}

function chartWheel(e, cv) {
  if (!view || !state.lapA) return;
  e.preventDefault();
  chartZoomAt(cv, e.clientX, wheelZoomFactor(e));
}

for (const id of ["ch-speed", "ch-thr", "ch-brk", "ch-steer", "ch-gear", "ch-ersstore", "ch-ers", "ch-delta"]) {
  const cv = $(id);
  let dragging = false;
  cv.addEventListener("mousedown", (e) => { dragging = true; chartSeek(e, cv); });
  window.addEventListener("mousemove", (e) => { if (dragging) chartSeek(e, cv); });
  window.addEventListener("mouseup", () => { dragging = false; });
  cv.addEventListener("wheel", (e) => chartWheel(e, cv), { passive: false });
  onPinch(cv, (x, _y, f) => chartZoomAt(cv, x, f));
}

/* ---------------------------------------------------------------- scene & playback */

/* Floating TELEMETRY card: apply the saved drag position, clamped to the
   stage; without one it keeps its default bottom-right CSS anchor. */
function applyHudPos() {
  const p = state.hudPos;
  if (!p) return;
  const hud = $("hud"), stage = $("stage");
  const maxX = stage.clientWidth - hud.offsetWidth;
  const maxY = stage.clientHeight - hud.offsetHeight;
  hud.style.right = "auto"; hud.style.bottom = "auto";
  hud.style.left = Math.max(0, Math.min(maxX, p.x * stage.clientWidth)) + "px";
  hud.style.top = Math.max(0, Math.min(maxY, p.y * stage.clientHeight)) + "px";
}

function rebuildScene() {
  const has = !!state.lapA;
  applyHudPos();
  $("charts").classList.toggle("compare", !!state.lapB);
  $("empty-hint").style.display = has ? "none" : "";
  $("map-toolbar").style.display = has ? "" : "none";
  $("sector-card").style.display = has ? "" : "none";
  $("map-legend").innerHTML = has
    ? `<span class="k kA">${fmtTime(state.lapA.lap_time_ms, 1)} (${state.lapA.car_role})</span>` +
      (state.lapB ? `<span class="k kB">${fmtTime(state.lapB.lap_time_ms, 1)} (${state.lapB.car_role}) — reference</span>` : "")
    : "";
  if (!has) { mapStatic = null; drawMap(); return; }
  const A = state.lapA;
  const geom = trackGeom(A.track_id);
  for (const lap of [A, state.lapB])   // re-place laps if the outline was
    if (lap && geom && lap.geomVersion !== geom.version)   // recalibrated
      synthCoords(lap, geom);
  if (geom) {   // fixed turn numbers, derived from the outline
    const k = geom.total / (A.track_length || geom.total);
    A.corners = geom.corners.map((c) => ({
      n: c.n, startD: c.s0 / k, endD: c.s1 / k, apexD: c.apexS / k }));
  } else if (!A.corners) A.corners = computeCorners(A);
  if (state.lapB) cornerDeltas();
  if (state.mode === "gap" && !state.lapB) state.mode = "speed";
  fitMap();
  computeLineColors(A);
  renderMapStatic();
  updateToolbar();
  updateSectorCard();
  state.viewD = computeViewD();   // zoom carries over between laps
  updateScrubTint();
  updateZoomChip();
  renderSetupCard();
  for (const cfg of charts) buildChart(cfg);
  buildDeltaChart();
  drawFrame();
}

function updateToolbar() {
  const gapBtn = document.querySelector('#mode-seg button[data-mode="gap"]');
  gapBtn.disabled = !state.lapB;
  for (const b of document.querySelectorAll("#mode-seg button"))
    b.classList.toggle("on", b.dataset.mode === state.mode);
  const bar = $("leg-bar"), lo = $("leg-lo"), hi = $("leg-hi");
  const title = $("leg-title");
  if (state.mode === "gap") {
    title.textContent = "TRACK DOMINANCE";
    bar.style.background = `linear-gradient(90deg, ${GAP_RAMP.join(",")})`;
    lo.textContent = "YOU FASTER"; lo.style.color = "#22d3ee";
    hi.textContent = "VS FASTER"; hi.style.color = "#fb923c";
  } else {
    title.textContent = "SPEED";
    // legend bar mirrors the gamma curve applied to the racing line
    const stops = [];
    for (let i = 0; i <= 8; i++) {
      const p = i / 8;
      stops.push(`${ramp(SPEED_RAMP, Math.pow(p, SPEED_GAMMA))} ${p * 100}%`);
    }
    bar.style.background = `linear-gradient(90deg, ${stops.join(",")})`;
    lo.textContent = "≤" + SPEED_MIN; lo.style.color = "";
    hi.textContent = SPEED_MAX + " KM/H"; hi.style.color = "";
  }
}

function updateSectorCard() {
  const A = state.lapA, B = state.lapB;
  const card = $("sector-card");
  const tag = `<div class="card-tag" title="collapse / expand this card">` +
    `TIMING ${state.timingOpen ? "▾" : "▸"}</div>`;
  card.classList.toggle("closed", !state.timingOpen);
  if (!state.timingOpen) { card.innerHTML = tag; return; }
  const secs = (l) => [l.s1_ms, l.s2_ms, l.s3_ms];
  const fmtS = (ms) => ms ? (ms / 1000).toFixed(3) : "—";
  let html = `${tag}
    <table><tr><th></th><th class="s1">S1</th><th class="s2">S2</th>
    <th class="s3">S3</th><th>LAP</th></tr>
    <tr><td class="rowlbl">${B ? "YOU" : "LAP"}</td>
      ${secs(A).map((v) => `<td>${fmtS(v)}</td>`).join("")}
      <td class="laptime">${fmtTime(A.lap_time_ms, 1)}</td></tr>`;
  if (B) {
    html += `<tr><td class="rowlbl">VS</td>
      ${secs(B).map((v) => `<td>${fmtS(v)}</td>`).join("")}
      <td class="laptime">${fmtTime(B.lap_time_ms, 1)}</td></tr><tr>
      <td class="rowlbl">Δ</td>`;
    for (let i = 0; i < 3; i++) {
      const a = secs(A)[i], b = secs(B)[i];
      if (a && b) {
        const d = (a - b) / 1000;
        html += `<td class="${d <= 0 ? "neg" : "pos"}">${d >= 0 ? "+" : "−"}${Math.abs(d).toFixed(3)}</td>`;
      } else html += "<td>—</td>";
    }
    const dl = (A.lap_time_ms - B.lap_time_ms) / 1000;
    html += `<td class="laptime ${dl <= 0 ? "neg" : "pos"}">${dl >= 0 ? "+" : "−"}${Math.abs(dl).toFixed(3)}</td></tr>`;
  }
  card.innerHTML = html + "</table>";
}

// the card is rebuilt via innerHTML on every update, so the fold toggle
// lives on the container, which persists
$("sector-card").addEventListener("click", (e) => {
  if (!e.target.closest(".card-tag")) return;
  state.timingOpen = !state.timingOpen;
  try { localStorage.setItem("f1trace.timingOpen", state.timingOpen ? "1" : "0"); }
  catch (err) { /* private mode */ }
  updateSectorCard();
});
// the SETUP card has a dedicated toolbar toggle, so its tag just closes it
$("setup-card").addEventListener("click", (e) => {
  if (!e.target.closest(".card-tag")) return;
  state.setupOpen = false;
  renderSetupCard();
});

/* ------------------------------------------------- setup & assists panel */

const SETUP_ROWS = [
  ["Aerodynamics", null],
  ["Front / rear wing", (s) => s.front_wing + " / " + s.rear_wing],
  ["Transmission", null],
  ["Diff on / off throttle", (s) => s.on_throttle + " / " + s.off_throttle],
  ["Engine braking", (s) => s.engine_braking],
  ["Suspension geometry", null],
  ["Camber f / r", (s) => s.front_camber + " / " + s.rear_camber],
  ["Toe f / r", (s) => s.front_toe + " / " + s.rear_toe],
  ["Suspension", null],
  ["Springs f / r", (s) => s.front_susp + " / " + s.rear_susp],
  ["Anti-roll bar f / r", (s) => s.front_arb + " / " + s.rear_arb],
  ["Ride height f / r", (s) => s.front_height + " / " + s.rear_height],
  ["Brakes", null],
  ["Pressure / bias", (s) => s.brake_pressure + "% / " + s.brake_bias + "%"],
  ["Tyre pressure", null],
  ["Front l / r", (s) => s.tp_fl + " / " + s.tp_fr],
  ["Rear l / r", (s) => s.tp_rl + " / " + s.tp_rr],
  ["Fuel", null],
  ["Load", (s) => s.fuel_load + " kg"],
];

function assistLine(a) {
  if (!a || !Object.keys(a).length) return "not recorded";
  const tc = { 0: "off", 1: "medium", 2: "full" };
  const gb = { 1: "manual", 2: "manual+hint", 3: "auto" };
  const br = { 0: "off", 1: "low", 2: "medium", 3: "high" };
  const parts = [];
  if ("tc" in a) parts.push("TC " + (tc[a.tc] ?? a.tc));
  if ("abs" in a) parts.push("ABS " + (a.abs ? "on" : "off"));
  if ("gearbox" in a) parts.push("gears " + (gb[a.gearbox] ?? a.gearbox));
  if ("brake_assist" in a) parts.push("braking " + (br[a.brake_assist] ?? a.brake_assist));
  if ("steer_assist" in a) parts.push("steering " + (a.steer_assist ? "on" : "off"));
  if ("racing_line" in a) parts.push("line " + (a.racing_line ? "on" : "off"));
  if ("custom_setup" in a) parts.push(a.custom_setup ? "custom setup" : "default setup");
  if ("equal_perf" in a)
    parts.push(a.equal_perf ? "equal car performance" : "real car performance");
  return parts.join(" · ") || "not recorded";
}

function renderSetupCard() {
  const card = $("setup-card");
  $("setup-btn").classList.toggle("on", state.setupOpen);
  if (!state.setupOpen || !state.lapA) { card.style.display = "none"; return; }
  card.style.display = "";
  const A = state.lapA, B = state.lapB;
  const cols = [A, B].filter(Boolean);
  let html = `<div class="card-tag" title="close — same as the SETUP button">SETUP</div><table><tr><th></th>
    <th class="cA">${B ? "YOU" : "LAP"}</th>${B ? '<th class="cB">VS</th>' : ""}</tr>`;
  html += `<tr><td class="rowlbl">Car</td>` + cols.map((l) =>
    `<td>${l.team_name || "—"}</td>`).join("") + "</tr>";
  html += `<tr><td class="rowlbl">Assists</td>` + cols.map((l) =>
    `<td class="asst-cell">${assistLine(l.assists)}</td>`).join("") + "</tr>";
  if (cols.some((l) => l.setup)) {
    for (const [label, fn] of SETUP_ROWS) {
      if (!fn) { html += `<tr><td class="grp" colspan="${1 + cols.length}">${label}</td></tr>`; continue; }
      html += `<tr><td class="rowlbl">${label}</td>` + cols.map((l) =>
        `<td>${l.setup ? fn(l.setup) : "—"}</td>`).join("") + "</tr>";
    }
  } else {
    html += `<tr><td class="grp" colspan="${1 + cols.length}">no setup broadcast for
      ${cols.length > 1 ? "these laps" : "this lap"} (recorded before setup
      capture, or the game hid it)</td></tr>`;
  }
  card.innerHTML = html + "</table>";
}

/* Scrub bar x = lap distance over the same span as the charts, so the
   thumb, the sector tints and the chart cursor line up exactly. */
function scrubMaxD() {
  const A = state.lapA;
  return Math.max(A.maxD, state.lapB ? state.lapB.maxD : 0);
}

function updateScrubTint() {
  const A = state.lapA, el = $("scrub");
  if (!A || !A.secD) { if (el) el.style.background = ""; return; }
  const den = scrubMaxD();
  const p1 = (A.secD[0] / den) * 100, p2 = (A.secD[1] / den) * 100;
  const p3 = (A.maxD / den) * 100;
  let bg =
    `linear-gradient(90deg, rgba(248,113,113,.4) 0 ${p1}%,` +
    ` rgba(96,165,250,.4) ${p1}% ${p2}%, rgba(251,191,36,.4) ${p2}% ${p3}%` +
    (p3 < 99.9 ? `, #1c2331 ${p3}% 100%` : "") + ")";
  if (state.viewD) {   // charts show a zoomed stretch: dim the rest
    const v0 = (state.viewD[0] / den) * 100, v1 = (state.viewD[1] / den) * 100;
    bg = `linear-gradient(90deg, rgba(10,13,18,.72) 0 ${v0}%,` +
         ` transparent ${v0}% ${v1}%, rgba(10,13,18,.72) ${v1}% 100%), ` + bg;
  }
  el.style.background = bg;
}

function seek(t) {
  if (!state.lapA) return;
  state.t = Math.min(Math.max(0, t), state.lapA.duration);
  drawFrame();
}

function drawFrame() {
  const A = state.lapA;
  if (!A) return;
  const s = A.samples, t = state.t;
  drawMap();
  setHalo(
    interp(s.t, s.thr, t), interp(s.t, s.brk, t),
    interp(s.t, s.spd, t), Math.round(interp(s.t, s.gear, t)),
    s.aero ? interp(s.t, s.aero, t) > 0.5 : null,   // pre-capture laps: null
    interp(s.t, s.ot, t) > 0.5,
    interp(s.t, s.str, t), interp(s.t, s.rpm, t));
  drawChartCursors();
  const dA = interp(s.t, s.d, t);
  $("scrub").value = Math.round((dA / scrubMaxD()) * 1000);
  $("time-display").textContent = fmtTime(t, 1);
  cursorReadouts(t, dA);

  const hd = $("hud-delta"), dv = $("cv-delta");
  if (state.lapB) {
    const dSec = (t - interp(state.lapB.samples.d, state.lapB.samples.t,
                             Math.min(dA, state.lapB.maxD))) / 1000;
    hd.textContent = fmtDelta(dSec);
    hd.className = "hud-delta " + (dSec >= 0 ? "pos" : "neg");
    dv.textContent = fmtDelta(dSec);
    dv.className = "cv " + (dSec >= 0 ? "pos" : "neg");
  } else { hd.textContent = ""; dv.textContent = ""; }
}

/* Numeric readouts in the chart label gutter: each trace's value at the
   playhead. Lap A is sampled by time, lap B at the same track distance —
   same convention as the ghost car on the map and the delta chart. */
function cursorReadouts(t, dA) {
  const sA = state.lapA.samples, B = state.lapB;
  const fmt = {
    spd: (v) => String(Math.round(v)),
    thr: (v) => Math.round(v) + "%",
    brk: (v) => Math.round(v) + "%",
    str: (v) => v <= -3 ? "L " + Math.round(-v)
      : v >= 3 ? "R " + Math.round(v) : "0",
  };
  for (const [el, col] of [["speed", "spd"], ["thr", "thr"],
                           ["brk", "brk"], ["steer", "str"]]) {
    $("cvA-" + el).textContent = fmt[col](interp(sA.t, sA[col], t));
    $("cvB-" + el).textContent = B && B.samples[col]
      ? fmt[col](interp(B.samples.d, B.samples[col], Math.min(dA, B.maxD)))
      : "";
  }
  const gA = $("cvA-gear"), gB = $("cvB-gear");
  gA.textContent = sA.gear ? gearName(interp(sA.t, sA.gear, t)) : "—";
  gB.textContent = B && B.samples.gear
    ? gearName(interp(B.samples.d, B.samples.gear, Math.min(dA, B.maxD)))
    : "";
  const esA = $("cvA-ersstore"), esB = $("cvB-ersstore");
  esA.textContent = sA.ers ? Math.round(Math.max(0, Math.min(100, interp(sA.t, sA.ers, t) * 25))) + "%" : "—";
  esB.textContent = B && B.samples.ers
    ? Math.round(Math.max(0, Math.min(100, interp(B.samples.d, B.samples.ers, Math.min(dA, B.maxD)) * 25))) + "%"
    : "";
  const eA = $("cvA-ers"), eB = $("cvB-ers");
  if (sA.ersm) {
    const v = Math.max(0, Math.min(3, Math.round(interp(sA.t, sA.ersm, t))));
    eA.textContent = ERS_MODES[v].name; eA.style.color = ERS_MODES[v].color;
  } else { eA.textContent = "—"; eA.style.color = ""; }
  if (B && B.samples.ersm) {
    const v = Math.max(0, Math.min(3, Math.round(
      interp(B.samples.d, B.samples.ersm, Math.min(dA, B.maxD)))));
    eB.textContent = ERS_MODES[v].name; eB.style.color = ERS_MODES[v].color;
    eB.style.opacity = 0.75;
  } else { eB.textContent = B ? "—" : ""; eB.style.color = ""; }
}

function loop(now) {
  requestAnimationFrame(loop);
  // clamp: rAF throttling in background tabs must not cause time jumps
  const dt = Math.min(100, now - (state.lastFrame || now));
  state.lastFrame = now;
  if (!state.playing || !state.lapA) return;
  state.t += dt * state.speed;
  if (state.t >= state.lapA.duration) {
    state.t = state.lapA.duration;
    state.playing = false; $("btn-play").textContent = "▶";
  }
  drawFrame();
}

/* ---------------------------------------------------------------- controls */

function togglePlay() {
  if (!state.lapA) return;
  if (!state.playing && state.t >= state.lapA.duration) state.t = 0;
  state.playing = !state.playing;
  $("btn-play").textContent = state.playing ? "❚❚" : "▶";
}
$("btn-play").addEventListener("click", togglePlay);
$("speed-select").addEventListener("change", (e) => {
  state.speed = parseFloat(e.target.value);
});
$("scrub").addEventListener("input", (e) => {
  const A = state.lapA;
  if (!A) return;
  const d = Math.min((e.target.value / 1000) * scrubMaxD(), A.maxD);
  seek(interp(A.samples.d, A.samples.t, d));
});
$("track-select").addEventListener("change", async (e) => {
  const id = parseInt(e.target.value, 10);
  if (id !== state.trackId) {   // new track: current lap/ref no longer apply
    state.lapA = state.lapB = null;
    state.mapZoom = null; state.viewD = null;
    state.playing = false;
  }
  await selectTrack(id);
  rebuildScene();
});
$("mode-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.disabled || !state.lapA || b.dataset.mode === state.mode) return;
  state.mode = b.dataset.mode;
  computeLineColors(state.lapA);
  renderMapStatic();
  updateToolbar();
  drawFrame();
});
$("setup-btn").addEventListener("click", () => {
  if (!state.lapA) return;
  state.setupOpen = !state.setupOpen;
  renderSetupCard();
});
$("export-btn").addEventListener("click", () => {
  if (!state.lapA) return;
  const a = document.createElement("a");
  a.href = `api/laps/${state.lapA.id}/export`;   // Content-Disposition names it
  a.download = "";
  document.body.appendChild(a); a.click(); a.remove();
});
$("sort-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b || b.dataset.sort === state.sort) return;
  state.sort = b.dataset.sort;
  for (const x of document.querySelectorAll("#sort-seg button"))
    x.classList.toggle("on", x === b);
  renderLapList();
});
// filter segs have no "ALL" button: clicking the active filter turns it
// off again (none active = everything shown)
$("role-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.roleFilter = b.dataset.role === state.roleFilter ? "all" : b.dataset.role;
  for (const x of document.querySelectorAll("#role-seg button"))
    x.classList.toggle("on", x.dataset.role === state.roleFilter);
  renderLapList();
});
$("asst-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  state.assistFilter = b.dataset.asst === state.assistFilter ? "all" : b.dataset.asst;
  for (const x of document.querySelectorAll("#asst-seg button"))
    x.classList.toggle("on", x.dataset.asst === state.assistFilter);
  renderLapList();
});
$("inv-toggle").addEventListener("click", () => {
  state.hideInvalid = !state.hideInvalid;
  $("inv-toggle").classList.toggle("on", state.hideInvalid);
  renderLapList();
});
$("fold-btn").addEventListener("click", () => {
  const ids = new Set(state.laps.map((l) => l.session_id));
  const allFolded = ids.size > 0 &&
    [...ids].every((id) => state.folded.has(id));
  state.folded = allFolded ? new Set() : ids;
  $("fold-btn").classList.toggle("on", !allFolded);
  renderLapList();
});
$("inv-del").addEventListener("click", async () => {
  if (!confirm("Delete ALL invalid laps — every track, every session, " +
               "not just the ones listed here. This cannot be undone. " +
               "Continue?")) return;
  await api("api/laps/invalid", { method: "DELETE" });
  if (state.lapA && !state.lapA.valid) { state.lapA = null; state.playing = false; }
  if (state.lapB && !state.lapB.valid) state.lapB = null;
  await loadTracks(true);
  rebuildScene();
});
window.addEventListener("keydown", (e) => {
  if (["SELECT", "INPUT", "BUTTON"].includes(e.target.tagName)) return;
  if (e.code === "Space") { e.preventDefault(); togglePlay(); }
  if (e.code === "ArrowRight") seek(state.t + (e.shiftKey ? 5000 : 1000));
  if (e.code === "ArrowLeft") seek(state.t - (e.shiftKey ? 5000 : 1000));
});
/* Collapse the whole lap tray to a thin rail; remembered. */
{
  const setSide = (collapsed) => {
    $("sidebar").classList.toggle("collapsed", collapsed);
    try { localStorage.setItem("f1trace.side", collapsed ? "1" : "0"); }
    catch (e) { /* private mode */ }
    if (state.lapA) rebuildScene();   // stage width changed
  };
  $("side-toggle").addEventListener("click", () => setSide(true));
  $("side-rail").addEventListener("click", () => setSide(false));
  if (localStorage.getItem("f1trace.side") === "1") setSide(true);
}

/* Floating telemetry card: drag anywhere on the stage; steering section
   can be hidden. Both are remembered. */
{
  const hud = $("hud"), stage = $("stage");
  try {
    state.hudPos = JSON.parse(localStorage.getItem("f1trace.hudPos"));
    state.hudOpen = localStorage.getItem("f1trace.hudOpen") !== "0";
    state.hudSteer = localStorage.getItem("f1trace.hudSteer") !== "0";
    state.timingOpen = localStorage.getItem("f1trace.timingOpen") !== "0";
  } catch (e) { /* defaults */ }
  applyHudOpen();
  applyHudSteer();
  applyHudPos();
  let ox = 0, oy = 0, moving = false;
  hud.addEventListener("pointerdown", (e) => {
    if (!state.hudOpen || e.target.closest("#hud-steer-btn, .card-tag")) return;
    moving = true; hud.classList.add("drag");
    hud.setPointerCapture(e.pointerId);
    const r = hud.getBoundingClientRect();
    ox = e.clientX - r.left; oy = e.clientY - r.top;
  });
  hud.addEventListener("pointermove", (e) => {
    if (!moving) return;
    const sr = stage.getBoundingClientRect();
    state.hudPos = { x: (e.clientX - sr.left - ox) / sr.width,
                     y: (e.clientY - sr.top - oy) / sr.height };
    applyHudPos();
  });
  const drop = () => {
    if (!moving) return;
    moving = false; hud.classList.remove("drag");
    try { localStorage.setItem("f1trace.hudPos", JSON.stringify(state.hudPos)); }
    catch (e) { /* private mode */ }
  };
  hud.addEventListener("pointerup", drop);
  hud.addEventListener("pointercancel", drop);
  $("hud-steer-btn").addEventListener("click", () => {
    state.hudSteer = !state.hudSteer;
    try { localStorage.setItem("f1trace.hudSteer", state.hudSteer ? "1" : "0"); }
    catch (e) { /* private mode */ }
    applyHudSteer();
    applyHudPos();   // re-clamp: the card just changed height
  });
  $("hud-tag").addEventListener("click", () => {
    state.hudOpen = !state.hudOpen;
    try { localStorage.setItem("f1trace.hudOpen", state.hudOpen ? "1" : "0"); }
    catch (e) { /* private mode */ }
    applyHudOpen();
    applyHudPos();   // re-clamp: the card just changed size
  });
}

/* Splitter: drag to trade stage height for chart height; remembered. */
{
  const sp = $("splitter"), chartsEl = $("charts"), mainEl = $("main");
  // upper bound keeps the stage usable; the floating TELEMETRY card can
  // be dragged (or its steering section hidden) if space gets tight
  const maxFrac = () =>
    Math.max(0.2, Math.min(0.72, 1 - 460 / mainEl.clientHeight));
  const saved = parseFloat(localStorage.getItem("f1trace.chartsFrac"));
  if (saved >= 0.12 && saved <= 0.72)
    chartsEl.style.height = Math.min(saved, maxFrac()) * 100 + "%";
  let dragging = false, raf = 0;
  sp.addEventListener("pointerdown", (e) => {
    dragging = true; sp.classList.add("drag");
    sp.setPointerCapture(e.pointerId);
  });
  sp.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const r = mainEl.getBoundingClientRect();
    const frac = Math.min(maxFrac(),
      Math.max(0.12, (r.bottom - e.clientY - 6) / r.height));
    chartsEl.style.height = frac * 100 + "%";
    try { localStorage.setItem("f1trace.chartsFrac", frac.toFixed(3)); } catch (err) {}
    if (!raf) raf = requestAnimationFrame(() => {
      raf = 0;
      if (state.lapA) rebuildScene();
    });
  });
  const stop = () => {
    if (!dragging) return;
    dragging = false; sp.classList.remove("drag");
    if (state.lapA) rebuildScene();
  };
  sp.addEventListener("pointerup", stop);
  sp.addEventListener("pointercancel", stop);
}

window.addEventListener("resize", () => { if (state.lapA) rebuildScene(); });
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && state.lapA) rebuildScene();
});

/* ---------------------------------------------------------------- error surface */

function showToast(msg) {
  let t = $("err-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "err-toast";
    t.style.cssText = "position:fixed;bottom:12px;left:50%;transform:translateX(-50%);" +
      "background:#7f1d1d;color:#fecaca;padding:8px 14px;border-radius:8px;" +
      "font-size:12px;z-index:99;max-width:80%;box-shadow:0 4px 14px #0008";
    document.body.appendChild(t);
  }
  t.textContent = "Error: " + msg + " — refresh the page (Cmd+Shift+R); if it persists, report this text.";
}
window.addEventListener("error", (e) => showToast(e.message));
window.addEventListener("unhandledrejection", (e) => showToast(String(e.reason)));

/* ---------------------------------------------------------------- boot */

initHalo();
styleSelect($("track-select"));
styleSelect($("speed-select"));
requestAnimationFrame(loop);
loadTracks(false).catch((e) => showToast("could not load tracks: " + e.message));
pollStatus();
