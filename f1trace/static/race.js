/* RACE page: polls /api/live for one live player and paints the
   race-engineer panels (standing, tyres, fuel/energy, damage, driver).
   All values come from the recorder's `race` snapshot. */
"use strict";

const $ = (id) => document.getElementById(id);
const CORNERS = ["FL", "FR", "RL", "RR"];

// visual compound ids (both the official 7/8 and the ids app.js uses)
const TYRES = {
  16: { code: "S", name: "SOFT", color: "#ef4444", kind: "slick" },
  17: { code: "M", name: "MEDIUM", color: "#eab308", kind: "slick" },
  18: { code: "H", name: "HARD", color: "#f8fafc", kind: "slick" },
  7: { code: "I", name: "INTER", color: "#22c55e", kind: "inter" },
  8: { code: "W", name: "WET", color: "#3b82f6", kind: "wet" },
  19: { code: "I", name: "INTER", color: "#22c55e", kind: "inter" },
  20: { code: "W", name: "WET", color: "#3b82f6", kind: "wet" },
};
// working windows (°C) for tyre surface / carcass by compound family
const WINDOW = { slick: [85, 105], inter: [60, 80], wet: [50, 70] };
const ERS_MODES = [
  { name: "NONE", color: "#6b7280" },
  { name: "MEDIUM", color: "#facc15" },
  { name: "HOTLAP", color: "#fb923c" },
  { name: "BOOST", color: "#c084fc" },
];
const MIX = ["LEAN", "STANDARD", "RICH", "MAX"];
const WEATHER = ["☀️ CLEAR", "🌤 LIGHT CLOUD", "☁️ OVERCAST", "🌦 LIGHT RAIN", "🌧 HEAVY RAIN", "⛈ STORM"];
const BODY_PARTS = [
  ["wing_fl", "FRONT WING L"], ["wing_fr", "FRONT WING R"], ["wing_rear", "REAR WING"],
  ["floor", "FLOOR"], ["diffuser", "DIFFUSER"], ["sidepod", "SIDEPOD"],
  ["gearbox", "GEARBOX"], ["engine", "ENGINE"],
];
const PU_PARTS = [
  ["ice", "ICE"], ["mguh", "MGU-H"], ["mguk", "MGU-K"],
  ["es", "ENERGY STORE"], ["ce", "CONTROL ELEC."], ["tc", "TURBO"],
];

// with the DAMAGE panel hidden, parts at or above this % still show up
// as alerts in the STANDING panel
const DAMAGE_ALERT = 20;

const fmtTime = (ms) => {
  if (!ms) return "–";
  const m = Math.floor(ms / 60000), s = (ms % 60000) / 1000;
  return `${m}:${s.toFixed(3).padStart(6, "0")}`;
};
const fmtGap = (ms) => (ms == null ? "–" : ms >= 60000 ? fmtTime(ms) : `+${(ms / 1000).toFixed(3)}`);
const set = (id, v) => { const e = $(id); if (e.textContent !== String(v)) e.textContent = v; };

/* cold → blue, under the window → cyan, in it → green, above → amber / red */
function tempColor(v, [lo, hi]) {
  if (v == null || v === 0) return "#4a5568";
  if (v < lo - 20) return "#3b82f6";
  if (v < lo) return "#22d3ee";
  if (v <= hi) return "#34d399";
  if (v <= hi + 10) return "#fbbf24";
  return "#f87171";
}
const brakeColor = (v) => tempColor(v, [300, 950]);
const wearColor = (v) => (v < 25 ? "#34d399" : v < 45 ? "#facc15" : v < 65 ? "#fb923c" : "#f87171");

/* ---------------------------------------------------------------- corners */

function buildCorners() {
  document.querySelectorAll(".rv-corner").forEach((el) => {
    const i = +el.dataset.i;
    el.innerHTML = `
      <div class="c-head"><span class="c-name">${CORNERS[i]}</span><span class="c-psi">–</span></div>
      <div class="c-surf"><span class="c-surf-v">–</span><small>SURF</small></div>
      <div class="c-stats">
        <div><span>CARCASS</span><b class="c-inner">–</b></div>
        <div><span>BRAKE</span><b class="c-brake">–</b></div>
        <div><span>WEAR</span><b class="c-wear-v">–</b></div>
      </div>
      <div class="c-wear"><i></i></div>`;
  });
}

