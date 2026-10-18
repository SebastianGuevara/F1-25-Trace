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

from . import brain

RACE_SESSIONS = {15, 16, 17}
# tyre surface working window (°C) by visual compound family
WINDOW = {"slick": (85, 105), "inter": (60, 80), "wet": (50, 70)}
VISUAL_KIND = {16: "slick", 17: "slick", 18: "slick",
               7: "inter", 19: "inter", 8: "wet", 20: "wet"}
COMPOUND_ES = {16: "blando", 17: "medio", 18: "duro", 7: "intermedio",
               19: "intermedio", 8: "lluvia", 20: "lluvia"}
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
STRATEGY_EVERY_LAPS = 8    # Claude strategy brief cadence (race)
TIME_CALLS = (600, 300, 180, 60)   # session time left (s) worth a call

# Rough list prices for the usage estimate (USD). Claude per million
# tokens (input, output) by model family; speech-to-text per minute.
# ElevenLabs is counted in characters/credits, its price depends on plan.
LLM_PRICES = {"haiku": (1.0, 5.0), "sonnet": (3.0, 15.0), "opus": (5.0, 25.0)}
STT_PRICES = {"gpt-4o-mini-transcribe": 0.003, "gpt-4o-transcribe": 0.006,
              "whisper-1": 0.006}


def _zero_usage():
    return {"llm_calls": 0, "llm_in": 0, "llm_out": 0, "llm_usd": 0.0,
            "stt_calls": 0, "stt_sec": 0.0, "stt_usd": 0.0,
            "tts_calls": 0, "tts_chars": 0}


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
        self.laps = []            # completed-lap history for the brain
        self.best_valid = 0       # best valid lap (ms), any session
        self.best_s = [0, 0]      # best sector 1 / 2 (ms)
        self.pending_sector = 0
        self.time_said = set()
        self.was_cold = False
        self.rain_said = set()
        self.pit_said = set()
        self.qa = []              # [(question, answer)] radio conversation
        self.asking = False
        self.ask_lock = threading.Lock()
        self.driver = None
        self.live = None          # latest live snapshot (for questions)
        self.session_type = None
        self.extra = {}           # whole field, setup, assists (1 Hz)
        self.field_best = {}      # driver name -> best lap ms seen
        self.pace = None          # last pace call id (push_* / manage_*)
        self.pace_lap = 0
        self.pit_lap = False      # this lap started in / went through the pits


