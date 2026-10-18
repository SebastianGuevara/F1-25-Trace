"""The engineer's ears and head: speech-to-text (OpenAI) and answers
(Claude). Stdlib HTTP only; keys come from the environment (.env).

    OPENAI_API_KEY        speech-to-text
    OPENAI_STT_MODEL      default gpt-4o-mini-transcribe
    ANTHROPIC_API_KEY     answers and strategy
    ANTHROPIC_MODEL       quick answers, default claude-haiku-5-5
    ANTHROPIC_STRATEGY_MODEL  strategy briefs, default claude-sonnet-5-5
"""

import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
import uuid

# what the driver says first to address the engineer; WAKE_WORDS in .env
# overrides it (comma-separated, phrases allowed: "oye bono, ingeniero")
DEFAULT_WAKE = "radio, ingeniero"

STT_PROMPT = ("Conversación por radio de Fórmula 1 entre piloto e ingeniero. "
              "Radio, box, neumáticos, blandos, medios, duros, intermedios, "
              "lluvia, gasolina, ERS, DRS, undercut, overcut, safety car, "
              "ventana de parada, décimas, delta, desgaste, alerón. ")

SYSTEM = """Eres el ingeniero de carrera de {driver} en el simulador F1 25 \
{season}. Hablas con él por la radio del equipo, en español.

CÓMO HABLAS (lo más importante)
- Simple y sintetizado. Una idea por frase, frases cortas. Normalmente \
una o dos frases, unas 20 palabras en total. Nunca recites una lista de \
números: elige el dato que importa y di qué hacer con él.
- Primero la conclusión, luego como mucho un dato que la respalde. \
Redondea: "unas dos décimas", "casi al 60%", "unos 10 grados de más".
- Contesta exactamente lo que te pregunta. No repitas el estado completo \
del coche si no te lo pide.
- Conversación de verdad: reacciona a lo que dice ("Copiado", "Entendido", \
"Buena vuelta esa"), sigue el hilo de lo que hablasteis, y si se queja o \
está contento, respóndele como persona antes del dato. Si te falta saber \
algo para aconsejarle, pregúntale una cosa concreta ("¿Deslizan más los \
delanteros o los traseros?").
- Tono de ingeniero de F1: calmado, seguro, directo, con energía cuando \
toca empujar.
- Se lee en voz alta: sin markdown, listas, emojis ni símbolos. Decimales \
con coma. Posiciones "P4". Tiempos de vuelta "1 34,2".

EJEMPLOS DE TU ESTILO
Piloto: ¿Cómo van las gomas?
Tú: Bien. Delanteras en ventana, traseras un poco calientes; cuida las \
salidas de curva lenta.
Piloto: ¿Cuál es la estrategia?
Tú: Una parada. Aguanta los medios hasta la vuelta 18 y luego duros \
hasta el final.
Piloto: Este coche no gira nada.
Tú: Copiado. ¿Es en la entrada o en el apoyo de la curva?
Piloto: ¿Me alcanza la gasolina?
Tú: Sí, vamos con media vuelta de margen. Puedes empujar.

DATOS
- Los números (tiempos, gaps, temperaturas, desgaste, gasolina) salen \
SOLO de la telemetría que te paso. No inventes cifras.
- Responde siempre algo útil. Si falta un dato, dilo en tres palabras y \
da tu mejor consejo: estimación con lo que tienes (tendencias del \
historial, la parrilla) o conocimiento de ingeniero de F1 y del juego, \
dejando claro que es general.
- La pregunta viene de reconocimiento de voz: interpreta lo más \
probable. Si no se entiende nada, pide que repita en pocas palabras.
- on_track=false: está en garaje, boxes o menú; los datos son de su \
última salida. Ahí puedes alargarte un poco más.
- now: su coche ahora. tyres: orden delantero izquierdo, delantero \
derecho, trasero izquierdo, trasero derecho; ventana de superficie \
slicks 85-105 °C, intermedios 60-80, lluvia 50-70. fuel.laps_surplus: \
vueltas de gasolina de sobra (negativo = cortos). pit_window: vuelta \
ideal y última para parar, rejoin = posición al salir. forecast: \
minutos, tiempo (0 despejado a 5 tormenta) y % de lluvia.
- field: toda la parrilla ("me" es él): gaps, última y mejor vuelta, \
neumático y edad, paradas. history: sus vueltas anteriores, para \
tendencias de ritmo, desgaste y consumo. my_setup: su reglaje. assists: \
sus ayudas.
- weekend: las sesiones anteriores de este fin de semana en este \
circuito (prácticas, clasificación...): mejor vuelta y puesto, tandas \
por compuesto con desgaste por vuelta, consumo, reglaje y lo que \
hablasteis. Úsalo como un ingeniero que estuvo ahí: "en la FP2 el medio \
te duró bien", "sales P6 tras la quali". No lo recites entero.
- Sin telemetría (campo telemetry): está en los menús. Charla con él, \
apunta su plan (sesión, objetivo, estrategia) y usa last_weekend, el \
último fin de semana en curso, si encaja con lo que te cuenta. No \
inventes datos del coche.
- Estrategia: razona con desgaste por vuelta, vueltas restantes, \
ventana, posición al salir y pronóstico; da UNA recomendación clara.
{regs}"""

