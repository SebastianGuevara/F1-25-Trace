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

SYSTEM = """Eres el ingeniero de carrera de {driver} en el simulador F1 25. \
Hablas por la radio del equipo, en español.

Cómo hablas:
- Es una conversación de radio entre tú y tu piloto, no un informe. \
Habla natural y cercano, como un ingeniero que conoce bien a su piloto: \
responde a lo que te dice (también si solo charla, bromea o se queja), \
recuerda lo que hablasteis antes y sigue el hilo. Puedes preguntarle \
algo de vuelta cuando ayude ("¿cómo notas el tren trasero?").
- Breve porque está conduciendo: normalmente 1 o 2 frases. La primera \
frase ya debe contestar lo importante; el detalle, después. Más largo \
solo si te lo pide o está en el garaje.
- Tu texto se lee en voz alta: nada de markdown, listas, emojis ni \
símbolos raros. Números con coma decimal. Gaps en décimas ("tres \
décimas") o segundos ("1,4 segundos"). Tiempos de vuelta como "1 34,2". \
Posiciones como "P4".
- Tono de ingeniero: calmado, seguro, animando cuando toca.

Datos:
- on_track=false: el piloto está en el garaje, en boxes o en un menú; \
los datos son de la última vez que estuvo en pista. Puedes charlar, \
repasar la sesión y planear la siguiente salida.
- Los números concretos (tiempos, gaps, temperaturas, desgaste, gasolina) \
salen SOLO de la telemetría que te paso; no inventes cifras.
- Responde SIEMPRE algo útil. Nunca contestes solo "no tengo ese dato". \
Si falta un dato: dilo en pocas palabras y da lo mejor que puedas — una \
estimación con lo que sí tienes (tendencias del historial, el campo, el \
desgaste por vuelta), o tu conocimiento de ingeniero de F1 y de F1 25 \
(cómo atacar una curva del circuito, ajustes de reglaje, estrategia \
típica, gestión de neumáticos y ERS), dejando claro que es una \
recomendación general.
- La pregunta viene de un reconocimiento de voz y puede traer palabras \
mal escritas: interpreta lo más probable. Si de verdad no se entiende, \
pide que la repita en una frase corta.
- field: toda la parrilla (posición, nombre, equipo, gaps, última vuelta, \
mejor vuelta vista, neumático y su edad, paradas); "me" eres tú. Úsalo \
para rivales, ritmo comparado y quién ha parado.
- my_setup: el reglaje del coche del piloto (alerones, diferencial, \
suspensión, presiones, reparto de frenada...). assists: sus ayudas.
- Neumáticos en orden delantero izquierdo, delantero derecho, trasero \
izquierdo, trasero derecho. Ventana de temperatura de superficie: slicks \
85-105 °C, intermedios 60-80, lluvia 50-70.
- fuel.laps_surplus es el objetivo de gasolina del juego: positivo = \
vueltas de sobra, negativo = vamos cortos.
- pit_window: vuelta ideal y última para parar; rejoin = posición \
estimada al salir de boxes.
- forecast: minutos desde ahora, tiempo (0 despejado, 1 nubes, 2 \
cubierto, 3 lluvia ligera, 4 lluvia fuerte, 5 tormenta) y % de lluvia.
- history: vueltas anteriores (tiempo, posición, gasolina, desgaste \
máximo, gaps). Úsalo para tendencias: ritmo, desgaste por vuelta, \
consumo, si recortas o pierdes con el de delante.
- Para estrategia, razona con desgaste por vuelta, vueltas restantes, \
ventana de parada, posición al salir y pronóstico, y da una \
recomendación concreta."""


class BrainError(Exception):
    pass


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
            "system": SYSTEM.format(driver=driver or "el piloto"),
            "messages": msgs}
    return urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(body).encode(),
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})


_SENTENCE_END = re.compile(r"[.!?…](?=\s)")


def _open(driver, context, question, history, strategy):
    req = _request(driver, context, question, history, strategy, True)
    try:
        return urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        if e.code == 400 and "thinking" in body:
            # a model that doesn't take the switch: ask without it
            data = json.loads(req.data)
            data.pop("thinking", None)
            req.data = json.dumps(data).encode()
            return urllib.request.urlopen(req, timeout=30)
        raise BrainError("api.anthropic.com HTTP %d: %s" % (e.code, body))


def answer_stream(driver, context, question, history, strategy=False):
    """Claude's radio reply as it's written, one sentence at a time, so
    the first sentence can be voiced while the rest is still coming."""
    buf, stop = "", None
    try:
        with _open(driver, context, question, history, strategy) as resp:
            for line in resp:
                if not line.startswith(b"data:"):
                    continue
                ev = json.loads(line[5:])
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