function paintTyres(ty) {
  const t = TYRES[ty.visual] || { code: "–", name: "TYRE", color: "#94a3b8", kind: "slick" };
  const win = WINDOW[t.kind];
  const comp = $("rv-compound");
  comp.textContent = t.code; comp.style.borderColor = comp.style.color = t.color;
  comp.title = t.name;
  $("rv-age").innerHTML = ty.age != null ? `<b>${ty.age}</b>LAPS OLD` : "";
  document.querySelectorAll(".rv-corner").forEach((el) => {
    const i = +el.dataset.i;
    const surf = ty.surface?.[i], inner = ty.inner?.[i], brake = ty.brake?.[i];
    const wear = ty.wear?.[i], psi = ty.pressure?.[i];
    const sv = el.querySelector(".c-surf-v");
    sv.textContent = surf ? `${surf}°` : "–";
    el.style.setProperty("--tc", tempColor(surf, win));
    const ie = el.querySelector(".c-inner");
    ie.textContent = inner ? `${inner}°` : "–"; ie.style.color = tempColor(inner, win);
    const be = el.querySelector(".c-brake");
    be.textContent = brake ? `${brake}°` : "–"; be.style.color = brakeColor(brake);
    el.querySelector(".c-psi").innerHTML = psi ? `${psi.toFixed(1)}<small>PSI</small>` : "–";
    const we = el.querySelector(".c-wear-v"), bar = el.querySelector(".c-wear i");
    if (wear != null) {
      we.textContent = `${wear.toFixed(0)}%`; we.style.color = wearColor(wear);
      bar.style.width = Math.min(100, wear) + "%"; bar.style.background = wearColor(wear);
    } else { we.textContent = "–"; bar.style.width = "0"; }
    $("rv-w" + i).style.fill = tempColor(surf, win);
  });
}

/* ---------------------------------------------------------------- damage */

function dmgRows(parts, d) {
  return parts.map(([k, label]) => {
    const v = d?.[k] ?? null;
    const col = v == null ? "#4a5568" : wearColor(v);
    return `<div class="d-row"><span>${label}</span><b style="color:${col}">${v == null ? "–" : v + "%"}</b>` +
      `<div class="d-bar"><i style="width:${v || 0}%;background:${col}"></i></div></div>`;
  }).join("");
}

function paintDamage(d) {
  $("rv-dmg-body").innerHTML = "<h4>BODY</h4>" + dmgRows(BODY_PARTS, d);
  $("rv-dmg-pu").innerHTML = "<h4>POWER UNIT</h4>" + dmgRows(PU_PARTS, d);
  const f = [];
  if (d?.drs_fault) f.push('<span class="rv-tag bad">DRS FAULT</span>');
  if (d?.ers_fault) f.push('<span class="rv-tag bad">ERS FAULT</span>');
  if (d?.blown) f.push('<span class="rv-tag bad">ENGINE BLOWN</span>');
  if (d?.seized) f.push('<span class="rv-tag bad">ENGINE SEIZED</span>');
  if (!d) f.push('<span class="rv-tag">NO DAMAGE DATA YET</span>');
  $("rv-faults").innerHTML = f.join("");
}

/* ---------------------------------------------------------------- paint */