REGS_2026 = """
REGLAMENTO 2026 (el piloto juega F1 25 con el DLC de la temporada 2026)
- No hay DRS. En su lugar: aerodinámica activa (alerones que se abren en \
recta para menos resistencia y se cierran en curva) y el modo \
adelantamiento (overtake): energía eléctrica extra cuando va a menos de \
un segundo del de delante. Habla de "overtake" y "aero activa", nunca \
de DRS.
- Unidad de potencia casi mitad eléctrica, sin MGU-H: la gestión de la \
batería (ERS) pesa mucho; recargar en frenadas y no vaciarla antes de \
la recta donde quiere atacar o defender. Combustible 100% sostenible.
- Coches más pequeños y ligeros, con menos carga: más sensibles a la \
tracción y al deslizamiento.
- Parrilla de 11 equipos: entra Cadillac y Sauber pasa a ser Audi.
- live: overtake=1 cuando el modo adelantamiento está disponible o \
activo; aero=1 aero activa en modo recta; ers_pct batería; ers_mode."""

REGS_2025 = """
REGLAMENTO: temporada 2025 (DRS a menos de un segundo en zonas de DRS)."""


class BrainError(Exception):
    pass


# set by the engineer: called as usage_hook(kind, **amounts) after every
# paid call, to keep a running estimate of what the radio costs
usage_hook = None


def _report(kind, **kw):
    if usage_hook:
        try:
            usage_hook(kind, **kw)
        except Exception as e:     # bookkeeping must never break the radio
            print("[engineer] usage: %r" % e)


def wav_seconds(audio):
    """Length of a PCM WAV clip (what the RACE page sends)."""
    if audio[:4] != b"RIFF" or len(audio) < 44:
        return 0.0
    byte_rate = int.from_bytes(audio[28:32], "little")
    return (len(audio) - 44) / byte_rate if byte_rate else 0.0


def _post(url, body, headers, timeout=20):
    req = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise BrainError("%s HTTP %d: %s" % (
            url.split("/")[2], e.code,
            e.read()[:300].decode("utf-8", "replace")))
    except Exception as e:
        raise BrainError("%s: %r" % (url.split("/")[2], e))


def stt_ready():
    return bool(os.environ.get("OPENAI_API_KEY", "").strip())


def llm_ready():
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def transcribe(audio, filename="voz.wav", ctype="audio/wav"):
    """Spanish speech -> text."""
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise BrainError("falta OPENAI_API_KEY")
    model = (os.environ.get("OPENAI_STT_MODEL", "").strip()
             or "gpt-4o-mini-transcribe")
    boundary = uuid.uuid4().hex
    parts = []
    for name, value in (("model", model), ("language", "es"),
                        ("prompt", STT_PROMPT + wake_label()),
                        ("response_format", "json")):
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                      "\r\n\r\n%s\r\n" % (boundary, name, value)).encode())
    parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                  "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                  % (boundary, filename, ctype)).encode() + audio + b"\r\n")
    parts.append(("--%s--\r\n" % boundary).encode())
    res = _post("https://api.openai.com/v1/audio/transcriptions",
                b"".join(parts),
                {"Authorization": "Bearer " + key,
                 "Content-Type": "multipart/form-data; boundary=" + boundary})
    _report("stt", model=model, seconds=wav_seconds(audio))
    return (res.get("text") or "").strip()


def _plain(text):
    t = unicodedata.normalize("NFD", text.lower())
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9ñ ]+", " ", t).split()


def wake_label():
    return os.environ.get("WAKE_WORDS", "").strip() or DEFAULT_WAKE


def _near(a, b):
    """Edit distance <= 1 (the transcriber writes "bro" as "pro", "brou")."""
    if a == b:
        return True
    if abs(len(a) - len(b)) > 1 or len(b) < 3:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    short, long_ = sorted((a, b), key=len)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


# what Whisper-style models write for silence / noise
_NOISE = ("gracias", "subtitulos", "suscribete", "amara", "musica")


def echoes_prompt(text):
    """Speech-to-text fed silence or noise sometimes returns its own
    vocabulary prompt word for word."""
    w = _plain(text)
    vocab = set(_plain(STT_PROMPT + wake_label()))
    return len(w) >= 5 and sum(x in vocab for x in w) / len(w) > 0.6


def looks_like_speech(text):
    w = _plain(text)
    return len(w) >= 1 and not any(n in " ".join(w) for n in _NOISE[1:]) \
        and w != ["gracias"]


