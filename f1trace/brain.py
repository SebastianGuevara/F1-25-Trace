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
- Corto y directo, como una radio real de F1: 1 a 3 frases, unas 35 \
palabras como máximo, salvo que el piloto pida detalle.
- Tu texto se lee en voz alta: nada de markdown, listas, emojis ni \
símbolos raros. Números con coma decimal. Gaps en décimas ("tres \
décimas") o segundos ("1,4 segundos"). Tiempos de vuelta como "1 34,2". \
Posiciones como "P4".
- Tono de ingeniero: calmado, seguro, animando cuando toca.

Datos:
- on_track=false: el piloto está en el garaje, en boxes o en un menú; \
los datos son de la última vez que estuvo en pista. Puedes charlar, \
repasar la sesión y planear la siguiente salida.
- Usa SOLO la telemetría que te paso en cada mensaje. Si un dato no está, \
dilo en vez de inventarlo.
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
            if tuple(words[j:j + n]) == ww:
                rest = raw[toks[j + n - 1][0] + 1:]
                return " ".join(rest).strip(" ,.:;¿?¡!") or ""
    return None


def answer(driver, context, question, history, strategy=False):
    """Claude's radio reply. `history` = [(question, answer), ...]."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        raise BrainError("falta ANTHROPIC_API_KEY")
    model = (os.environ.get("ANTHROPIC_STRATEGY_MODEL" if strategy
                            else "ANTHROPIC_MODEL", "").strip()
             or ("claude-sonnet-5-5" if strategy else "claude-haiku-5-5"))
    msgs = []
    for q, a in history[-4:]:
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content":
                 "Telemetría ahora mismo:\n%s\n\nPiloto: %s" % (
                     json.dumps(context, ensure_ascii=False,
                                separators=(",", ":")), question)})
    res = _post("https://api.anthropic.com/v1/messages",
                json.dumps({"model": model, "max_tokens": 300,
                            "system": SYSTEM.format(driver=driver or "el piloto"),
                            "messages": msgs}).encode(),
                {"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"}, timeout=30)
    text = " ".join(b.get("text", "") for b in res.get("content", [])
                    if b.get("type") == "text").strip()
    return re.sub(r"[*_#`]+", "", text)