function paint(live) {
  const r = live.race || {};
  set("rv-pos", r.position || "–");
  set("rv-of", r.num_cars ? `/${r.num_cars}` : "");
  set("rv-lap", r.total_laps ? `${r.lap_num}/${r.total_laps}` : (r.lap_num || "–"));
  set("rv-laps-left", r.total_laps ? `${Math.max(0, r.total_laps - r.lap_num + 1)} TO GO` : (r.session_type || ""));
  set("rv-gap-front", r.position === 1 ? "LEADER" : fmtGap(r.gap_front_ms));
  set("rv-gap-behind", fmtGap(r.gap_behind_ms));
  set("rv-gap-leader", r.position === 1 ? "—" : fmtGap(r.gap_leader_ms));
  set("rv-cur", fmtTime(live.lap_time_ms));
  set("rv-last", fmtTime(r.last_lap_ms));
  set("rv-best", fmtTime(r.best_lap_ms));

  const tags = [];
  if (r.session_type) tags.push(`<span class="rv-tag">${r.session_type.toUpperCase()}</span>`);
  if (r.weather != null && WEATHER[r.weather]) tags.push(`<span class="rv-tag">${WEATHER[r.weather]}</span>`);
  if (r.grid) tags.push(`<span class="rv-tag">GRID P${r.grid}</span>`);
  tags.push(`<span class="rv-tag">PITS ${r.pit_stops || 0}</span>`);
  if (r.penalties_s) tags.push(`<span class="rv-tag bad">+${r.penalties_s}s PEN</span>`);
  if (r.warnings) tags.push(`<span class="rv-tag warn">${r.warnings} WARN</span>`);
  if (live.invalid) tags.push('<span class="rv-tag bad">LAP INVALID</span>');
  if ($("rv-damage").classList.contains("off") && r.damage) {
    for (const [k, label] of BODY_PARTS.concat(PU_PARTS))
      if (r.damage[k] >= DAMAGE_ALERT)
        tags.push(`<span class="rv-tag ${r.damage[k] >= 50 ? "bad" : "warn"}">⚠ ${label} ${r.damage[k]}%</span>`);
    if (r.damage.drs_fault) tags.push('<span class="rv-tag bad">⚠ DRS FAULT</span>');
    if (r.damage.ers_fault) tags.push('<span class="rv-tag bad">⚠ ERS FAULT</span>');
  }
  $("rv-tags").innerHTML = tags.join("");

  const flag = $("rv-flag");
  const sc = { 1: "SAFETY CAR", 2: "VIRTUAL SAFETY CAR", 3: "FORMATION LAP" }[r.safety_car];
  const pit = r.pit_status ? (r.pit_status === 1 ? "PITTING" : "IN PIT LANE") : (r.pit_limiter ? "PIT LIMITER" : "");
  flag.style.display = sc || pit ? "" : "none";
  flag.className = "rv-flag " + (sc ? "sc" : "pit");
  flag.textContent = sc || pit;

  if (r.tyres) paintTyres(r.tyres);

  const fu = r.fuel || {};
  set("rv-fuel-kg", fu.kg != null ? fu.kg.toFixed(1) : "–");
  $("rv-fuel-bar").style.width = fu.kg && fu.capacity ? Math.min(100, fu.kg / fu.capacity * 100) + "%" : "0";
  const sp = $("rv-surplus");
  if (fu.laps_surplus != null) {
    sp.textContent = `${fu.laps_surplus >= 0 ? "+" : ""}${fu.laps_surplus.toFixed(2)} LAPS`;
    sp.className = "rv-surplus " + (fu.laps_surplus >= 0 ? "rv-good" : "rv-bad");
  } else sp.textContent = "–";
  set("rv-per-lap", fu.per_lap ? `${fu.per_lap.toFixed(2)} kg` : "–");
  set("rv-in-tank", fu.laps_left != null ? fu.laps_left.toFixed(1) : "–");
  set("rv-mix", MIX[fu.mix] || "–");

  set("rv-ers", r.ers_pct != null ? r.ers_pct.toFixed(0) : "–");
  $("rv-ers-bar").style.width = Math.min(100, r.ers_pct || 0) + "%";
  const m = ERS_MODES[r.ers_mode] || ERS_MODES[0], me = $("rv-ers-mode");
  me.textContent = m.name; me.style.color = m.color;
  set("rv-engine", r.engine_temp ? `${r.engine_temp}°C` : "–");
  set("rv-bias", r.brake_bias ? `${r.brake_bias}%` : "–");
  set("rv-temps", r.track_temp != null ? `${r.track_temp}° / ${r.air_temp}°` : "–");

  paintDamage(r.damage);

  set("rv-speed", live.speed || 0);
  set("rv-gear", live.gear > 0 ? live.gear : live.gear < 0 ? "R" : "N");
  $("rv-thr").style.setProperty("--v", (live.throttle || 0) + "%");
  $("rv-brk").style.setProperty("--v", (live.brake || 0) + "%");
  $("rv-rpm").style.width = Math.min(100, (live.rpm || 0) / 130) + "%";
  set("rv-rpm-txt", `${live.rpm || 0} RPM`);

  $("rv-who").innerHTML = `<b>${esc(live.driver_name || "")}</b> · ${esc(live.track_name || "")}` +
    (live.team_name ? ` · ${esc(live.team_name)}` : "");
}

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);

/* ---------------------------------------------------------------- polling */

/* Follow exactly one driver, by name: the one picked in the selector,
   else this browser's own driver (set on the main page), else the only
   one on track. Never "most recent sender" — with several players live
   that alternates between them on every poll and mixes their data. */
