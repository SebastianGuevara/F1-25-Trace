"""Race engineer: watches each live player's `race` snapshot and speaks up
(in Spanish) when something worth a radio call happens — gaps, fuel, tyre
temperatures and wear, damage, flags, penalties, weather.

Calls are plain rules over the telemetry, so they're instant and free;
only the voice (ElevenLabs, optional) costs anything. The RACE page polls
/api/radio for new calls and plays them; without an ElevenLabs key it
falls back to the browser's own speech synthesis.

Stdlib only. Keys come from the environment or a `.env` file at the repo
root (see `load_env`); they never leave the server.
"""

import collections
import json
import os
import threading
import time
import urllib.error
import urllib.request

RACE_SESSIONS = {15, 16, 17}
# tyre surface working window (°C) by visual compound family
WINDOW = {"slick": (85, 105), "inter": (60, 80), "wet": (50, 70)}
VISUAL_KIND = {16: "slick", 17: "slick", 18: "slick",
               7: "inter", 19: "inter", 8: "wet", 20: "wet"}
CORNER_ES = ["delantero izquierdo", "delantero derecho",
             "trasero izquierdo", "trasero derecho"]
PARTS_ES = {
    "wing_fl": "alerón delantero izquierdo",
    "wing_fr": "alerón delantero derecho",
    "wing_rear": "alerón trasero", "floor": "fondo plano",
    "diffuser": "difusor", "sidepod": "pontón",
}
WEAR_STEPS = (40, 60, 75)
SPEAK_EVERY_LAPS = 3       # race summary cadence


def load_env(path):
    """KEY=value lines into os.environ (existing variables win)."""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip().removeprefix("export ").strip()
                os.environ.setdefault(k, v.strip().strip("'\""))
    except OSError:
        pass


# ------------------------------------------------------------ phrasing

def gap_es(ms):
    """Gap the way an engineer says it: tenths under a second."""
    if ms is None:
        return "?"
    if ms < 1000:
        d = round(ms / 100)
        if d <= 0:
            return "menos de una décima"
        return "una décima" if d == 1 else "%d décimas" % d
    return "%s segundos" % ("%.1f" % (ms / 1000)).replace(".", ",")


def laptime_es(ms):
    m, s = divmod(ms / 1000, 60)
    return "%d %s" % (m, ("%04.1f" % s).replace(".", ","))


def num_es(x, nd=1):
    return ("%.*f" % (nd, x)).replace(".", ",")


class _Sender:
    def __init__(self):
        self.prev = None          # previous race snapshot
        self.last_obs = 0.0
        self.cooldown = {}        # key -> time it may speak again
        self.since = {}           # condition key -> time it started
        self.pos_seen = None      # (position, since)
        self.pos_said = None
        self.fuel_state = None
        self.fuel_said_lap = 0
        self.wear_step = 0
        self.dmg_said = {}
        self.listened = 0.0       # last time a browser polled this sender
        self.session = None


