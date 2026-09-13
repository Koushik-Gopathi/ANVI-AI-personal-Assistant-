"""ANVI - personal voice assistant backend.

Pipeline:  mic audio -> Deepgram STT -> Groq LLM (+ tools) -> Deepgram TTS -> browser
Run:       python main.py      then open http://127.0.0.1:8000
"""

import base64
import json
import os
import re
import ssl
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
load_dotenv(BASE_DIR / ".env")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-2")
TTS_VOICE = os.getenv("DEEPGRAM_TTS_VOICE", "aura-asteria-en")
ANVI_LOCATION = os.getenv("ANVI_LOCATION", "").strip()  # e.g. "Hyderabad"; blank = detect from IP
PORT = int(os.getenv("APP_PORT", "8000"))


# ---------------------------------------------------------------------------
# HTTP session
#
# Antivirus HTTPS scanning (Avast/AVG "Web Shield") re-signs every TLS
# connection with its own root CA. Windows trusts that CA but Python's
# certifi bundle does not, so every API call failed with
# CERTIFICATE_VERIFY_FAILED. Use the Windows certificate store instead, and
# relax Python 3.13+'s strict X.509 mode, which rejects the Avast root.
# ---------------------------------------------------------------------------
class _SystemTrustAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.load_default_certs()
        if hasattr(ssl, "VERIFY_X509_STRICT"):
            ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


http = requests.Session()
http.mount("https://", _SystemTrustAdapter())


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------
def transcribe(audio_bytes: bytes, mime_type: str) -> str:
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY missing in .env")

    response = http.post(
        "https://api.deepgram.com/v1/listen",
        params={"model": STT_MODEL, "smart_format": "true", "punctuate": "true", "language": "en"},
        headers={
            "Authorization": f"Token {DEEPGRAM_API_KEY}",
            # "audio/webm;codecs=opus" -> "audio/webm"
            "Content-Type": mime_type.split(";")[0].strip() or "audio/webm",
        },
        data=audio_bytes,
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return (
        data.get("results", {})
        .get("channels", [{}])[0]
        .get("alternatives", [{}])[0]
        .get("transcript", "")
    ).strip()


def clean_for_speech(text: str) -> str:
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"\*\*|__|`|#+\s?", "", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("°C", " degrees").replace("°F", " degrees Fahrenheit").replace("°", " degrees")
    return re.sub(r"\s+", " ", text).strip()


def synthesize(text: str) -> str:
    """Return base64-encoded MP3 for `text` (nothing is written to disk)."""
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY missing in .env")

    response = http.post(
        "https://api.deepgram.com/v1/speak",
        params={"model": TTS_VOICE},
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
        json={"text": clean_for_speech(text)[:1900]},
        timeout=60,
    )
    response.raise_for_status()
    return base64.b64encode(response.content).decode("ascii")


# ---------------------------------------------------------------------------
# Tools the assistant can call
# ---------------------------------------------------------------------------
_location_cache: dict | None = None

WEATHER_CODES = {
    0: "clear sky", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain", 67: "heavy freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers", 85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "severe thunderstorm with hail",
}


def home_location() -> dict:
    """User's default location: ANVI_LOCATION from .env, else a one-time IP lookup."""
    global _location_cache
    if _location_cache:
        return _location_cache
    if ANVI_LOCATION:
        _location_cache = geocode(ANVI_LOCATION)
    else:
        try:
            ip = http.get("http://ip-api.com/json/?fields=city,regionName,country,lat,lon", timeout=8).json()
            _location_cache = {
                "name": ", ".join(p for p in (ip.get("city"), ip.get("country")) if p),
                "lat": ip["lat"],
                "lon": ip["lon"],
            }
        except Exception as e:  # noqa: BLE001
            print("Location lookup failed:", e)
            _location_cache = {}
    return _location_cache


def geocode(place: str) -> dict:
    r = http.get(
        "https://geocoding-api.open-meteo.com/v1/search",
        params={"name": place, "count": 1, "language": "en"},
        timeout=10,
    )
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        return {}
    g = results[0]
    name = ", ".join(p for p in (g.get("name"), g.get("admin1"), g.get("country")) if p)
    return {"name": name, "lat": g["latitude"], "lon": g["longitude"]}


def get_weather(location: str = "") -> dict:
    loc = geocode(location) if location else home_location()
    if not loc:
        return {"error": f"Could not find location '{location}'" if location else "Location unknown; ask the user for a city."}

    r = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": loc["lat"],
            "longitude": loc["lon"],
            "current": "temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,sunrise,sunset",
            "timezone": "auto",
            "forecast_days": 3,
        },
        timeout=10,
    )
    r.raise_for_status()
    d = r.json()
    cur = d["current"]
    now = cur["time"]
    hourly = d["hourly"]
    start = next((i for i, t in enumerate(hourly["time"]) if t >= now[:13]), 0)
    next_hours = [
        {
            "time": hourly["time"][i][11:16],
            "temp_c": hourly["temperature_2m"][i],
            "rain_chance": hourly["precipitation_probability"][i],
            "sky": WEATHER_CODES.get(hourly["weather_code"][i], "unknown"),
        }
        for i in range(start, min(start + 24, len(hourly["time"])), 3)
    ]
    daily = d["daily"]
    days = [
        {
            "date": daily["time"][i],
            "sky": WEATHER_CODES.get(daily["weather_code"][i], "unknown"),
            "high_c": daily["temperature_2m_max"][i],
            "low_c": daily["temperature_2m_min"][i],
            "rain_chance": daily["precipitation_probability_max"][i],
            "sunset": daily["sunset"][i][11:16],
        }
        for i in range(len(daily["time"]))
    ]
    return {
        "location": loc["name"],
        "local_time": now,
        "current": {
            "temp_c": cur["temperature_2m"],
            "feels_like_c": cur["apparent_temperature"],
            "humidity": cur["relative_humidity_2m"],
            "wind_kmh": cur["wind_speed_10m"],
            "sky": WEATHER_CODES.get(cur["weather_code"], "unknown"),
        },
        "next_24h_every_3h": next_hours,
        "daily": days,
    }


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Real current weather and forecast (next 24h and 3 days). "
            "Always use this for any weather question instead of guessing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "City name. Leave empty for the user's current location.",
                    }
                },
            },
        },
    }
]
TOOL_FUNCS = {"get_weather": get_weather}