def wake_words():
    return [tuple(_plain(w)) for w in wake_label().split(",") if _plain(w)]


def strip_wake(text):
    """The question without its wake word, or None if the phrase doesn't
    start with one (talking to someone else, game audio, or
    a transcription of noise)."""
    raw = text.split()
    toks = [(i, w) for i, r in enumerate(raw) for w in _plain(r)]
    words = [w for _, w in toks]
    for ww in wake_words():
        n = len(ww)
        # first or second word, so "oye bono" works but not mid-sentence
        for j in range(0, min(2, len(words) - n + 1)):
            if all(_near(a, b) for a, b in zip(words[j:j + n], ww)) \
                    and len(words[j:j + n]) == n:
                rest = raw[toks[j + n - 1][0] + 1:]
                return " ".join(rest).strip(" ,.:;¿?¡!") or ""
    return None


def _request(driver, context, question, history, strategy, stream):
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise BrainError("falta ANTHROPIC_API_KEY")
    model = (os.environ.get("ANTHROPIC_STRATEGY_MODEL" if strategy
                            else "ANTHROPIC_MODEL", "").strip()
             or ("claude-sonnet-5-5" if strategy else "claude-haiku-5-5"))
    msgs = []
    for q, a in history[-8:]:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content":
                 "Telemetría ahora mismo:\n%s\n\nPiloto: %s" % (
                     json.dumps(context, ensure_ascii=False,
                                separators=(",", ":")), question)})
    # no extended thinking: on the radio it burned the whole token budget
    # before writing a word, so the driver got no answer at all
    body = {"model": model, "max_tokens": 1024, "stream": stream,
            "thinking": {"type": "disabled"},
            "system": SYSTEM.format(
                driver=driver or "el piloto",
                season="con el DLC de la temporada 2026" if context.get("season") == 2026
                else "(temporada 2025)",
                regs=REGS_2026 if context.get("season") == 2026 else REGS_2025),
            "messages": msgs}
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    req.model = model
    return req


_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def _open(driver, context, question, history, strategy):
    req = _request(driver, context, question, history, strategy, True)
    try:
        return req.model, urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        if e.code == 400 and "thinking" in body:
            # a model that doesn't take the switch: ask without it
            data = json.loads(req.data)
            data.pop("thinking", None)
            req.data = json.dumps(data).encode()
            return req.model, urllib.request.urlopen(req, timeout=30)
        raise BrainError("api.anthropic.com HTTP %d: %s" % (e.code, body))


def answer_stream(driver, context, question, history, strategy=False):
    """Claude's radio reply as it's written, one sentence at a time, so
    the first sentence can be voiced while the rest is still coming."""
    buf, stop = "", None
    used, model = {"input": 0, "output": 0}, None
    try:
        model, conn = _open(driver, context, question, history, strategy)
        with conn as resp:
            for line in resp:
                if not line.startswith(b"data:"):
                    continue
                ev = json.loads(line[5:])
                u = ((ev.get("message") or {}).get("usage") or ev.get("usage") or {})
                if u.get("input_tokens"):
                    used["input"] = (u["input_tokens"]
                                     + (u.get("cache_read_input_tokens") or 0)
                                     + (u.get("cache_creation_input_tokens") or 0))
                if u.get("output_tokens"):
                    used["output"] = u["output_tokens"]
                if ev.get("type") == "error":
                    raise BrainError("Claude: %s" % ev.get("error"))
                if ev.get("type") == "message_delta":
                    stop = (ev.get("delta") or {}).get("stop_reason")
                d = ev.get("delta") or {}
                if d.get("type") != "text_delta":
                    continue
                buf += re.sub(r"[*_#`]+", "", d.get("text", ""))
                while True:   # emit whole sentences, at least ~4 words
                    m = next((m for m in _SENTENCE_END.finditer(buf)
                              if m.end() >= 18), None)
                    if not m:
                        break
                    out, buf = buf[:m.end()].strip(), buf[m.end():]
                    if out:
                        yield out
    except urllib.error.HTTPError as e:
        raise BrainError("api.anthropic.com HTTP %d: %s" % (
            e.code, e.read()[:300].decode("utf-8", "replace")))
    except BrainError:
        raise
    except Exception as e:
        raise BrainError("api.anthropic.com: %r" % e)
    finally:
        if used["input"] or used["output"]:
            _report("llm", model=model, **used)
    if stop and stop != "end_turn":
        print("[engineer] Claude stop_reason: %s" % stop)
    # a reply cut off by the token limit ends mid-word: don't say that bit
    if buf.strip() and not (stop == "max_tokens" and
                            not _SENTENCE_END.search(buf + " ")):
        yield buf.strip()


def answer(driver, context, question, history, strategy=False):
    """Claude's whole radio reply. `history` = [(question, answer), ...]."""
    return " ".join(answer_stream(driver, context, question, history,
                                  strategy)).strip()