const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};
let follow = new URLSearchParams(location.search).get("driver")
  || store.get("f1trace.raceFollow") || store.get("f1trace.myDriver") || "";
let sender = "";          // ip:port of the followed driver, resolved from live_all
// the radio keeps talking to the last sender seen even when telemetry
// stops (garage, menus); it only changes when another driver is picked
let radioTarget = "";
let inFlight = false, lastOk = 0, waitMsg = "";
const same = (a, b) => (a || "").trim().toLowerCase() === (b || "").trim().toLowerCase();

async function tick() {
  if (inFlight) return;
  if (sender) {
    inFlight = true;
    try {
      const live = await fetch("/api/live?sender=" + encodeURIComponent(sender),
                               { cache: "no-store" }).then((r) => r.json());
      if (live.active && live.sender === sender) {
        lastOk = Date.now();
        paint(live);
      }
    } catch (e) { /* server restarting: keep the last frame */ }
    inFlight = false;
  }
  const fresh = !!sender && Date.now() - lastOk < 3000;
  document.body.classList.toggle("stale", !fresh && lastOk > 0);
  $("rv-wait").classList.toggle("hide", fresh);
  const chip = $("rv-chip");
  chip.className = "chip " + (fresh ? "live" : "idle");
  chip.textContent = fresh ? "LIVE" : "NO SIGNAL";
}

async function refreshSenders() {
  let all;
  try {
    all = await fetch("/api/live_all", { cache: "no-store" }).then((r) => r.json());
  } catch (e) { return; }
  // one entry per live connection, keyed by driver name; a name shared by
  // two connections (same public IP) is qualified with its ip:port
  const count = {};
  all.forEach((p) => { const k = (p.driver_name || "").toLowerCase(); count[k] = (count[k] || 0) + 1; });
  const entries = all.map((p) => {
    const name = p.driver_name || p.sender;
    const dup = count[(p.driver_name || "").toLowerCase()] > 1;
    return { key: dup ? `${name}@${p.sender}` : name,
             label: name.toUpperCase() + (dup ? ` · ${p.sender.split(":").pop()}` : ""),
             sender: p.sender, name };
  });
  const matches = (e) => (follow.includes("@") ? e.key === follow : same(e.name, follow));
  let pick = follow ? entries.find(matches) : null;
  if (!follow && entries.length === 1) pick = entries[0];
  if (follow && !pick)
    entries.push({ key: follow, label: follow.split("@")[0].toUpperCase() + " (OFF TRACK)" });

  const sel = $("rv-sender");
  sel.style.display = entries.length ? "" : "none";
  const html = (follow ? "" : '<option value="">CHOOSE DRIVER…</option>') +
    entries.map((e) => `<option value="${esc(e.key)}">${esc(e.label)}</option>`).join("");
  if (sel.dataset.html !== html) { sel.innerHTML = html; sel.dataset.html = html; }
  sel.value = pick ? pick.key : (follow || "");

  const next = pick ? pick.sender : "";
  if (next !== sender) { sender = next; lastOk = 0; document.body.classList.remove("stale"); }
  if (next) radioTarget = next;

  const h = $("rv-wait").querySelector("h2"), p = $("rv-wait").querySelector("p");
  if (!follow && all.length > 1) {
    h.textContent = "Choose whose telemetry to follow";
    p.textContent = "Several drivers are on track — pick one in the selector at the top.";
  } else if (follow && !pick) {
    h.textContent = `Waiting for ${follow.split("@")[0].toUpperCase()}`;
    p.textContent = "Not on track right now — this page fills in as soon as they are.";
  } else {
    h.textContent = "Waiting for live telemetry";
    p.textContent = "Get on track with UDP telemetry on — this page fills in by itself.";
  }
}
$("rv-sender").addEventListener("change", (e) => {
  follow = e.target.value;
  radioTarget = "";
  store.set("f1trace.raceFollow", follow);
  refreshSenders();
});

$("rv-fs").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.().catch(() => {});
});

/* panel switches: which panels show, remembered; damage and driver are
   off by default so tyres, fuel and standing get the room */
const PANELS = ["rv-standing", "rv-tyres", "rv-fuel", "rv-damage", "rv-drive"];
let shown;
try { shown = JSON.parse(store.get("f1trace.racePanels")); } catch (e) { shown = null; }
if (!Array.isArray(shown) || !shown.length) shown = ["rv-standing", "rv-tyres", "rv-fuel"];