class RaceEngineer:
    def __init__(self):
        self._lock = threading.Lock()
        self._senders = {}
        self._msgs = collections.deque(maxlen=200)   # (id, sender, t, text, prio)
        self._next_id = 1
        self._audio = collections.OrderedDict()      # msg id -> mp3 bytes
        self._audio_events = {}
        self.el_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.el_voice = (os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
                         or "pNInz6obpgDQGcFmaJgB")
        self.el_model = (os.environ.get("ELEVENLABS_MODEL", "").strip()
                         or "eleven_flash_v2_5")
        self.last_tts_error = None

    # -------------------------------------------------------- public

    def info(self):
        return {"tts": "elevenlabs" if self.el_key else "browser",
                "tts_error": self.last_tts_error}

    def messages(self, sender, since):
        """Calls for one sender newer than `since`; marks it as listened."""
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
            st.listened = time.time()
            last = self._next_id - 1
            out = [{"id": i, "t": t, "text": tx, "prio": p}
                   for (i, s, t, tx, p) in self._msgs
                   if s == sender and i > since]
        return {"last_id": last, "messages": out,
                "tts": "elevenlabs" if self.el_key else "browser"}

    def radio_check(self, sender, live):
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
            st.listened = time.time()
            if time.time() < st.cooldown.get("radio_check", 0):
                return False
            st.cooldown["radio_check"] = time.time() + 5
        r = (live or {}).get("race") or {}
        bits = ["Radio check, te escucho fuerte y claro."]
        if r.get("position"):
            bits.append("Vas P%d" % r["position"])
            if r.get("total_laps") and r.get("lap_num"):
                bits[-1] += ", vuelta %d de %d" % (r["lap_num"], r["total_laps"])
            bits[-1] += "."
        self._say(sender, " ".join(bits), 2)
        return True

    def audio(self, msg_id):
        """mp3 bytes for one call (synthesised on demand, cached)."""
        if not self.el_key:
            return None
        with self._lock:
            if msg_id in self._audio:
                return self._audio[msg_id]
            ev = self._audio_events.get(msg_id)
            # only fresh calls: old ones are never played, so don't pay for them
            text = next((m[3] for m in self._msgs if m[0] == msg_id
                         and time.time() - m[2] < 60), None)
        if ev is not None:            # already being synthesised
            ev.wait(15)
            return self._audio.get(msg_id)
        if text is None:
            return None
        return self._synth(msg_id, text)

    # -------------------------------------------------------- core

    def _say(self, sender, text, prio=1):
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            self._msgs.append((mid, sender, time.time(), text, prio))
            st = self._senders.get(sender)
            listening = st is not None and time.time() - st.listened < 10
        print("[engineer] %s: %s" % (sender, text))
        if listening and self.el_key:
            threading.Thread(target=self._synth, args=(mid, text),
                             daemon=True).start()

    def _synth(self, mid, text):
        with self._lock:
            if mid in self._audio:
                return self._audio[mid]
            ev = self._audio_events.get(mid)
            if ev is None:
                ev = self._audio_events[mid] = threading.Event()
                owner = True
            else:
                owner = False
        if not owner:
            ev.wait(15)
            return self._audio.get(mid)
        data = None
        try:
            req = urllib.request.Request(
                "https://api.elevenlabs.io/v1/text-to-speech/%s"
                "?output_format=mp3_44100_64" % self.el_voice,
                data=json.dumps({
                    "text": text, "model_id": self.el_model,
                    "language_code": "es",
                    "voice_settings": {"stability": 0.5,
                                       "similarity_boost": 0.75,
                                       "speed": 1.1},
                }).encode(),
                headers={"xi-api-key": self.el_key,
                         "Content-Type": "application/json",
                         "Accept": "audio/mpeg"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            self.last_tts_error = None
        except urllib.error.HTTPError as e:
            self.last_tts_error = "ElevenLabs HTTP %d: %s" % (
                e.code, e.read()[:200].decode("utf-8", "replace"))
            print("[engineer] %s" % self.last_tts_error)
        except Exception as e:
            self.last_tts_error = "ElevenLabs: %r" % e
            print("[engineer] %s" % self.last_tts_error)
        with self._lock:
            if data:
                self._audio[mid] = data
                while len(self._audio) > 40:
                    self._audio.popitem(last=False)
            self._audio_events.pop(mid, None)
        ev.set()
        return data

    def _ready(self, st, key, every, now):
        """True (and starts the cooldown) if `key` may speak now."""
        if now < st.cooldown.get(key, 0):
            return False
        st.cooldown[key] = now + every
        return True

    def _held(self, st, key, cond, secs, now):
        """True once `cond` has held for `secs` seconds."""
        if not cond:
            st.since.pop(key, None)
            return False
        t0 = st.since.setdefault(key, now)
        return now - t0 >= secs

    def observe(self, sender, live, session_type, names):
        """Called by the recorder on every lap-data update of a sender."""
        now = time.time()
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
        if now - st.last_obs < 0.25:
            return
        st.last_obs = now
        r = live.get("race")
        if not r:
            return
        sess = (live.get("track_id"), session_type)
        if st.session != sess:        # new session: forget everything
            listened = st.listened
            st.__init__()
            st.listened, st.session = listened, sess
            st.prev = r
            return
        p, st.prev = st.prev, r
        if p is None:
            return
        try:
            self._rules(sender, st, p, r, live, session_type in RACE_SESSIONS,
                        names, now)
        except Exception as e:      # a rule bug must never stop recording
            print("[engineer] rule error: %r" % e)

    # -------------------------------------------------------- rules

    def _rules(self, sender, st, p, r, live, race, names, now):
        say = lambda text, prio=1: self._say(sender, text, prio)
        lap, total = r.get("lap_num") or 0, r.get("total_laps") or 0
        pos = r.get("position")
        on_track = (r.get("pit_status") or 0) == 0
        speed = live.get("speed") or 0

        # --- flags (always first, they matter most)
        sc0, sc1 = p.get("safety_car") or 0, r.get("safety_car") or 0
        if sc1 != sc0:
            if sc1 == 1:
                say("Safety car, safety car. Respeta el delta y no adelantes.", 3)
            elif sc1 == 2:
                say("Virtual safety car desplegado. Mantén el delta positivo.", 3)
            elif sc0 == 1 and sc1 == 0:
                say("Safety car terminado. Verde, verde, verde. ¡A fondo!", 3)
            elif sc0 == 2 and sc1 == 0:
                say("Fin del virtual safety car. Verde, ¡empuja!", 3)

        # --- penalties / warnings
        if (r.get("penalties_s") or 0) > (p.get("penalties_s") or 0):
            add = r["penalties_s"] - (p.get("penalties_s") or 0)
            say("Nos han puesto una penalización de %d segundos. "
                "Ahora llevamos %d en total." % (add, r["penalties_s"]), 3)
        if (r.get("warnings") or 0) > (p.get("warnings") or 0):
            say("Advertencia por límites de pista. Llevas %d, cuidado."
                % r["warnings"], 2)

        # --- weather
        w0, w1 = p.get("weather"), r.get("weather")
        if w0 is not None and w1 is not None and w0 != w1:
            if w0 < 3 <= w1:
                say("Empieza a llover. Atento al agarre, piensa en intermedios.", 2)
            elif w1 < 3 <= w0:
                say("Ha dejado de llover, la pista se va a secar.", 2)
            elif w1 >= 4 > w0:
                say("La lluvia es más fuerte ahora.", 2)

        # --- pit stop done
        if (r.get("pit_stops") or 0) > (p.get("pit_stops") or 0):
            st.wear_step = 0
            st.since.clear()
            st.cooldown["cold"] = now + 60
        if race and (p.get("pit_status") or 0) != 0 and on_track and pos \
                and r.get("pit_stops"):
            say("Parada hecha, sales P%d. Calienta bien las gomas." % pos, 2)
            st.cooldown["cold"] = now + 60

        # --- lap completed
        if lap and p.get("lap_num") and lap == p["lap_num"] + 1:
            self._on_lap(sender, st, p, r, race, names, now)

        # --- position changes (held for 3 s so side-by-side doesn't flap)
        if race and pos and lap >= 1:
            if st.pos_seen is None or st.pos_seen[0] != pos:
                st.pos_seen = (pos, now)
            if st.pos_said is None:
                st.pos_said = pos
            elif pos != st.pos_said and now - st.pos_seen[1] >= 3:
                if pos < st.pos_said:
                    say("¡Bien hecho! Ganas %s. Ahora P%d." % (
                        "la posición" if st.pos_said - pos == 1
                        else "%d posiciones" % (st.pos_said - pos), pos), 2)
                else:
                    say("Pierdes %s, ahora P%d. Vamos a recuperarla." % (
                        "una posición" if pos - st.pos_said == 1
                        else "%d posiciones" % (pos - st.pos_said), pos), 2)
                st.pos_said = pos

        # --- gaps: DRS range ahead, attack from behind
        if race and lap >= 2 and on_track:
            ga0, ga1 = p.get("gap_front_ms"), r.get("gap_front_ms")
            if ga0 and ga1 and ga1 < 1000 <= ga0 \
                    and self._ready(st, "drs_ahead", 45, now):
                who = names.get("ahead")
                say("Estás a %s%s. Tienes DRS, a por él." % (
                    gap_es(ga1), " de " + who if who else " del de delante"), 1)
            gb0, gb1 = p.get("gap_behind_ms"), r.get("gap_behind_ms")
            if gb0 and gb1 and gb1 < 1000 <= gb0 \
                    and self._ready(st, "drs_behind", 45, now):
                who = names.get("behind")
                say("%s a %s detrás, tiene DRS. Defiende." % (
                    who or "El de atrás", gap_es(gb1)), 2)

        self._tyre_rules(say, st, r, lap, on_track, speed, now)
        self._damage_rules(say, st, r)

    def _on_lap(self, sender, st, p, r, race, names, now):
        say = lambda text, prio=1: self._say(sender, text, prio)
        lap, total = r["lap_num"], r.get("total_laps") or 0
        last, best = r.get("last_lap_ms"), r.get("best_lap_ms")
        prev_best = p.get("best_lap_ms")

        if last and best and last == best and prev_best and last < prev_best:
            say("Vuelta rápida personal, %s. ¡Muy bien!" % laptime_es(last), 1)

        if race and total:
            if lap == total:
                say("Última vuelta. Todo lo que tengas, ¡vamos!", 2)
                return
            left = total - lap + 1
            if left == 5:
                say("Quedan 5 vueltas.", 1)

        # fuel: speak when the picture changes, and keep reminding if short
        surplus = (r.get("fuel") or {}).get("laps_surplus")
        if race and surplus is not None and lap >= 3:
            state = ("short" if surplus < -0.4
                     else "plenty" if surplus > 0.8 else "ok")
            remind = (state == "short" and lap - st.fuel_said_lap >= 4)
            changed = state != st.fuel_state and (
                st.fuel_state is not None or state != "ok")
            if changed or remind:
                if state == "short":
                    say("Gasolina justa, vamos %s vueltas cortos. Levanta y deja "
                        "rodar antes de las frenadas." % num_es(-surplus), 2)
                elif state == "plenty":
                    say("Vamos bien de gasolina, %s vueltas de sobra. "
                        "Puedes usar mezcla rica." % num_es(surplus), 1)
                else:
                    say("Gasolina en objetivo ahora.", 1)
                st.fuel_said_lap = lap
            st.fuel_state = state

        # race summary every few laps
        if race and lap >= 2 and lap % SPEAK_EVERY_LAPS == 0 and r.get("position"):
            bits = ["Vuelta %d. P%d." % (lap, r["position"])]
            if r.get("gap_front_ms"):
                bits.append("Delante a %s." % gap_es(r["gap_front_ms"]))
            if r.get("gap_behind_ms"):
                bits.append("Detrás a %s." % gap_es(r["gap_behind_ms"]))
            if last:
                bits.append("Última vuelta %s." % laptime_es(last))
            say(" ".join(bits), 0)

    def _tyre_rules(self, say, st, r, lap, on_track, speed, now):
        ty = r.get("tyres") or {}
        surf = ty.get("surface")
        kind = VISUAL_KIND.get(ty.get("visual"), "slick")
        lo, hi = WINDOW[kind]
        if surf and on_track and speed > 120:
            for axle, ix, name in (("front", (0, 1), "Delanteros"),
                                   ("rear", (2, 3), "Traseros")):
                avg = sum(surf[i] for i in ix) / 2
                cold = avg < lo - 8 and lap >= 2 and (ty.get("age") or 0) >= 1
                hot = avg > hi + 5
                if self._held(st, axle + "_cold", cold, 8, now) and \
                        now >= st.cooldown.get("cold", 0) and \
                        self._ready(st, axle + "_cold", 120, now):
                    say("%s fríos, %d grados. Mételes temperatura." %
                        (name, avg), 1)
                if self._held(st, axle + "_hot", hot, 6, now) and \
                        self._ready(st, axle + "_hot", 90, now):
                    say("%s sobrecalentando, %d grados. %s" % (
                        name, avg, "Cuida el deslizamiento en curva."
                        if axle == "front" else "Suave con el acelerador."), 2)
        brakes = ty.get("brake")
        if brakes and max(brakes) > 1050 and \
                self._held(st, "brakes_hot", True, 4, now) and \
                self._ready(st, "brakes_hot", 120, now):
            say("Frenos muy calientes. Busca aire limpio.", 1)
        elif brakes and max(brakes) <= 1050:
            st.since.pop("brakes_hot", None)

        wear = ty.get("wear")
        if wear:
            worst = max(range(4), key=lambda i: wear[i])
            w = wear[worst]
            if st.wear_step and w < WEAR_STEPS[0] - 15:
                st.wear_step = 0          # fresh set
            crossed = sum(1 for x in WEAR_STEPS if w >= x)
            if crossed > st.wear_step:
                st.wear_step = crossed
                step = WEAR_STEPS[crossed - 1]
                msg = "Neumáticos al %d%% de desgaste, el peor es el %s." % (
                    w, CORNER_ES[worst])
                if step >= 75:
                    msg += " Están al límite, piensa en parar."
                say(msg, 2 if step >= 60 else 1)

        dmg = ty.get("damage")
        if dmg and max(dmg) >= 50 and st.dmg_said.get("tyre", 0) < 50:
            i = max(range(4), key=lambda i: dmg[i])
            say("Daño en el neumático %s. Entra a boxes si pierde presión."
                % CORNER_ES[i], 3)
            st.dmg_said["tyre"] = 50

    def _damage_rules(self, say, st, r):
        d = r.get("damage") or {}
        for k, label in PARTS_ES.items():
            v = d.get(k)
            if v is None:
                continue
            said = st.dmg_said.get(k, 0)
            if v >= 10 and v >= said + 10:
                msg = "Daño en el %s, %d%%." % (label, v)
                if k.startswith("wing_f") and v >= 40:
                    msg += " Considera cambiar el alerón en la parada."
                say(msg, 3 if v >= 40 else 2)
                st.dmg_said[k] = v
            elif v < said - 20:
                st.dmg_said[k] = v      # repaired
        for k, label in (("drs_fault", "Fallo de DRS. No lo vas a tener."),
                         ("ers_fault", "Fallo de ERS. Vas sin batería.")):
            if d.get(k) and not st.dmg_said.get(k):
                say(label, 3)
                st.dmg_said[k] = 1