class RaceEngineer:
    def __init__(self, usage_path=None):
        self._lock = threading.Lock()
        self._senders = {}
        self.usage_path = usage_path
        self._usage = {"total": _zero_usage(), "days": {}}
        self._usage_start = _zero_usage()      # since TRACE started
        if usage_path:
            try:
                with open(usage_path, encoding="utf-8") as f:
                    saved = json.load(f)
                self._usage["total"].update(saved.get("total") or {})
                self._usage["days"] = saved.get("days") or {}
            except (OSError, ValueError):
                pass
        brain.usage_hook = self._add_usage
        self._msgs = collections.deque(maxlen=200)   # (id, sender, t, text, prio)
        self._next_id = 1
        self._audio = collections.OrderedDict()      # msg id -> mp3 bytes
        self._audio_events = {}
        self._replies = collections.deque(maxlen=50)   # ids answering the driver
        self._conts = collections.deque(maxlen=50)     # 2nd+ sentence of a reply
        self.el_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
        self.el_voice = (os.environ.get("ELEVENLABS_VOICE_ID", "").strip()
                         or "pNInz6obpgDQGcFmaJgB")
        self.el_model = (os.environ.get("ELEVENLABS_MODEL", "").strip()
                         or "eleven_flash_v2_5")
        try:   # ElevenLabs accepts 0.7 (slow) .. 1.2 (fast)
            self.el_speed = min(1.2, max(0.7, float(
                os.environ.get("ELEVENLABS_SPEED", "") or 0.95)))
        except ValueError:
            self.el_speed = 0.95
        self.last_tts_error = None

    # -------------------------------------------------------- usage

    def _add_usage(self, kind, model=None, input=0, output=0, seconds=0.0,
                   chars=0):
        d = _zero_usage()
        if kind == "llm":
            fam = next((k for k in LLM_PRICES if k in (model or "")), "sonnet")
            pi, po = LLM_PRICES[fam]
            d.update(llm_calls=1, llm_in=input, llm_out=output,
                     llm_usd=(input * pi + output * po) / 1e6)
        elif kind == "stt":
            d.update(stt_calls=1, stt_sec=seconds,
                     stt_usd=seconds / 60 * STT_PRICES.get(model, 0.006))
        elif kind == "tts":
            d.update(tts_calls=1, tts_chars=chars)
        day = time.strftime("%Y-%m-%d")
        with self._lock:
            days = self._usage["days"]
            for bucket in (self._usage["total"], self._usage_start,
                           days.setdefault(day, _zero_usage())):
                for k, v in d.items():
                    bucket[k] = bucket.get(k, 0) + v
            for old in sorted(days)[:-60]:      # keep two months of days
                del days[old]
            snap = json.dumps(self._usage)
        if self.usage_path:
            try:
                tmp = self.usage_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(snap)
                os.replace(tmp, self.usage_path)
            except OSError:
                pass

    def usage(self):
        def view(u):
            u = dict(_zero_usage(), **u)
            u["usd"] = round(u["llm_usd"] + u["stt_usd"], 4)
            u["llm_usd"] = round(u["llm_usd"], 4)
            u["stt_usd"] = round(u["stt_usd"], 4)
            u["stt_min"] = round(u.pop("stt_sec") / 60, 2)
            return u
        with self._lock:
            today = self._usage["days"].get(time.strftime("%Y-%m-%d"), {})
            return {"today": view(today), "since_start": view(self._usage_start),
                    "total": view(self._usage["total"]),
                    "tts_model": self.el_model,
                    "note": "estimado con precios de lista; ElevenLabs en caracteres"}

    # -------------------------------------------------------- public

    def info(self):
        return {"tts": "elevenlabs" if self.el_key else "browser",
                "tts_error": self.last_tts_error,
                "stt": brain.stt_ready(), "llm": brain.llm_ready(),
                "token": bool(os.environ.get("RADIO_TOKEN", "").strip()),
                "wake": brain.wake_label()}

    def knows(self, sender):
        """True if this sender has sent telemetry since TRACE started."""
        with self._lock:
            st = self._senders.get(sender)
            return st is not None and st.live is not None

    def messages(self, sender, since):
        """Calls for one sender newer than `since`; marks it as listened."""
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
            st.listened = time.time()
            last = self._next_id - 1
            out = [{"id": i, "t": t, "text": tx, "prio": p,
                    "reply": i in self._replies, "cont": i in self._conts}
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

    def _say(self, sender, text, prio=1, reply=False, cont=False):
        with self._lock:
            mid = self._next_id
            self._next_id += 1
            if reply:
                self._replies.append(mid)
            if cont:
                self._conts.append(mid)
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
                                       "speed": self.el_speed},
                }).encode(),
                headers={"xi-api-key": self.el_key,
                         "Content-Type": "application/json",
                         "Accept": "audio/mpeg"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = resp.read()
            self.last_tts_error = None
            self._add_usage("tts", chars=len(text))
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

    def observe(self, sender, live, session_type, names, extra=None):
        """Called by the recorder on every lap-data update of a sender."""
        now = time.time()
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
        if extra:
            st.extra = extra
            for c in extra.get("field") or []:
                ms, n = c.get("last_lap_ms"), c.get("name")
                if ms and n and (n not in st.field_best or ms < st.field_best[n]):
                    st.field_best[n] = ms
        if now - st.last_obs < 0.25:
            return
        st.last_obs = now
        r = live.get("race")
        if not r:
            return
        st.live, st.driver = live, live.get("driver_name")
        st.session_type = session_type
        sess = (live.get("track_id"), session_type)
        if st.session != sess:        # new session: forget everything
            listened = st.listened
            st.__init__()
            st.listened, st.session = listened, sess
            st.live, st.driver = live, live.get("driver_name")
            st.session_type = session_type
            st.prev = r
            # joining mid-session: don't read out time marks already passed
            tl = r.get("time_left") or 0
            st.time_said = {x for x in TIME_CALLS if tl and tl <= x}
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
        if not on_track:
            st.pit_lap = True
        if lap and p.get("lap_num") and lap == p["lap_num"] + 1:
            self._on_lap(sender, st, p, r, race, names, now)
            st.pit_lap = not on_track

        # --- position changes (held for 3 s so side-by-side doesn't flap;
        # the lap-1 scramble is summed up once lap 2 starts)
        if race and pos and lap >= 2:
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
                say("Estás a %s%s. %s" % (
                    gap_es(ga1), " de " + who if who else " del de delante",
                    "Tienes overtake, úsalo en la recta." if r.get("season") == 2026
                    else "Tienes DRS, a por él."), 1)
            gb0, gb1 = p.get("gap_behind_ms"), r.get("gap_behind_ms")
            if gb0 and gb1 and gb1 < 1000 <= gb0 \
                    and self._ready(st, "drs_behind", 45, now):
                who = names.get("behind")
                say("%s a %s detrás, %s. Defiende." % (
                    who or "El de atrás", gap_es(gb1),
                    "puede usar overtake" if r.get("season") == 2026
                    else "tiene DRS"), 2)

        if race and lap >= 2 and on_track:
            self._rival_pit_rules(say, st, r, now)
        self._tyre_rules(say, st, r, lap, on_track, speed, now)
        self._damage_rules(say, st, r)
        self._forecast_rules(say, st, r)
        if race:
            self._pit_window_rules(say, st, r)
        else:
            self._session_rules(say, st, p, r, lap, on_track, speed, now)

    def _on_lap(self, sender, st, p, r, race, names, now):
        say = lambda text, prio=1: self._say(sender, text, prio)
        lap, total = r["lap_num"], r.get("total_laps") or 0
        last, best = r.get("last_lap_ms"), r.get("best_lap_ms")
        prev_best = p.get("best_lap_ms")

        valid = not p.get("lap_invalid")
        ty = r.get("tyres") or {}
        st.laps = (st.laps + [{
            "lap": p.get("lap_num"), "time": laptime_es(last) if last else None,
            "ms": last, "valid": valid, "pos": r.get("position"),
            "fuel_kg": (r.get("fuel") or {}).get("kg"),
            "wear_max": max(ty["wear"]) if ty.get("wear") else None,
            "compound": ty.get("visual"), "tyre_age": ty.get("age"),
            "gap_front_ms": r.get("gap_front_ms"),
            "gap_behind_ms": r.get("gap_behind_ms"),
            "pit_stops": r.get("pit_stops"),
        }])[-60:]
        if last and valid and not st.pit_lap:
            is_best = not st.best_valid or last < st.best_valid
            prev_valid_best = st.best_valid
            if is_best:
                st.best_valid = last
            if not race:
                if is_best:
                    say("%s. Tu mejor vuelta%s." % (
                        laptime_es(last),
                        ", P%d" % r["position"] if r.get("position") else ""), 1)
                elif prev_valid_best and last < prev_valid_best * 1.04:
                    say("%s, a %s de tu mejor." % (
                        laptime_es(last), gap_es(last - prev_valid_best)), 0)
            elif last and best and last == best and prev_best and last < prev_best:
                say("Vuelta rápida personal, %s. ¡Muy bien!" % laptime_es(last), 1)

        if race and lap == 2 and r.get("grid") and r.get("position"):
            d = r["grid"] - r["position"]
            say("Primera vuelta hecha, P%d. %s" % (r["position"],
                "Ganas %d posiciones, ¡buena salida!" % d if d > 1 else
                "Ganas una posición." if d == 1 else
                "Mantienes la posición." if d == 0 else
                "Perdemos %d, tranquilo, carrera larga." % -d), 1)

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

        if race and total and lap >= 3:
            self._pace_rules(say, st, r, lap, total)

        # race summary every few laps
        if race and lap >= 2 and lap % SPEAK_EVERY_LAPS == 0 and r.get("position"):
            # one fact, the one that matters, not a list of numbers
            bits = ["Vuelta %d, P%d." % (lap, r["position"])]
            trend = self._gap_trend(st)
            gf, gb = r.get("gap_front_ms"), r.get("gap_behind_ms")
            if trend:
                bits.append(trend)
            elif gb and gb < 1500 and (not gf or gb < gf):
                bits.append("%s detrás." % gap_es(gb).capitalize())
            elif gf and gf < 3000:
                bits.append("A %s del de delante." % gap_es(gf))
            elif last and best and last <= best + 300:
                bits.append("Buen ritmo, sigue así.")
            say(" ".join(bits), 0)

        # Claude strategy brief now and then (only when someone listens)
        if race and total and lap >= 4 and lap % STRATEGY_EVERY_LAPS == 0 \
                and total - lap >= 3 and brain.llm_ready() \
                and now - st.listened < 10:
            threading.Thread(target=self._strategy_brief, args=(sender, st),
                             daemon=True).start()

    @staticmethod
    def _gap_trend(st):
        """'Le recortas 3 décimas por vuelta' over the last 3 laps at an
        unchanged position, or None."""
        h = st.laps[-4:]
        if len(h) < 4 or len({x["pos"] for x in h}) != 1 or \
                not all(x["gap_front_ms"] for x in h):
            return None
        per = (h[-1]["gap_front_ms"] - h[0]["gap_front_ms"]) / 3
        if abs(per) < 100:
            return None
        if per < 0:
            return "Le recortas %s por vuelta al de delante." % gap_es(-per)
        return "El de delante se escapa, %s por vuelta." % gap_es(per)

    def _rival_pit_rules(self, say, st, r, now):
        """The car just ahead / behind dives into the pits: the classic
        undercut call (push to jump them / cover them)."""
        field = st.extra.get("field") or []
        pos = r.get("position")
        for c in field:
            n = c.get("name")
            if not n or c.get("me"):
                continue
            key = "pit_" + n
            if not c.get("in_pit") or now < st.cooldown.get(key, 0):
                continue
            if c["pos"] == pos - 1:
                st.cooldown[key] = now + 180
                say("%s entra a boxes. Push, push: vueltas rápidas ahora para "
                    "ganarle la posición al salir." % n, 2)
            elif c["pos"] == pos + 1:
                st.cooldown[key] = now + 180
                say("%s, detrás, entra a boxes. Atento al undercut: empuja "
                    "esta vuelta." % n, 2)

    def _pace_rules(self, say, st, r, lap, total):
        """Once a lap: push or manage, with the reason, only when the
        call changes (at most every 2 laps)."""
        left = total - lap + 1
        gf, gb = r.get("gap_front_ms"), r.get("gap_behind_ms")
        ty = r.get("tyres") or {}
        wear_now = max(ty["wear"]) if ty.get("wear") else None
        # wear rate over the current stint's last laps
        stint = [x for x in st.laps if x.get("pit_stops") == r.get("pit_stops")
                 and x.get("wear_max") is not None][-4:]
        proj, to_go = None, left
        pw = r.get("pit_window")
        if pw and not r.get("pit_stops") and pw.get("ideal", 0) > lap:
            to_go = pw["ideal"] - lap
        if wear_now is not None and len(stint) >= 3:
            rate = (stint[-1]["wear_max"] - stint[0]["wear_max"]) / (len(stint) - 1)
            if rate > 0:
                proj = wear_now + rate * to_go

        call = reason = None
        if left <= 3 and ((gf and gf < 2000) or (gb and gb < 1500)):
            call, reason = "push_end", "Últimas vueltas y hay pelea, todo lo que tengas."
        elif proj is not None and proj > 80:
            call, reason = "manage_tyres", (
                "Gestiona los neumáticos: a este ritmo %s %s. Suave en las "
                "tracciones y sin deslizar." % (
                    "no llegan" if proj >= 95 else
                    "llegamos al %d%% de desgaste" % proj,
                    "a la parada" if to_go < left else "al final"))
        elif gf and gf < 1500:
            call, reason = "push_attack", "Tienes al de delante a %s, push ahora." % gap_es(gf)
        elif gb and gb < 1200:
            call, reason = "push_defend", "Te aprietan por detrás a %s, mantén el ritmo." % gap_es(gb)
        elif (gf is None or gf > 4000) and gb and gb > 4000:
            call, reason = "manage_air", ("Aire limpio delante y detrás. Gestiona "
                                      "gomas y gasolina, ritmo constante.")
        elif proj is not None and proj < 55 and to_go <= 8:
            call, reason = "push_tyres", "Las gomas aguantan de sobra, puedes apretar."
        if call and call != st.pace and lap - st.pace_lap >= 2:
            st.pace, st.pace_lap = call, lap
            say(reason, 1)

    def _forecast_rules(self, say, st, r):
        fc = r.get("forecast") or []
        w = r.get("weather")
        if w is None or not fc:
            return
        if w < 3:
            nxt = next((f for f in fc if f["min"] > 0 and
                        (f["weather"] >= 3 or f["rain"] >= 60)), None)
            if nxt is None:
                st.rain_said.discard("far")
                st.rain_said.discard("near")
                return
            key = "near" if nxt["min"] <= 5 else "far"
            if key not in st.rain_said:
                st.rain_said.add(key)
                say("Pronóstico: lluvia en unos %d minutos, probabilidad del %d%%."
                    % (nxt["min"], nxt["rain"]), 2 if key == "near" else 1)
        else:
            st.rain_said.discard("far")
            st.rain_said.discard("near")
            nxt = next((f for f in fc if f["min"] > 0 and f["weather"] < 3
                        and f["rain"] < 30), None)
            if nxt and "dry" not in st.rain_said:
                st.rain_said.add("dry")
                say("Se espera que pare de llover en unos %d minutos." % nxt["min"], 1)

    def _pit_window_rules(self, say, st, r):
        pw, lap = r.get("pit_window"), r.get("lap_num") or 0
        if not pw or (r.get("pit_stops") or 0) > 0:
            return
        if lap >= pw["ideal"] - 1 and "open" not in st.pit_said:
            st.pit_said.add("open")
            msg = "Se abre la ventana de parada. Vuelta ideal %d, como tarde la %d." % (
                pw["ideal"], pw["latest"])
            if pw.get("rejoin"):
                msg += " Saldrías P%d." % pw["rejoin"]
            say(msg, 2)
        if pw["latest"] and lap >= pw["latest"] and "last" not in st.pit_said:
            st.pit_said.add("last")
            say("Última vuelta de la ventana. Si paramos, es ahora: box, box.", 3)

    def _session_rules(self, say, st, p, r, lap, on_track, speed, now):
        """Practice / qualifying: invalid laps, sector deltas, time left,
        tyres ready after the out lap."""
        if r.get("lap_invalid") and not p.get("lap_invalid") and \
                (r.get("current_lap_ms") or 0) > 3000 and \
                self._ready(st, "invalid", 20, now):
            say("Vuelta anulada por límites de pista.", 2)

        sec0, sec1 = p.get("sector"), r.get("sector")
        if sec1 != sec0 and sec1 in (1, 2):
            st.pending_sector = sec1
        k = st.pending_sector
        if k and not r.get("lap_invalid"):
            t = r.get("s1_ms") if k == 1 else r.get("s2_ms")
            if t:
                st.pending_sector = 0
                best = st.best_s[k - 1]
                if best and t < best * 1.05:      # a push lap, not an out lap
                    d = t - best
                    if abs(d) >= 50:
                        say("Sector %s, %s %s." % (
                            "uno" if k == 1 else "dos", gap_es(abs(d)),
                            "más rápido" if d < 0 else "más lento"), 0)
                if not best or t < best:
                    st.best_s[k - 1] = t
        elif k and r.get("lap_invalid"):
            st.pending_sector = 0

        tl = r.get("time_left") or 0
        for x in sorted(TIME_CALLS):
            if tl and tl <= x and x not in st.time_said:
                st.time_said.update(y for y in TIME_CALLS if y >= x)
                if x == 60:
                    say("Último minuto. Cruza la línea antes de cero para "
                        "tener otra vuelta.", 2)
                else:
                    say("Quedan %d minutos de sesión." % (x // 60), 1)
                break

        ty = r.get("tyres") or {}
        surf = ty.get("surface")
        if surf and on_track:
            lo = WINDOW[VISUAL_KIND.get(ty.get("visual"), "slick")][0]
            cold = min(surf) < lo - 8
            if self._held(st, "out_cold", cold, 10, now):
                st.was_cold = True
            if st.was_cold and min(surf) >= lo - 3 and speed > 120:
                st.was_cold = False
                say("Gomas en temperatura, puedes empujar.", 1)

    # -------------------------------------------------------- brain

    def _context(self, st):
        live = st.live or {}
        r = dict(live.get("race") or {})
        r.pop("forecast", None)
        r["forecast"] = (live.get("race") or {}).get("forecast", [])[:6]
        ty = r.get("tyres") or {}
        r["tyre_compound"] = COMPOUND_ES.get(ty.get("visual"))
        age = time.time() - live.get("updated_at", 0)
        return {
            "season": r.get("season"),
            "live": {"overtake": live.get("overtake"), "aero": live.get("aero"),
                     "ers_pct": r.get("ers_pct"), "ers_mode": r.get("ers_mode"),
                     "drs": live.get("drs")},
            "on_track": age < 3, "telemetry_age_s": int(age),
            "session": r.get("session_type"), "track": live.get("track_name"),
            "speed_kmh": live.get("speed"), "gear": live.get("gear"),
            "now": r,
            "field": self._field(st),
            "my_setup": st.extra.get("setup"),
            "assists": st.extra.get("assists"),
            "history": [dict({k: v for k, v in x.items() if k != "ms"},
                             compound=COMPOUND_ES.get(x.get("compound")))
                        for x in st.laps[-15:]],
            "recent_radio_calls": [m[3] for m in list(self._msgs)[-6:]],
        }

    @staticmethod
    def _field(st):
        out = []
        for c in st.extra.get("field") or []:
            best = st.field_best.get(c.get("name"))
            out.append({
                "pos": c["pos"], "name": c.get("name"), "team": c.get("team"),
                "me": c.get("me") or None,
                "gap_leader": gap_es(c["gap_leader_ms"]) if c.get("gap_leader_ms") else None,
                "gap_front": gap_es(c["gap_front_ms"]) if c.get("gap_front_ms") else None,
                "last_lap": laptime_es(c["last_lap_ms"]) if c.get("last_lap_ms") else None,
                "best_lap_seen": laptime_es(best) if best else None,
                "tyre": COMPOUND_ES.get(c.get("tyre_visual")),
                "tyre_age": c.get("tyre_age"), "pit_stops": c.get("pit_stops"),
                "in_pit": c.get("in_pit") or None,
                "penalties_s": c.get("penalties_s") or None,
            })
        return [{k: v for k, v in x.items() if v is not None} for x in out]

    def _strategy_brief(self, sender, st):
        try:
            text = brain.answer(
                st.driver, self._context(st),
                "(Mensaje automático, no del piloto.) Dame una lectura "
                "estratégica breve: cómo vamos de neumáticos, gasolina y "
                "ritmo, y qué conviene hacer ahora. Máximo 2 frases.",
                [], strategy=True)
            if text:
                self._say(sender, text, 1)
        except brain.BrainError as e:
            print("[engineer] strategy: %s" % e)

    def ask(self, sender, audio, ctype, need_wake=True):
        """Driver spoke on the radio: transcribe, check the wake word,
        answer with Claude through the normal radio queue."""
        with self._lock:
            st = self._senders.setdefault(sender, _Sender())
        # one phrase at a time per driver: a phrase that arrives while the
        # previous one is still being answered waits instead of being lost
        if not st.ask_lock.acquire(timeout=20):
            return {"heard": None, "busy": True}
        st.asking = True
        t0 = time.time()
        try:
            heard = brain.transcribe(
                audio, "voz.webm" if "webm" in ctype else "voz.wav", ctype)
            if not heard or brain.echoes_prompt(heard):
                return {"heard": ""}
            if need_wake:
                q = brain.strip_wake(heard)
            else:
                # follow-up without wake word: drop noise transcriptions
                # ("gracias", "subtítulos...") — a real question has words
                q = heard if brain.looks_like_speech(heard) else None
            if q is None:
                return {"heard": heard, "accepted": False}
            print("[engineer] %s pregunta: %s" % (sender, heard))
            if not q:
                # just the wake word: the page beeps and listens for the
                # question without needing the wake word again
                return {"heard": heard, "accepted": True, "wake_only": True}
            t_stt = time.time() - t0
            parts = []
            try:
                # voice each sentence as soon as it's written
                for chunk in brain.answer_stream(
                        st.driver, self._context(st), q, st.qa):
                    if not parts:
                        print("[engineer] tiempos: voz->texto %.1fs, "
                              "primera frase %.1fs" % (t_stt, time.time() - t0))
                    self._say(sender, chunk, 3, reply=True, cont=bool(parts))
                    parts.append(chunk)
            except brain.BrainError as e:
                print("[engineer] answer: %s" % e)
                if not parts:
                    self._say(sender, "Perdona, tengo problemas en el muro. "
                              "Repíteme en un momento.", 3, reply=True)
                    return {"heard": heard, "accepted": True, "error": str(e)}
            if not parts:
                parts = ["Perdona, se me ha cortado la respuesta. "
                         "Pregúntamelo otra vez."]
                self._say(sender, parts[0], 3, reply=True)
            st.qa = (st.qa + [(q, " ".join(parts))])[-8:]
            return {"heard": heard, "accepted": True}
        except brain.BrainError as e:
            print("[engineer] ask: %s" % e)
            return {"heard": None, "error": str(e)}
        finally:
            st.asking = False
            st.ask_lock.release()

    def _tyre_rules(self, say, st, r, lap, on_track, speed, now):
        ty = r.get("tyres") or {}
        surf = ty.get("surface")
        kind = VISUAL_KIND.get(ty.get("visual"), "slick")
        lo, hi = WINDOW[kind]
        if surf and on_track and speed > 120:
            for axle, ix, name in (("front", (0, 1), "Delanteros"),
                                   ("rear", (2, 3), "Traseros")):
                avg = sum(surf[i] for i in ix) / 2
                cold = (avg < lo - 8 and lap >= 2 and (ty.get("age") or 0) >= 1
                        and st.session_type in RACE_SESSIONS)
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
        drs = ("Fallo en la aerodinámica activa." if r.get("season") == 2026
               else "Fallo de DRS. No lo vas a tener.")
        for k, label in (("drs_fault", drs),
                         ("ers_fault", "Fallo de ERS. Vas sin batería.")):
            if d.get(k) and not st.dmg_said.get(k):
                say(label, 3)
                st.dmg_said[k] = 1
