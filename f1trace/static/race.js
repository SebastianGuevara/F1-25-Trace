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
      <span class="c-name">${CORNERS[i]}</span><span class="c-name c-psi"></span>
      <div class="c-surf"><span class="c-surf-v">–</span><small>SURF</small></div>
      <div class="c-row"><span>CARCASS</span><b class="c-inner">–</b></div>
      <div class="c-row"><span>BRAKE</span><b class="c-brake">–</b></div>
      <div class="c-row"><span>WEAR</span><b class="c-wear-v">–</b></div>
      <div class="c-wear"><i></i></div>`;
  });
}

function paintTyres(ty) {
  const t = TYRES[ty.visual] || { code: "–", name: "TYRE", color: "#94a3b8", kind: "slick" };
  const win = WINDOW[t.kind];
  const comp = $("rv-compound");
  comp.textContent = t.code; comp.style.borderColor = comp.style.color = t.color;
  comp.title = t.name;
  $("rv-age").innerHTML = ty.age != null ? `<b>${ty.age}</b> LAPS OLD` : "";
  document.querySelectorAll(".rv-corner").forEach((el) => {
    const i = +el.dataset.i;
    const surf = ty.surface?.[i], inner = ty.inner?.[i], brake = ty.brake?.[i];
    const wear = ty.wear?.[i], psi = ty.pressure?.[i];
    const sv = el.querySelector(".c-surf-v");
    sv.textContent = surf ? `${surf}°` : "–";
    sv.parentElement.style.color = tempColor(surf, win);
    const ie = el.querySelector(".c-inner");
    ie.textContent = inner ? `${inner}°` : "–"; ie.style.color = tempColor(inner, win);
    const be = el.querySelector(".c-brake");
    be.textContent = brake ? `${brake}°` : "–"; be.style.color = brakeColor(brake);
    el.querySelector(".c-psi").textContent = psi ? `${psi.toFixed(1)} PSI` : "";
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
  set("rv-per-lap", fu.per_lap ? `${fu.per_lap.toFixed(2)} kg` : "after 1 lap");
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
  store.set("f1trace.raceFollow", follow);
  refreshSenders();
});

$("rv-fs").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else document.documentElement.requestFullscreen?.().catch(() => {});
});

buildCorners();
paintDamage(null);
setInterval(tick, 100);
setInterval(refreshSenders, 1500);
refreshSenders().then(tick);