function applyPanels() {
  const grid = $("rv-grid");
  PANELS.forEach((id) => $(id).classList.toggle("off", !shown.includes(id)));
  document.querySelectorAll("#rv-panels button").forEach((b) =>
    b.classList.toggle("on", shown.includes(b.dataset.panel)));
  const others = shown.filter((id) => id !== "rv-tyres").length;
  const tyres = shown.includes("rv-tyres");
  grid.classList.toggle("no-tyres", !tyres);
  grid.classList.toggle("solo", tyres && !others);
  // 16:9 grid: tyres span every row of the stacked right-hand column
  $("rv-tyres").style.gridRow = `1 / span ${Math.max(1, others)}`;
}
$("rv-panels").addEventListener("click", (e) => {
  const id = e.target.closest("button")?.dataset.panel;
  if (!id) return;
  if (shown.includes(id)) { if (shown.length > 1) shown = shown.filter((x) => x !== id); }
  else shown = PANELS.filter((x) => x === id || shown.includes(x));
  store.set("f1trace.racePanels", JSON.stringify(shown));
  applyPanels();
});

/* race engineer radio: the server writes the calls (/api/radio), this
   page speaks them — ElevenLabs mp3 when the server has a key, else the
   browser's own Spanish voice. Subtitles show either way. */
let radioOn = store.get("f1trace.radio") === "1";
let radioSince = -1, radioSender = "", radioBusy = false, subTimer = 0;
const radioQueue = [];
let audioCtxUnlocked = false;

function paintRadioBtn() {
  const b = $("rv-radio");
  b.classList.toggle("on", radioOn);
  b.textContent = radioOn ? "📻 RADIO ON" : "📻 RADIO";
}
function showSub(m, me = 0) {
  $("rv-sub-txt").textContent = m.text;
  const el = $("rv-sub");
  el.classList.toggle("p3", m.prio >= 3);
  el.classList.toggle("me", !!me);
  el.classList.toggle("miss", me === 2);
  el.querySelector("b").textContent = me ? "🎙" : "📻";
  el.classList.add("show");
  clearTimeout(subTimer);
  subTimer = setTimeout(() => el.classList.remove("show"), 4000 + m.text.length * 60);
}
function speakBrowser(text) {
  return new Promise((done) => {
    if (!window.speechSynthesis) return done();
    const u = new SpeechSynthesisUtterance(text);
    u.lang = "es-ES"; u.rate = 0.95;
    const v = speechSynthesis.getVoices().find((v) => v.lang.startsWith("es"));
    if (v) u.voice = v;
    u.onend = u.onerror = () => done();
    speechSynthesis.speak(u);
  });
}
/* F1-style team radio, all synthesised: a squelch burst and a double
   beep open each call, the voice goes through a band-limited "radio"
   filter with a faint hiss, and a short squelch closes it. */
let fx = null;
const wait = (ms) => new Promise((r) => setTimeout(r, ms));
function fxCtx() {
  fx = fx || new AudioContext();
  if (fx.state === "suspended") fx.resume();
  return fx;
}
function noise(ctx, secs) {
  const b = ctx.createBuffer(1, Math.ceil(ctx.sampleRate * secs), ctx.sampleRate);
  const d = b.getChannelData(0);
  for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
  const s = ctx.createBufferSource();
  s.buffer = b;
  return s;
}
function filt(ctx, type, f, q = 0.7, gain = 0) {
  const b = ctx.createBiquadFilter();
  b.type = type; b.frequency.value = f; b.Q.value = q; b.gain.value = gain;
  return b;
}
function env(ctx, t, peak, dur) {
  const g = ctx.createGain();
  g.gain.setValueAtTime(0.0001, t);
  g.gain.exponentialRampToValueAtTime(peak, t + 0.008);
  g.gain.setValueAtTime(peak, t + dur * 0.6);
  g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
  return g;
}
function squelch(ctx, t, dur, vol) {
  const n = noise(ctx, dur);
  n.connect(filt(ctx, "bandpass", 2400, 0.9)).connect(env(ctx, t, vol, dur)).connect(ctx.destination);
  n.start(t); n.stop(t + dur);
}
function tone(ctx, t, f, dur, vol) {
  const o = ctx.createOscillator();
  o.type = "triangle"; o.frequency.value = f;
  o.connect(env(ctx, t, vol, dur)).connect(ctx.destination);
  o.start(t); o.stop(t + dur);
}
/* 1 = incoming call, 2 = "I'm listening" chirp, 3 = end of call.
   Returns how long it lasts (s). */