# ---------------------------------------------------------------------------
# Brain
# ---------------------------------------------------------------------------
history: list[dict] = []
history_lock = threading.Lock()
MAX_HISTORY = 16


def system_prompt() -> str:
    now = datetime.now().astimezone()
    loc = home_location().get("name") or "unknown"
    return (
        "You are ANVI, a smart, warm personal voice assistant. "
        "Your replies are spoken aloud, so answer in 1-3 short natural sentences. "
        "Never use markdown, lists, emojis, tables or URLs. "
        "Say numbers the way a person would (\"around 26 degrees\"). "
        "If you don't know something live (news, prices, scores), say so honestly instead of inventing it. "
        f"Current local date and time: {now:%A, %d %B %Y, %I:%M %p}. User's location: {loc}."
    )


def groq_chat(messages: list[dict], use_tools: bool = True) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing in .env")

    payload = {
        "model": GROQ_MODEL,
        "messages": messages,
        "temperature": 0.6,
        "max_completion_tokens": 1024,
    }
    if GROQ_MODEL.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    response = http.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
        json=payload,
        timeout=60,
    )
    if not response.ok:
        print("Groq error:", response.status_code, response.text[:500])
    response.raise_for_status()
    return response.json()["choices"][0]["message"]


def get_reply(user_text: str) -> str:
    with history_lock:
        history.append({"role": "user", "content": user_text})
        convo = [{"role": "system", "content": system_prompt()}, *history[-MAX_HISTORY:]]

    reply = ""
    for step in range(4):
        msg = groq_chat(convo, use_tools=step < 3)
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = (msg.get("content") or "").strip()
            break

        convo.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for call in calls:
            name = call["function"]["name"]
            try:
                args = json.loads(call["function"].get("arguments") or "{}")
                result = TOOL_FUNCS[name](**args)
            except Exception as e:  # noqa: BLE001
                result = {"error": str(e)}
            print(f"  tool {name}({call['function'].get('arguments')}) ->", json.dumps(result)[:200])
            convo.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})

    if not reply:
        reply = "Sorry, I lost my train of thought. Could you say that again?"

    with history_lock:
        history.append({"role": "assistant", "content": reply})
        del history[:-MAX_HISTORY]
    return reply


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="ANVI")


def api_error(e: Exception) -> JSONResponse:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        host = requests.utils.urlparse(e.response.url).hostname
        detail = f"{host} {e.response.status_code}: {e.response.text[:200]}"
    else:
        detail = str(e)
    print("ERROR:", detail)
    return JSONResponse({"error": detail}, status_code=502)


class ChatIn(BaseModel):
    text: str
    speak: bool = True


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model": GROQ_MODEL,
        "deepgram_key": bool(DEEPGRAM_API_KEY),
        "groq_key": bool(GROQ_API_KEY),
    }


# Plain `def` endpoints run in FastAPI's threadpool, so blocking HTTP calls
# don't freeze the server.
@app.post("/api/transcribe")
def api_transcribe(audio: UploadFile = File(...), mime_type: str = Form("audio/webm")):
    try:
        audio_bytes = audio.file.read()
        if len(audio_bytes) < 1000:
            return {"transcript": ""}
        transcript = transcribe(audio_bytes, mime_type)
        print(f"\nYOU  > {transcript}")
        return {"transcript": transcript}
    except Exception as e:  # noqa: BLE001
        return api_error(e)


@app.post("/api/chat")
def api_chat(body: ChatIn):
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    try:
        reply = get_reply(text)
        print(f"ANVI > {reply}")
        audio = synthesize(reply) if body.speak else None
        return {"reply": reply, "audio": audio}
    except Exception as e:  # noqa: BLE001
        return api_error(e)


@app.post("/api/reset")
def api_reset():
    with history_lock:
        history.clear()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")


if __name__ == "__main__":
    import uvicorn

    url = f"http://127.0.0.1:{PORT}"
    print("=" * 52)
    print(f"  ANVI online  ->  {url}")
    print(f"  brain: {GROQ_MODEL}   voice: {TTS_VOICE}")
    print("=" * 52)
    if os.getenv("ANVI_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