function beep(kind) {
  try {
    const ctx = fxCtx(), t = ctx.currentTime + 0.02;
    if (kind === 2) { tone(ctx, t, 880, 0.06, 0.2); tone(ctx, t + 0.075, 1320, 0.09, 0.2); return 0.18; }
    if (kind === 3) { squelch(ctx, t, 0.14, 0.12); return 0.14; }
    squelch(ctx, t, 0.08, 0.16);
    tone(ctx, t + 0.07, 1250, 0.075, 0.18);
    tone(ctx, t + 0.17, 1250, 0.075, 0.18);
    return 0.27;
  } catch (e) { return 0; }
}
function shaper(ctx, k) {
  const ws = ctx.createWaveShaper(), c = new Float32Array(1024);
  for (let i = 0; i < c.length; i++) { const x = (i / 511.5) - 1; c[i] = ((1 + k) * x) / (1 + k * Math.abs(x)); }
  ws.curve = c;
  return ws;
}
async function playRadio(blob) {
  const ctx = fxCtx();
  if (ctx.state !== "running") throw new Error("audio locked");
  const buf = await ctx.decodeAudioData(await blob.arrayBuffer());
  const src = ctx.createBufferSource();
  src.buffer = buf;
  const comp = ctx.createDynamicsCompressor();
  src.connect(filt(ctx, "highpass", 320)).connect(filt(ctx, "peaking", 1800, 1, 5))
    .connect(filt(ctx, "lowpass", 3600)).connect(shaper(ctx, 2.5)).connect(comp)
    .connect(ctx.destination);
  const hiss = noise(ctx, buf.duration + 0.1), hg = ctx.createGain();
  hg.gain.value = 0.012;
  hiss.connect(filt(ctx, "bandpass", 3000, 0.6)).connect(hg).connect(ctx.destination);
  const t = ctx.currentTime + 0.02;
  hiss.start(t); src.start(t);
  await new Promise((done) => {
    src.onended = done;
    setTimeout(done, (buf.duration + 1) * 1000);   // never hang the queue
  });
  try { hiss.stop(); } catch (e) { /* already stopped */ }
}
async function speak(m, tts) {
  if (tts === "elevenlabs") {
    try {
      const r = await fetch(`/api/radio/audio?id=${m.id}`, { cache: "no-store" });
      if (r.ok) {
        const blob = await r.blob();
        try { await playRadio(blob); } catch (e) {
          const url = URL.createObjectURL(blob), a = new Audio(url);
          await new Promise((done) => { a.onended = a.onerror = done; a.play().catch(done); });
          URL.revokeObjectURL(url);
        }
        return;
      }
    } catch (e) { /* fall back to the browser voice */ }
  }
  await speakBrowser(m.text);
}
async function playQueue() {
  if (radioBusy) return;
  radioBusy = true;
  while (radioQueue.length) {
    const { m, tts } = radioQueue.shift();
    // stale low-priority calls aren't worth hearing late
    if (Date.now() / 1000 - m.t > (m.prio >= 2 ? 25 : 12)) continue;
    showSub(m);
    if (radioOn) {
      $("rv-radio").classList.add("talking");
      engineerTalking = true;
      await wait(beep(1) * 1000 + 60);
      await speak(m, tts);
      beep(3);
      $("rv-radio").classList.remove("talking");
      setTimeout(() => {
        engineerTalking = false;
        // after an answer the driver can follow up without the wake word
        if (m.reply && mic) listenFor(7000);
      }, 400);
      await new Promise((r) => setTimeout(r, 350));
    }
  }
  radioBusy = false;
}
async function pollRadio() {
  if (!radioTarget) return;
  if (radioTarget !== radioSender) { radioSender = radioTarget; radioSince = -1; }
  try {
    const q = `sender=${encodeURIComponent(radioTarget)}&since=${Math.max(radioSince, 0)}`;
    const res = await fetch(`/api/radio?${q}`, { cache: "no-store" }).then((r) => r.json());
    if (radioSince < 0) { radioSince = res.last_id; return; }   // skip old calls
    for (const m of res.messages) {
      radioSince = Math.max(radioSince, m.id);
      radioQueue.push({ m, tts: res.tts });
    }
    if (radioQueue.length) playQueue();
  } catch (e) { /* server restarting */ }
}
$("rv-radio").addEventListener("click", () => {
  radioOn = !radioOn;
  store.set("f1trace.radio", radioOn ? "1" : "0");
  paintRadioBtn();
  if (radioOn) {
    // this click is the user gesture browsers need before audio can play
    fxCtx();
    if (!audioCtxUnlocked && window.speechSynthesis) {
      speechSynthesis.speak(new SpeechSynthesisUtterance(""));
      audioCtxUnlocked = true;
    }
    if (radioTarget) fetch(`/api/radio/check?sender=${encodeURIComponent(radioTarget)}`, { method: "POST" });
  } else {
    radioQueue.length = 0;
    window.speechSynthesis?.cancel();
  }
});
paintRadioBtn();
setInterval(pollRadio, 400);
// browsers keep audio locked until the first touch/click on the page
addEventListener("pointerdown", () => { if (radioOn) fxCtx(); }, { once: true });

/* hands-free talkback: the mic stays open; a simple voice-activity
   detector cuts each phrase (with a little pre-roll so the first word
   isn't clipped) and posts it as 16 kHz WAV. The server transcribes it
   and only answers phrases that start with a wake word ("radio…").
   The mic is ignored while the engineer is talking. */
let engineerTalking = false;
let followUntil = 0;      // until then, phrases need no wake word

function listenFor(ms) {
  followUntil = Date.now() + ms;
  $("rv-mic").classList.add("follow");
  setTimeout(() => { if (Date.now() >= followUntil) $("rv-mic").classList.remove("follow"); }, ms + 50);
}
let wakeLabel = "radio";
fetch("/api/status").then((r) => r.json()).then((st) => {
  if (st.radio?.wake) {
    wakeLabel = st.radio.wake;
    $("rv-mic").title = `talk to the engineer hands-free: start with «${wakeLabel}» (needs HTTPS)`;
  }
}).catch(() => {});
let mic = null;
let micOn = store.get("f1trace.mic") === "1";
const vad = { floor: 0.004, loud: 0, quiet: 0, speaking: false, pre: [], frames: [], voiced: 0 };
const FRAME = 1024, RATE = 16000;

function paintMicBtn() {
  const b = $("rv-mic");
  b.classList.toggle("on", micOn && !!mic);
  b.textContent = micOn && mic ? "🎙 MIC ON" : "🎙 MIC";
}
function wav(frames) {
  const n = frames.reduce((a, f) => a + f.length, 0);
  const buf = new ArrayBuffer(44 + n * 2), v = new DataView(buf);
  const str = (o, t) => [...t].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)));
  str(0, "RIFF"); v.setUint32(4, 36 + n * 2, true); str(8, "WAVE"); str(12, "fmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, RATE, true); v.setUint32(28, RATE * 2, true);
  v.setUint16(32, 2, true); v.setUint16(34, 16, true); str(36, "data"); v.setUint32(40, n * 2, true);
  let o = 44;
  for (const f of frames) for (let i = 0; i < f.length; i++, o += 2)
    v.setInt16(o, Math.max(-1, Math.min(1, f[i])) * 0x7fff, true);
  return new Blob([buf], { type: "audio/wav" });
}
async function sendPhrase(frames, retry = true) {
  if (!radioTarget) return;
  const headers = { "Content-Type": "audio/wav" };
  const tok = store.get("f1trace.radioToken");
  if (tok) headers["X-Radio-Token"] = tok;
  try {
    const follow = Date.now() < followUntil;
    if (follow) followUntil = Date.now() + 15000;   // keep it open while we wait
    showSub({ text: "…", prio: 0 }, 2);
    const r = await fetch(`/api/radio/ask?sender=${encodeURIComponent(radioTarget)}&wake=${follow ? 0 : 1}`,
      { method: "POST", headers, body: wav(frames) });
    if (r.status === 401 && retry) {
      const t = prompt("Radio token (RADIO_TOKEN in the server's .env):");
      if (t) { store.set("f1trace.radioToken", t.trim()); return sendPhrase(frames, false); }
      return;
    }
    const res = await r.json();
    if (res.error && r.status !== 409) showSub({ text: "Radio: " + res.error, prio: 3 }, 2);
    else if (res.wake_only) {
      engineerTalking = true;               // don't hear our own chirp
      setTimeout(() => { engineerTalking = false; }, beep(2) * 1000 + 150);
      listenFor(8000);
      showSub({ text: "Te escucho…", prio: 0 }, 1);
    } else if (res.heard) {
      showSub({ text: res.heard, prio: 0 }, res.accepted ? 1 : 2);
      if (res.accepted) followUntil = 0;   // answer on its way
      else if (follow) followUntil = 0;
    } else if (follow) followUntil = 0;
    if (!followUntil) $("rv-mic").classList.remove("follow");
  } catch (e) { /* network hiccup: drop the phrase */ }
}
function onFrame(x) {
  const f = new Float32Array(x);
  let sum = 0;
  for (let i = 0; i < f.length; i++) sum += f[i] * f[i];
  const rms = Math.sqrt(sum / f.length);
  if (engineerTalking) {           // don't transcribe our own radio
    vad.speaking = false; vad.pre = []; vad.loud = 0;
    $("rv-mic").classList.remove("hearing");
    return;
  }
  const th = Math.max(vad.floor * 3.5, 0.012);
  if (!vad.speaking) {
    vad.floor = Math.max(0.002, vad.floor * 0.97 + rms * 0.03);
    vad.pre.push(f);
    if (vad.pre.length > 6) vad.pre.shift();
    vad.loud = rms > th ? vad.loud + 1 : 0;
    if (vad.loud >= 3) {
      vad.speaking = true; vad.frames = vad.pre.slice(); vad.quiet = 0; vad.voiced = vad.loud;
      $("rv-mic").classList.add("hearing");
    }
    return;
  }
  vad.frames.push(f);
  if (rms < th * 0.7) vad.quiet++; else { vad.quiet = 0; vad.voiced++; }
  const tooLong = vad.frames.length > (RATE * 12) / FRAME;
  if (vad.quiet >= 15 || tooLong) {       // ~0.95 s of silence ends a phrase
    const frames = vad.frames, voiced = vad.voiced;
    vad.speaking = false; vad.pre = []; vad.frames = []; vad.loud = 0;
    $("rv-mic").classList.remove("hearing");
    if (voiced >= 4) sendPhrase(frames);  // skip clicks; a lone "bro" is ~4
  }
}
async function startMic() {
  if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
    showSub({ text: "El micrófono necesita HTTPS: abre https://pitwallsim.site/race.html", prio: 3 }, 2);
    return false;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    const ctx = new AudioContext({ sampleRate: RATE });
    const src = ctx.createMediaStreamSource(stream);
    const proc = ctx.createScriptProcessor(FRAME, 1, 1);
    proc.onaudioprocess = (e) => onFrame(e.inputBuffer.getChannelData(0));
    src.connect(proc);
    proc.connect(ctx.destination);   // output stays silent; needed to run
    if (ctx.state === "suspended") {
      showSub({ text: "Toca la pantalla para abrir el micrófono", prio: 2 }, 2);
      const go = () => { ctx.resume(); removeEventListener("pointerdown", go); };
      addEventListener("pointerdown", go);
    }
    mic = { stream, ctx, proc, src };
    return true;
  } catch (e) {
    showSub({ text: "No pude abrir el micrófono: " + (e.message || e.name), prio: 3 }, 2);
    return false;
  }
}
function stopMic() {
  if (!mic) return;
  mic.proc.disconnect(); mic.src.disconnect();
  mic.stream.getTracks().forEach((t) => t.stop());
  mic.ctx.close();
  mic = null;
}
$("rv-mic").addEventListener("click", async () => {
  if (micOn && mic) { micOn = false; stopMic(); }
  else {
    micOn = await startMic();
    if (micOn) {
      if (!radioOn) $("rv-radio").click();   // answers come over the radio
      const w = (wakeLabel.split(",")[0] || "radio").trim();
      const W = w.charAt(0).toUpperCase() + w.slice(1);
      showSub({ text: `Micrófono abierto. Empieza con «${w}», por ejemplo: «${W}, ¿cómo van las gomas?»`, prio: 1 }, 1);
    }
  }
  store.set("f1trace.mic", micOn ? "1" : "0");
  paintMicBtn();
});
if (micOn) startMic().then((ok) => { micOn = ok; paintMicBtn(); });
paintMicBtn();

applyPanels();
buildCorners();
paintDamage(null);
setInterval(tick, 100);
setInterval(refreshSenders, 1500);
refreshSenders().then(tick);
