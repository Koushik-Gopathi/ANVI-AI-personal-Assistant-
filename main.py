"""Karen - personal voice assistant backend.

Pipeline:  mic audio -> Deepgram STT -> Groq LLM (+ tools, streamed)
           -> sentence-by-sentence Deepgram TTS -> browser
Run:       python main.py      then open http://127.0.0.1:8000
"""

import asyncio
import base64
import json
import os
import queue
import random
import re
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# BASE_DIR holds .env, .anvi/ and generated/; WEB_DIR the UI files (bundled inside Karen.exe when packaged)
BASE_DIR = Path(os.getenv("ANVI_HOME") or Path(__file__).resolve().parent)
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
load_dotenv(BASE_DIR / ".env")  # before importing modules that read the environment

import requests  # noqa: E402
from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import agent  # noqa: E402
import docs  # noqa: E402
import pc  # noqa: E402
import phone  # noqa: E402
import search  # noqa: E402
import store  # noqa: E402
import vision  # noqa: E402
import weather  # noqa: E402
from net import friendly_error, http  # noqa: E402

# Windows consoles default to cp1252; don't crash when printing ₹ or Telugu
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
FALLBACK_MODEL = os.getenv("GROQ_FALLBACK_MODEL", "openai/gpt-oss-20b")
STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
STT_LANGUAGE = os.getenv("DEEPGRAM_STT_LANGUAGE", "en-IN")  # Indian English: far fewer mis-hearings
TTS_VOICE = os.getenv("DEEPGRAM_TTS_VOICE", "aura-asteria-en")
PORT = int(os.getenv("APP_PORT", "8000"))
SARVAM_API_KEY = os.getenv("SARVAM_API_KEY", "").strip()  # Telugu/Hindi voice
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()  # pay-as-you-go brain (optional)

# Which brain Karen thinks with. Groq is free but limited; OpenRouter costs a few cents per hundred
# requests and is much better at long multi-step jobs. Switched in Settings.
BRAINS = {
    "groq": {
        "label": "Groq (free)",
        "url": "https://api.groq.com/openai/v1/chat/completions",
        "models": [GROQ_MODEL, FALLBACK_MODEL],
        "vision": os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
    },
    "openrouter_free": {
        "label": "OpenRouter (free models)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [os.getenv("OPENROUTER_FREE_MODEL", "qwen/qwen3.8-27b:free"),
                   os.getenv("OPENROUTER_FREE_FALLBACK_MODEL", "google/gemma-4-26b-a4b-it:free")],
        "vision": "qwen/qwen3.8-27b:free",
    },
    "mix": {
        "label": "Smart mix (free chat, Haiku for work)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [],  # filled per request by turn_models()
        "vision": os.getenv("GROQ_VISION_MODEL", "qwen/qwen3.8-27b"),
    },
    "openrouter": {
        "label": "OpenRouter (paid, best)",
        "url": "https://openrouter.ai/api/v1/chat/completions",
        "models": [os.getenv("OPENROUTER_MODEL", "anthropic/claude-haiku-4.5"),
                   os.getenv("OPENROUTER_FALLBACK_MODEL", "google/gemini-3.1-flash-lite")],
        "vision": os.getenv("OPENROUTER_VISION_MODEL", "qwen/qwen3.8-27b:free"),
    },
}


def brain_name() -> str:
    """The chosen brain, falling back to Groq when OpenRouter has no key."""
    chosen = store.settings().get("brain", "groq")
    if chosen in ("openrouter", "openrouter_free", "mix") and not OPENROUTER_API_KEY:
        return "groq"
    if chosen == "mix" and not GROQ_API_KEY:
        return "openrouter"
    return chosen if chosen in BRAINS else "groq"


def turn_models(wants_action: bool) -> list[tuple[str, str]]:
    """(provider, model) to try for this turn, best first.

    The smart mix keeps the free Groq models for chat and questions, and pays for Claude only when
    the user actually asked for something to be done - each is the other's fallback, so a busy or
    rate-limited model never stops the turn.
    """
    name = brain_name()
    if name != "mix":
        return [(name, m) for m in BRAINS[name]["models"] if m]
    free = [("groq", m) for m in BRAINS["groq"]["models"]]
    paid = [("openrouter", m) for m in BRAINS["openrouter"]["models"]]
    return (paid + free) if wants_action else (free + paid)


def brain() -> dict:
    return BRAINS[brain_name()]


def brain_key(name: str = "") -> str:
    name = name or brain_name()
    return OPENROUTER_API_KEY if name.startswith("openrouter") else (GROQ_API_KEY or "")
PHONE_ENABLED = os.getenv("ANVI_PHONE", "1") == "1"
PHONE_PORT = int(os.getenv("ANVI_PHONE_PORT", "8443"))
# public HTTPS address that forwards to this PC, e.g. https://my-laptop.tail1234.ts.net (Tailscale)
PUBLIC_URL = os.getenv("ANVI_PUBLIC_URL", "").strip().rstrip("/")


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------
def transcribe(audio_bytes: bytes, mime_type: str, purpose: str = "") -> str:
    """Deepgram for English and for spotting the wake word; Groq Whisper for Telugu/Hindi/any language."""
    settings = store.settings()
    language = store.LANGUAGES[settings["language"]]
    content_type = mime_type.split(";")[0].strip() or "audio/webm"  # "audio/webm;codecs=opus" -> "audio/webm"

    if purpose != "wake" and settings["language"] != "english":
        ext = {"audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "m4a", "audio/wav": "wav",
               "audio/mpeg": "mp3"}.get(content_type, "webm")
        form = {"model": "whisper-large-v3", "response_format": "json", "prompt": settings["wake_word"]}
        if language["code"]:
            form["language"] = language["code"]
        r = http.post("https://api.groq.com/openai/v1/audio/transcriptions",
                      headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                      files={"file": (f"speech.{ext}", audio_bytes, content_type)}, data=form, timeout=60)
        r.raise_for_status()
        return (r.json().get("text") or "").strip()

    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY missing in .env")
    params = {"model": STT_MODEL, "smart_format": "true", "punctuate": "true", "language": STT_LANGUAGE}
    # help it spell the wake/sleep word
    if STT_MODEL.startswith("nova-3"):
        params["keyterm"] = settings["wake_word"]
    else:
        params["keywords"] = f"{settings['wake_word']}:2"

    response = http.post(
        "https://api.deepgram.com/v1/listen",
        params=params,
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": content_type},
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


INDIC_SCRIPTS = [(re.compile(r"[\u0C00-\u0C7F]"), "te-IN"), (re.compile(r"[\u0900-\u097F]"), "hi-IN")]


def indic_language(text: str) -> str:
    """BCP-47 code if the text is written in Telugu or Hindi script, else ''."""
    return next((code for rx, code in INDIC_SCRIPTS if rx.search(text)), "")


def clean_for_speech(text: str) -> str:
    text = text.replace(" ", " ").replace(" ", " ")
    text = re.sub(r"\*\*|__|`|#+\s?", "", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    lang = indic_language(text)
    degrees, rupees = {"te-IN": (" డిగ్రీలు", "రూపాయలు "), "hi-IN": (" डिग्री", "रुपये ")}.get(lang, (" degrees", "rupees "))
    text = text.replace("°C", degrees).replace("°F", degrees + " Fahrenheit").replace("°", degrees)
    text = re.sub(r"₹\s?", rupees, text)
    return re.sub(r"\s+", " ", text).strip()


def synthesize(text: str):
    """Base64 MP3 for `text` (nothing is written to disk).

    Telugu/Hindi text goes to Sarvam AI when SARVAM_API_KEY is set; without it a
    {"say", "lang"} dict is returned so the device speaks it with its own voice.
    """
    lang = indic_language(text)
    if lang:
        if not SARVAM_API_KEY:
            return {"say": clean_for_speech(text), "lang": lang}
        r = http.post("https://api.sarvam.ai/text-to-speech", headers={"api-subscription-key": SARVAM_API_KEY},
                      json={"text": clean_for_speech(text)[:2400], "language_code": lang, "model": "bulbul:v3",
                            "speaker": "priya", "output_audio_codec": "mp3"}, timeout=30)
        r.raise_for_status()
        return r.json()["audios"][0]
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY missing in .env")

    response = http.post(
        "https://api.deepgram.com/v1/speak",
        params={"model": store.settings()["voice"]},
        headers={"Authorization": f"Token {DEEPGRAM_API_KEY}", "Content-Type": "application/json"},
        json={"text": clean_for_speech(text)[:1900]},
        timeout=30,
    )
    response.raise_for_status()
    return base64.b64encode(response.content).decode("ascii")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def _tool(name: str, description: str, props: dict | None = None, required: tuple = ()) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description,
        "parameters": {"type": "object", "properties": props or {}, "required": list(required)},
    }}


def _str(description: str) -> dict:
    return {"type": "string", "description": description}


def _num(description: str) -> dict:
    return {"type": "number", "description": description}


def _bool(description: str) -> dict:
    return {"type": "boolean", "description": description}


PATH = _str("full path, or a path starting with desktop/, documents/ or downloads/")

TOOLS = [
    # information
    _tool("web_search", "Search the internet and read the top pages. Use for anything current or uncertain: "
          "prices/rates, news, sports, events, releases, people's current roles, how-to questions.",
          {"query": _str("search query; add city/country and 'today' when relevant")}, ("query",)),
    _tool("get_news", "Latest news headlines, optionally on a topic.", {"topic": _str("topic, or empty for top stories")}),
    _tool("get_weather", "Real weather and forecast.", {"location": _str("city; empty = user's location")}),
    _tool("speed_test", "Measure this PC's internet download/upload speed and ping (takes ~15 s)."),
    _tool("system_info", "PC battery, CPU, RAM and disk usage."),
    # doing work
    _tool("run_command", "Run a PowerShell command on the PC and get its output. Use it for anything the other "
          "tools can't do: Wi-Fi/network info, installed programs, processes, git, python scripts, zipping, "
          "downloading files, settings. Risky commands ask the user first.",
          {"command": _str("PowerShell command (not wrapped in powershell -Command)"),
           "timeout_seconds": _num("default 60, max 600")}, ("command",)),
    _tool("list_folder", "List files and folders in a folder.", {"path": PATH}),
    _tool("read_file", "Read a text file.", {"path": PATH}, ("path",)),
    _tool("write_file", "Create or overwrite a text file (notes, code, lists, reports). Overwriting asks first.",
          {"path": PATH, "content": _str("full text"), "append": _bool("add to the end instead")}, ("path", "content")),
    _tool("create_folder", "Create a folder (and parents).", {"path": PATH}, ("path",)),
    _tool("move_path", "Move, rename or copy a file or folder.",
          {"source": PATH, "destination": PATH, "copy": _bool("copy instead of move")}, ("source", "destination")),
    _tool("delete_path", "Move a file or folder to the Recycle Bin (asks the user first).", {"path": PATH}, ("path",)),
    _tool("find_files", "Find files/folders by name in the user's Desktop, Documents, Downloads, Pictures, Music, "
          "Videos.", {"name": _str("words in the name")}, ("name",)),
    _tool("open_path", "Open a file or folder with its default app.", {"path": PATH}, ("path",)),
    _tool("read_document", "Read the text of a Word (.docx), PDF, PowerPoint (.pptx) or text file, e.g. to "
          "summarise it or answer questions. Long files come in parts: call again with next_start.",
          {"path": PATH, "start": _num("character offset from a previous call's next_start")}, ("path",)),
    _tool("look_at_screen", "Look at the PC screen right now and answer a question about it (read an error, "
          "describe a page, check what's open). Only when the user asks you to look at / read the screen.",
          {"question": _str("what the user wants to know about the screen")}),
    _tool("describe_image", "Look at an image file (photo, screenshot, scanned page) and answer a question about it.",
          {"path": PATH, "question": _str("what the user wants to know")}, ("path",)),
    _tool("create_document", "Create a real Word (.docx) or PDF document. Write content in simple markdown: "
          "# heading, ## subheading, - bullet, 1. numbered, **bold**, blank line between paragraphs.",
          {"path": _str("file path ending in .docx or .pdf"), "title": _str("document title"),
           "content": _str("the document body")}, ("path", "content")),
    _tool("organize_folder", "Tidy a folder by moving its loose files into Documents/Images/Videos/Audio/Archives/"
          "Installers/Code/Others subfolders (asks first).", {"path": PATH}),
    _tool("open_app", "Open an app on the PC.", {"name": _str("app name, e.g. chrome, whatsapp, notepad, settings")},
          ("name",)),
    _tool("close_app", "Close an app when the user says close/quit it; force=true kills a frozen app (asks first).",
          {"name": _str("app name"), "force": _bool("force-close a frozen/unresponsive app")}, ("name",)),
    _tool("open_website", "Open a website or a Google search in a browser.",
          {"url_or_query": _str("URL/domain or search text"),
           "browser": _str("chrome, edge, firefox or brave when the user names one; empty = default browser")},
          ("url_or_query",)),
    _tool("play_youtube", "Play a song or video on YouTube.",
          {"query": _str("song or video"), "browser": _str("browser the user named, if any")}, ("query",)),
    _tool("list_windows", "List the titles of open windows."),
    _tool("focus_window", "Bring an open window to the front.", {"title": _str("part of the window title")}, ("title",)),
    _tool("type_text", "Type text into an app, as if on the keyboard. Open/focus the app first.",
          {"text": _str("text to type; newlines press Enter"), "window": _str("part of the target window title")},
          ("text",)),
    _tool("press_keys", "Press a key or shortcut in an app, e.g. ctrl+s, enter, alt+tab, ctrl+shift+n.",
          {"keys": _str("keys joined with +"), "window": _str("part of the target window title"),
           "times": _num("repeat count")}, ("keys",)),
    _tool("get_clipboard", "Read text on the clipboard."),
    _tool("set_clipboard", "Put text on the clipboard.", {"text": _str("text")}, ("text",)),
    _tool("set_reminder", "Show a reminder popup on the PC after some minutes.",
          {"minutes": _num("minutes from now (0.5 = 30 seconds)"), "message": _str("reminder text")},
          ("minutes", "message")),
    _tool("list_reminders", "List pending reminders."),
    _tool("set_volume", "Set the PC volume level (0-100) or action.",
          {"level": {"type": "integer", "description": "0-100"}, "action": {"type": "string", "enum": ["up", "down", "mute"]}}),
    _tool("media_control", "Play/pause, next, previous or stop music and videos.",
          {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, ("action",)),
    _tool("take_screenshot", "Save a screenshot to Pictures. ONLY when the user asks for a screenshot; you can't see it."),
    _tool("lock_pc", "Lock the PC, only when the user asks."),
    _tool("power_action", "Shut down, restart or sleep the PC, or cancel a pending shutdown (asks first).",
          {"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "cancel"]}}, ("action",)),
    _tool("remember", "Save a fact the user wants you to remember permanently (people, dates, preferences).",
          {"fact": _str("the fact, written as a full sentence")}, ("fact",)),
    _tool("forget", "Delete a remembered fact.", {"fact": _str("words from the fact to forget")}, ("fact",)),
    _tool("daily_briefing", "Morning/daily briefing: weather, top news, reminders and remembered dates. Use for "
          "'good morning', 'brief me', 'what's my day'."),
]


def daily_briefing() -> dict:
    out: dict = {"date": f"{datetime.now():%A, %d %B %Y}"}
    try:
        w = weather.get_weather()
        out["weather"] = {"location": w["location"], "now": w["current"], "today": w["daily"][0]}
    except Exception as e:  # noqa: BLE001
        out["weather"] = f"unavailable ({e})"
    try:
        out["top_news"] = [a["headline"] for a in search.get_news()["articles"][:5]]
    except Exception as e:  # noqa: BLE001
        out["top_news"] = f"unavailable ({e})"
    out["reminders"] = agent.list_reminders()["reminders"]
    out["remembered"] = [m["fact"] for m in store.memories()[-15:]]
    out["how_to_answer"] = ("A warm spoken briefing under 90 words: greeting, weather, 2-3 headlines, and any "
                            "reminders or remembered dates coming up soon.")
    return out


def power_action(action: str, confirmed: bool = False) -> dict:
    if action != "cancel" and not confirmed:
        return {"needs_confirmation": True, "will_do": f"{action} the PC"}
    return pc.power_action(action)


TOOL_FUNCS = {
    "web_search": search.web_search, "get_news": search.get_news, "get_weather": weather.get_weather,
    "speed_test": agent.speed_test, "system_info": pc.system_info,
    "run_command": agent.run_command, "list_folder": agent.list_folder, "read_file": agent.read_file,
    "write_file": agent.write_file, "create_folder": agent.create_folder, "move_path": agent.move_path,
    "delete_path": agent.delete_path, "find_files": pc.find_files, "open_path": pc.open_path,
    "open_app": pc.open_app, "close_app": pc.close_app, "open_website": pc.open_website,
    "play_youtube": pc.play_youtube, "list_windows": agent.list_windows, "focus_window": agent.focus_window,
    "type_text": agent.type_text, "press_keys": agent.press_keys, "get_clipboard": agent.get_clipboard,
    "set_clipboard": agent.set_clipboard, "set_reminder": agent.set_reminder, "list_reminders": agent.list_reminders,
    "set_volume": pc.set_volume, "media_control": pc.media_control, "take_screenshot": pc.take_screenshot,
    "lock_pc": pc.lock_pc, "power_action": power_action,
    "read_document": docs.read_document, "create_document": docs.create_document,
    "organize_folder": docs.organize_folder, "remember": store.remember, "forget": store.forget,
    "daily_briefing": daily_briefing, "look_at_screen": vision.look_at_screen,
    "describe_image": vision.describe_image,
}
TOOL_PARAMS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in TOOLS}
# tools that return {"needs_confirmation"} until called again after the user says yes
CONFIRMABLE = {"run_command", "write_file", "move_path", "delete_path", "power_action", "create_document",
               "organize_folder", "close_app"}


def _short(value, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


TOOL_STATUS = {
    "web_search": lambda a: f"searching: {a.get('query', '')}",
    "get_news": lambda a: f"checking news{': ' + a['topic'] if a.get('topic') else ''}",
    "get_weather": lambda a: "checking the weather",
    "read_document": lambda a: f"reading {Path(str(a.get('path', 'document'))).name}",
    "create_document": lambda a: f"creating {Path(str(a.get('path', 'document'))).name}",
    "organize_folder": lambda a: f"organising {_short(a.get('path', 'downloads'), 30)}",
    "remember": lambda a: "remembering that",
    "forget": lambda a: "forgetting that",
    "daily_briefing": lambda a: "getting your briefing",
    "look_at_screen": lambda a: "looking at your screen",
    "describe_image": lambda a: f"looking at {Path(str(a.get('path', 'image'))).name}",
    "speed_test": lambda a: "running a speed test",
    "system_info": lambda a: "checking your PC",
    "run_command": lambda a: f"running: {_short(a.get('command', ''), 40)}",
    "list_folder": lambda a: f"looking in {_short(a.get('path', 'desktop'), 30)}",
    "read_file": lambda a: f"reading {Path(str(a.get('path', 'file'))).name}",
    "write_file": lambda a: f"writing {Path(str(a.get('path', 'file'))).name}",
    "create_folder": lambda a: f"creating {Path(str(a.get('path', 'folder'))).name}",
    "move_path": lambda a: "copying" if a.get("copy") else "moving files",
    "delete_path": lambda a: f"deleting {Path(str(a.get('path', 'it'))).name}",
    "find_files": lambda a: f"looking for {a.get('name', 'files')}",
    "open_app": lambda a: f"opening {a.get('name', 'app')}",
    "close_app": lambda a: f"closing {a.get('name', 'app')}",
    "open_website": lambda a: "opening browser",
    "play_youtube": lambda a: f"finding {a.get('query', '')} on youtube",
    "type_text": lambda a: "typing",
    "press_keys": lambda a: f"pressing {a.get('keys', 'keys')}",
    "set_reminder": lambda a: "setting a reminder",
    "power_action": lambda a: f"{a.get('action', 'power')} requested",
}

CONFIRM_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|confirm(ed)?|do it|go ahead|proceed|okay|ok|haan|ha)\b", re.I)
_pending: dict[tuple[str, str, str], dict] = {}

# Actions the model must not take on its own initiative: they only run when the
# user's own words in this turn ask for them (the model sometimes grabs a
# screenshot "to check its work", or closes apps it opened earlier).
EXPLICIT_ONLY = {
    "take_screenshot": re.compile(r"screen\s*shot|screen\s*grab|snap\s*shot|\bss\b|capture (the |my )?screen|print\s*screen", re.I),
    "lock_pc": re.compile(r"\block", re.I),
    "look_at_screen": re.compile(r"screen|\blook|\bsee\b|\bshowing\b|\bmonitor\b|\bdisplay\b|what'?s (open|this)"
                                 r"|read (this|that|the)", re.I),
    "close_app": re.compile(r"\b(close|quit|exit|kill|stop|end|terminate)\b", re.I),
}


def _args_key(name: str, args: dict) -> tuple[str, str]:
    return name, json.dumps({k: v for k, v in args.items() if k != "confirmed"}, sort_keys=True, ensure_ascii=False)


def run_tool(name: str, args: dict, ctx: dict) -> dict:
    # keep only arguments the tool declares: models send empty strings and junk keys like {"": {}}
    allowed = TOOL_PARAMS.get(name, set())
    args = {k: v for k, v in args.items() if k in allowed and v not in (None, "")}
    guard = EXPLICIT_ONLY.get(name)
    if guard and not guard.search(ctx["user_text"]):
        return {"error": "Not done: the user did not ask for this. Only use this tool when explicitly asked."}
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"unknown tool {name}"}
    try:
        if name not in CONFIRMABLE:
            return fn(**args)
        key = (ctx.get("source", "pc"), *_args_key(name, args))
        pending = _pending.get(key)
        said_yes = bool(CONFIRM_RE.search(ctx["user_text"])) and len(ctx["user_text"].split()) <= 8
        confirmed = said_yes and (
            bool(pending and pending["turn"] < ctx["turn"] and time.time() - pending["at"] < 300)
            or _was_asked_about(name, args, ctx.get("last_reply", ""))
        )
        result = fn(**args, confirmed=confirmed)
        if isinstance(result, dict) and result.get("needs_confirmation"):
            _pending[key] = {"turn": ctx["turn"], "at": time.time()}
            result["instruction"] = ("Stop and ask the user to confirm, saying exactly what will happen. After they "
                                     "say yes, call this tool again with exactly the same arguments.")
        else:
            _pending.pop(key, None)
        return result
    except TypeError as e:
        return {"error": f"wrong arguments for {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


def _was_asked_about(name: str, args: dict, last_reply: str) -> bool:
    """Did Karen's previous reply ask the user about this exact action?"""
    reply = last_reply.lower()
    if "?" not in reply:
        return False
    targets = [Path(str(args[k])).name for k in ("path", "source", "destination") if args.get(k)]
    if name == "run_command" and args.get("command"):
        targets.append(str(args["command"]).split()[0])
    if name == "power_action" and args.get("action"):
        targets.append(str(args["action"]))
    return any(t and t.lower() in reply for t in targets)


def step_summary(name: str, args: dict, result: dict) -> dict:
    """One line for the activity list in the UI."""
    label = TOOL_STATUS.get(name, lambda a: name.replace("_", " "))(args)
    if result.get("needs_confirmation"):
        outcome = "waiting for your OK"
    elif result.get("error"):
        outcome = "failed"
    elif name == "run_command" and result.get("exit_code") not in (0, None):
        outcome = f"exit code {result.get('exit_code')}"
    else:
        outcome = "done"
    return {"text": label, "outcome": outcome}


# ---------------------------------------------------------------------------
# Brain (Groq, streamed)
# ---------------------------------------------------------------------------
history: list[dict] = []
history_lock = threading.Lock()
MAX_HISTORY = 16
MAX_STEPS = 12
TOKEN_LIMIT = 7600  # Groq free tier: ~8k tokens/minute per model, prompt + completion
INPUT_BUDGET = 5400
_turn_counter = 0
# each model has its own per-minute token allowance, so an agent task can move between them



LANGUAGE_RULES = {
    "english": "Reply in English. ",
    "telugu": "Reply in Telugu, written in Telugu script, unless the user clearly wants English. Keep names, "
              "file names and technical terms as they are. ",
    "hindi": "Reply in Hindi, written in Devanagari script, unless the user clearly wants English. Keep names, "
             "file names and technical terms as they are. ",
    "auto": "Reply in the same language the user used (Telugu in Telugu script, Hindi in Devanagari). ",
}


def system_prompt(on_phone: bool = False) -> str:
    loc = weather.home_location().get("name") or "unknown"
    folders = agent.known_folders()
    settings = store.settings()
    return (
        f"You are {settings['wake_word']}, the user's personal AI agent running on their Windows PC. You don't just "
        "chat: you get work done with your tools - running PowerShell commands, managing files and folders, reading "
        "and creating Word/PDF documents, opening apps and websites, typing and pressing keys in apps, speed tests, "
        "reminders, web search, and remembering things. "
        "For a task: work out the steps, call tools one after another, read every result, fix errors by trying "
        "another way, and keep going until the task is really finished. Don't ask permission for normal steps and "
        "don't stop halfway to report progress. If a tool result says needs_confirmation, stop and ask the user, "
        "describing exactly what will happen. Never claim you did something unless a tool result shows it worked; "
        "if something truly can't be done, say so plainly. Only take PC actions the user asked for. "
        "For anything that changes over time or that you're unsure of - prices, news, sports, releases, people's "
        "roles - search the web first and answer with the specific facts and numbers you found. "
        "Your final reply is spoken aloud: 1-3 short natural sentences (under 60 words unless the user asks for "
        "detail) summarising the result, with no markdown, lists, emojis, tables or URLs. Write numbers as digits "
        "with units (like ₹15,458 or 27°C). "
        "When the user asks for code to read, write complete code in fenced code blocks with the language and a "
        "filename on the opening fence line, like ```python hello.py; it is shown on screen, so outside the code "
        "write one short sentence. When they want a file created, use write_file (or create_document for Word/PDF). "
        "When the user tells you something to remember, use remember. "
        + LANGUAGE_RULES[settings["language"]]
        + store.memory_prompt()
        + f"User's folders: Desktop {folders['desktop']}, Documents {folders['documents']}, "
        f"Downloads {folders['downloads']}. "
        + ("The user is talking to you from their phone, not at the PC: PC actions happen on the PC, so say "
           "'on your PC'. " if on_phone else "")
        + f"The user's location is {loc}."
    )


def time_note() -> dict:
    """The clock, kept out of the system prompt so its start never changes (that's what Claude caches)."""
    return {"role": "system", "content": f"Current local date and time: {datetime.now().astimezone():%A, %d %B %Y, %I:%M %p}."}


TOOLS_TOKENS = len(json.dumps(TOOLS)) // 4


def est_tokens(messages: list[dict]) -> int:
    return sum(len(json.dumps(m, ensure_ascii=False)) for m in messages) // 3 + TOOLS_TOKENS


def build_convo(on_phone: bool = False) -> list[dict]:
    msgs = [dict(m) for m in history[-MAX_HISTORY:]]
    last_assistant = max((i for i, m in enumerate(msgs) if m["role"] == "assistant"), default=-1)
    for i, m in enumerate(msgs):
        if m["role"] == "assistant" and i != last_assistant:
            m["content"] = FENCE.sub("[code omitted]", m["content"])
    system = {"role": "system", "content": system_prompt(on_phone)}
    clock = time_note()
    while len(msgs) > 1 and est_tokens([system, clock, *msgs]) > INPUT_BUDGET:
        msgs.pop(0)
    return [system, clock, *msgs]


def compact_convo(convo: list[dict], turn_start: int) -> None:
    """Keep a long agent task under the token budget: shorten old tool results, then drop old chat."""
    if est_tokens(convo) <= INPUT_BUDGET:
        return
    tool_msgs = [m for m in convo[turn_start:] if m["role"] == "tool"]
    for m in tool_msgs[:-2]:
        if len(m["content"]) > 300:
            m["content"] = m["content"][:300] + "… [shortened]"
    while est_tokens(convo) > INPUT_BUDGET and turn_start > 2:
        del convo[1]  # oldest message after the system prompt
        turn_start -= 1


class RetryableGroqError(Exception):
    def __init__(self, message: str, wait: float = 0):
        super().__init__(message)
        self.wait = wait


def _retry_after(r) -> float:
    header = r.headers.get("retry-after")
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    m = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", r.text)
    return (int(m.group(1) or 0) * 60 + float(m.group(2))) if m else 20.0


def groq_stream(messages: list[dict], use_tools: bool, model: str, provider: str = ""):
    """One streamed reply from a brain (Groq and OpenRouter both speak the OpenAI format)."""
    name = provider or brain_name()
    key = brain_key(name)
    if not key:
        raise RuntimeError(("OPENROUTER_API_KEY" if name == "openrouter" else "GROQ_API_KEY") + " missing in .env")

    on_groq = name == "groq"
    spend = {}
    limit = max(600, min(3000, TOKEN_LIMIT - est_tokens(messages))) if on_groq else 3000
    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        ("max_completion_tokens" if on_groq else "max_tokens"): limit,
        "stream": True,
    }
    if not on_groq:
        payload["usage"] = {"include": True}  # so Karen can show what a request cost
    if on_groq and model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    elif on_groq and model.startswith("qwen/"):
        payload["reasoning_format"] = "hidden"
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    if model.startswith("anthropic/"):
        # Claude can cache the unchanging start of the prompt (these tool definitions and the system
        # prompt): about a tenth of the price on every later request.
        payload["cache_control"] = {"type": "ephemeral"}
    headers = {"Authorization": f"Bearer {key}"}
    if not on_groq:  # OpenRouter shows these on your activity page
        headers |= {"HTTP-Referer": "https://github.com/Koushik-Gopathi/ANVI-AI-personal-Assistant-", "X-Title": "Karen"}
    r = http.post(BRAINS[name]["url"], headers=headers, json=payload, stream=True, timeout=(15, 60))
    if r.status_code in (413, 429, 503) or (r.status_code == 400 and "tool_use_failed" in r.text):
        wait = _retry_after(r) if r.status_code == 429 else 0
        body = r.text[:200]
        r.close()
        raise RetryableGroqError(f"{r.status_code} {body}", wait)
    if not r.ok:
        body = r.text[:400]
        r.close()
        if r.status_code in (401, 402, 403) and name.startswith("openrouter"):
            raise RuntimeError(f"OpenRouter refused the request ({r.status_code}). Out of credits, or a bad key: "
                               f"{body}")
        raise RuntimeError(f"{BRAINS[name]['label']} {r.status_code}: {body}")

    r.encoding = "utf-8"
    try:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("usage"):
                spend.update(chunk["usage"])
            if chunk.get("error"):
                raise RuntimeError(f"{BRAINS[name]['label']}: {chunk['error']}")
            choices = chunk.get("choices") or []
            if choices:
                yield choices[0].get("delta") or {}
    finally:
        r.close()
        if spend.get("cost") is not None:
            note_spend(model, float(spend["cost"]), spend)


_model_free_at: dict[str, float] = {}
# what today's requests cost (OpenRouter reports it per request; Groq is free)
spending = {"today": "", "cost": 0.0, "requests": 0, "cached_in": 0, "in": 0, "out": 0}


def note_spend(model: str, cost: float, usage: dict) -> None:
    today = datetime.now().strftime("%d %b")
    if spending["today"] != today:
        spending.update({"today": today, "cost": 0.0, "requests": 0, "cached_in": 0, "in": 0, "out": 0})
    details = usage.get("prompt_tokens_details") or {}
    spending["cost"] += cost
    spending["requests"] += 1
    spending["cached_in"] += int(details.get("cached_tokens") or 0)
    spending["in"] += int(usage.get("prompt_tokens") or 0)
    spending["out"] += int(usage.get("completion_tokens") or 0)


def stream_with_fallback(messages: list[dict], use_tools: bool, wants_action: bool = False):
    """Try each model; when all are rate limited, wait (up to 45 s) for the first to free up.

    Yields streamed deltas, plus {"_wait": seconds} while waiting.
    """
    network_failures = 0
    for _ in range(4):
        available = turn_models(wants_action)
        for provider, model in sorted(available, key=lambda pm: _model_free_at.get(pm[1], 0) > time.time()):
            if _model_free_at.get(model, 0) > time.time():
                continue
            started = False
            try:
                for delta in groq_stream(messages, use_tools, model, provider):
                    started = True
                    yield delta
                return
            except RetryableGroqError as e:
                if started:
                    raise
                _model_free_at[model] = time.time() + max(e.wait, 2 if "413" not in str(e) else 60)
                print(f"  {model} unavailable ({str(e)[:80]}), trying another model")
            except requests.RequestException as e:
                # slow or dropped connection (the session already retried): try again a couple of times
                network_failures += 1
                print(f"  network problem talking to the brain ({type(e).__name__}), attempt {network_failures}")
                if started or network_failures >= 3:
                    raise
                yield {"_wait": 2 * network_failures, "_why": "reconnecting"}
                time.sleep(2 * network_failures)
        waits = [_model_free_at[m] for _, m in available if m in _model_free_at]
        wait = min(waits) - time.time() if waits else 0
        if wait > 45:
            break
        if wait > 0:
            yield {"_wait": wait}
            time.sleep(wait + 0.5)
    if brain_name().startswith("openrouter"):
        raise RuntimeError("OpenRouter is busy, rate limited or out of credit. Wait a minute, or switch the brain "
                           "back to Groq in Settings.")
    raise RuntimeError("I've hit the free Groq limit for now. Give me a minute and ask again.")


ACTION_REQUEST = re.compile(
    r"\b(delete|remove|create|make|open|close|run|install|uninstall|move|copy|rename|write|save|send|type|set|"
    r"start|stop|turn|change|download|lock|shut|restart|play|pause|clean|zip|organi[sz]e|add|put|kill)\b", re.I)
CLAIMS_DONE = re.compile(
    r"\b(done|deleted|removed|created|made|opened|closed|ran|installed|moved|copied|renamed|written|wrote|saved|"
    r"sent|typed|started|stopped|turned|changed|downloaded|locked|playing|paused|cleaned|zipped|added|killed|"
    r"has been|have been|is now|are now)\b", re.I)


def _call_key(call: dict) -> tuple[str, str]:
    try:
        args = {k: v for k, v in json.loads(call["args"] or "{}").items() if v not in (None, "")}
    except (json.JSONDecodeError, AttributeError):
        args = call["args"]
    return call["name"], json.dumps(args, sort_keys=True)


_active_turn: dict = {}


def run_turn(user_text: str, on_phone: bool = False):
    """Yield ("text", delta), ("tool", name, args), ("step", summary) and ("status", text) events."""
    global _turn_counter, _active_turn
    me = {"cancel": threading.Event(), "closed": False}
    with history_lock:
        _turn_counter += 1
        # the user spoke over (or cancelled) the previous answer, which may still be running in
        # its thread: close it off now, so this question isn't mixed up with the old one
        previous = _active_turn
        if previous and not previous["closed"]:
            previous["cancel"].set()
            previous["closed"] = True
            history.append({"role": "assistant", "content": "[the user interrupted this answer; don't continue it "
                                                            "unless asked]"})
        _active_turn = me
        history.append({"role": "user", "content": user_text})
        convo = build_convo(on_phone)
    turn_start = len(convo) - 1
    last_reply = next((m["content"] for m in reversed(convo[:-1]) if m["role"] == "assistant"), "")
    ctx = {"turn": _turn_counter, "user_text": user_text, "last_reply": last_reply}

    final = ""
    steps: list[dict] = []
    executed: set[tuple[str, str]] = set()
    no_tools = False
    nudged = False
    # For requests to *do* something, hold back text written before any tool ran:
    # if it claims success without a tool call, it is thrown away and the model is told to act.
    wants_action = bool(ACTION_REQUEST.search(user_text))
    try:
        for step in range(MAX_STEPS):
            compact_convo(convo, turn_start)
            calls: dict[int, dict] = {}
            content = ""
            hold = wants_action and not executed and not nudged
            for delta in stream_with_fallback(convo, use_tools=step < MAX_STEPS - 1 and not no_tools,
                                              wants_action=wants_action or bool(executed)):
                if me["cancel"].is_set():
                    return
                if "_wait" in delta:
                    why = delta.get("_why", "busy")
                    yield ("status", f"{why}, continuing in {round(delta['_wait'])}s")
                    continue
                piece = delta.get("content")
                if piece:
                    if not content and final and not final.endswith((" ", "\n")):
                        piece = " " + piece
                    content += piece
                    if not hold:
                        yield ("text", piece)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
            if hold and not calls and CLAIMS_DONE.search(content):
                nudged = True
                print(f"  claimed without acting, retrying: {content[:80]!r}")
                convo.append({"role": "system", "content": (
                    "You have not called any tool in this turn, so nothing has actually been done. Call the right "
                    "tool now to do what the user asked, or say honestly that you haven't done it.")})
                continue
            if hold and content:
                yield ("text", content)
            # models sometimes answer and then repeat the same tool call; stop there
            fresh = [c for c in calls.values() if _call_key(c) not in executed]
            if calls and not fresh:
                if content.strip():
                    final = content
                    break
                no_tools = True  # it is looping: make it answer from what it has
                continue
            final += content
            if not fresh:
                break
            executed.update(_call_key(c) for c in fresh)

            convo.append({"role": "assistant", "content": content, "tool_calls": [
                {"id": c["id"] or f"call_{step}_{i}", "type": "function",
                 "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for i, c in enumerate(fresh)
            ]})
            for i, c in enumerate(fresh):
                try:
                    args = json.loads(c["args"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                if me["cancel"].is_set():
                    return  # interrupted: don't start more actions
                yield ("tool", c["name"], args)
                result = run_tool(c["name"], args, ctx)
                summary = step_summary(c["name"], args, result)
                steps.append(summary)
                yield ("step", summary)
                text = json.dumps(result, ensure_ascii=False)
                print(f"  tool {c['name']}({_short(json.dumps(args, ensure_ascii=False), 120)}) -> {text[:160]}")
                convo.append({"role": "tool", "tool_call_id": c["id"] or f"call_{step}_{i}", "content": text[:4000]})

        if not final.strip():
            final = "Sorry, I couldn't finish that. Could you ask again?"
            yield ("text", final)
    finally:
        # An interrupted turn must still be closed off in the history, or the
        # next turn sees an unanswered request and acts on it again.
        reply = final.strip() or "[interrupted before replying; do not act on that request again unless asked]"
        with history_lock:
            if not me["closed"]:
                me["closed"] = True
                history.append({"role": "assistant", "content": reply})
            del history[:-MAX_HISTORY]
        if final.strip() and not me["cancel"].is_set():
            store.log_exchange(user_text, split_code(final)[0] or final.strip(), "phone" if on_phone else "pc", steps)


# ---------------------------------------------------------------------------
# Code blocks: shown in the UI and saved to generated/, never spoken
# ---------------------------------------------------------------------------
GENERATED_DIR = BASE_DIR / "generated"
FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
EXTENSIONS = {
    "python": "py", "py": "py", "javascript": "js", "js": "js", "typescript": "ts", "ts": "ts",
    "tsx": "tsx", "jsx": "jsx", "html": "html", "css": "css", "json": "json", "java": "java",
    "c": "c", "cpp": "cpp", "c++": "cpp", "csharp": "cs", "cs": "cs", "go": "go", "rust": "rs",
    "kotlin": "kt", "swift": "swift", "php": "php", "ruby": "rb", "sql": "sql", "bash": "sh",
    "sh": "sh", "shell": "sh", "powershell": "ps1", "ps1": "ps1", "bat": "bat", "cmd": "bat",
    "yaml": "yml", "yml": "yml", "xml": "xml", "markdown": "md", "md": "md", "dart": "dart",
    "r": "r", "lua": "lua", "dockerfile": "Dockerfile", "toml": "toml", "ini": "ini",
}


def _block(info: str, code: str) -> dict:
    parts = info.replace("title=", " ").replace(":", " ").split()
    lang = parts[0].lower() if parts and "." not in parts[0] else ""
    filename = next((p.strip("\"'") for p in parts if "." in p), None)
    if not lang and filename:
        ext = filename.rsplit(".", 1)[-1].lower()
        lang = next((k for k, v in EXTENSIONS.items() if v == ext), ext)
    return {"lang": lang, "filename": filename, "code": code.rstrip()}


def split_code(text: str) -> tuple[str, list[dict]]:
    """Separate fenced code blocks from the text that should be spoken."""
    blocks = [_block(m.group(1), m.group(2)) for m in FENCE.finditer(text)]
    spoken = FENCE.sub(" ", text)
    if "```" in spoken:  # unterminated fence (reply was cut off)
        head, tail = spoken.split("```", 1)
        info, _, code = tail.partition("\n")
        blocks.append(_block(info, code))
        spoken = head
    spoken = re.sub(r"\s+", " ", spoken).strip()
    return spoken, [b for b in blocks if b["code"].strip()]


def visible_text(full: str, final: bool) -> tuple[str, bool]:
    """Text outside code fences so far (stable prefix while streaming), and whether code started."""
    out = FENCE.sub(" ", full)
    has_code = out != full
    if "```" in out:
        out, has_code = out.split("```", 1)[0], True
    elif not final:
        trimmed = out.rstrip("`")  # might be the start of a fence
        if len(out) - len(trimmed) in (1, 2):
            out = trimmed
    return out, has_code


def save_code(blocks: list[dict]) -> None:
    if not blocks:
        return
    folder = GENERATED_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
    folder.mkdir(parents=True, exist_ok=True)
    for i, b in enumerate(blocks, 1):
        name = re.sub(r"[^\w.\-]", "_", Path(b["filename"]).name) if b["filename"] else ""
        if not name:
            ext = EXTENSIONS.get(b["lang"], "txt")
            name = ext if ext == "Dockerfile" else f"snippet{i}.{ext}"
        path = folder / name
        path.write_text(b["code"] + "\n", encoding="utf-8")
        b["path"] = str(path.relative_to(BASE_DIR))
        print(f"  saved {b['path']}")


# ---------------------------------------------------------------------------
# Streaming speech: synthesize sentence by sentence while the reply streams
# ---------------------------------------------------------------------------
tts_pool = ThreadPoolExecutor(max_workers=4)
SENTENCE_END = re.compile(r"(?<=[.!?।…])[\"')\]]*\s+")
FILLERS = ["One sec, let me check.", "Let me look that up.", "Give me a second.", "Checking that for you."]
# slow tools get a short spoken heads-up so the silence doesn't feel broken
TOOL_FILLERS = {
    "web_search": None, "get_news": None,
    "speed_test": "Running a speed test, this takes about 15 seconds.",
    "run_command": "On it.",
    "look_at_screen": "Taking a look.",
}
_filler_audio: dict[str, str] = {}


def _filler_clip(text: str) -> str:
    if text not in _filler_audio:
        _filler_audio[text] = synthesize(text)
    return _filler_audio[text]


class SpeechStream:
    """Collects streamed reply text and hands back ordered TTS clips.

    Clips are synthesized as soon as a sentence completes, but only released
    once we know the reply is not a code answer (code replies stay silent).
    """

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self.buffer = ""
        self.futures = []
        self.next_out = 0
        self.released = False
        self.muted = not enabled
        self.filled = False

    @property
    def started(self) -> bool:
        return bool(self.futures) or bool(self.buffer.strip())

    def _queue(self, sentence: str, fn=synthesize) -> None:
        if self.muted or not re.search(r"\w", sentence):
            return
        self.futures.append(tts_pool.submit(fn, sentence.strip()))

    def feed(self, text: str) -> None:
        self.buffer += text
        while True:
            m = SENTENCE_END.search(self.buffer, 24)
            if m:
                self._queue(self.buffer[: m.end()])
                self.buffer = self.buffer[m.end():]
                continue
            if len(self.buffer) > 260:  # very long sentence: break at a comma or space
                cut = max(self.buffer.rfind(", ", 0, 240), self.buffer.rfind(" ", 0, 240))
                if cut > 40:
                    self._queue(self.buffer[: cut + 1])
                    self.buffer = self.buffer[cut + 1:]
                    continue
            break

    def filler(self, text: str | None = None) -> None:
        # once per turn, only before the answer has started, and only when replies are in English
        if store.settings()["language"] != "english":
            return
        if not self.muted and not self.started and not self.filled:
            self.filled = True
            self._queue(text or random.choice(FILLERS), fn=_filler_clip)
            self.released = True

    def flush(self) -> None:
        self._queue(self.buffer)
        self.buffer = ""

    def mute(self) -> None:
        self.muted = True
        for f in self.futures[self.next_out:]:
            f.cancel()
        del self.futures[self.next_out:]

    def take(self, block: bool = False):
        while self.released and self.next_out < len(self.futures):
            f = self.futures[self.next_out]
            if not block and not f.done():
                return
            self.next_out += 1
            try:
                yield f.result(timeout=30)
            except Exception as e:  # noqa: BLE001
                print("  TTS failed:", e)


def chat_events(text: str, speak: bool, on_phone: bool = False):
    def event(**payload) -> str:
        return json.dumps(payload, ensure_ascii=False) + "\n"

    speech = SpeechStream(speak)
    full, spoken_len, seq = "", 0, 0
    t0 = time.time()
    marks: dict[str, float] = {}
    has_code = announced_code = False

    def clip_event(clip) -> str:
        nonlocal seq
        seq += 1
        if isinstance(clip, dict):  # Telugu/Hindi without Sarvam: the device speaks it
            return event(t="say", seq=seq - 1, text=clip["say"], lang=clip["lang"])
        return event(t="audio", seq=seq - 1, data=clip)

    def audio():
        for clip in speech.take():
            marks.setdefault("first_audio", time.time() - t0)
            yield clip_event(clip)

    # Run the turn (LLM + tools) in a worker thread so finished TTS clips can
    # be sent while a slow tool such as web search is still running.
    events: queue.Queue = queue.Queue()
    stop = threading.Event()

    def produce():
        try:
            for item in run_turn(text, on_phone):
                if stop.is_set():
                    break
                events.put(item)
        except Exception as e:  # noqa: BLE001
            events.put(("error", e))
        finally:
            events.put(("end",))

    threading.Thread(target=produce, daemon=True).start()

    try:
        while True:
            try:
                kind, *data = events.get(timeout=0.05)
            except queue.Empty:
                yield from audio()
                continue
            if kind == "end":
                break
            if kind == "error":
                raise data[0]
            if kind == "text":
                marks.setdefault("first_text", time.time() - t0)
                full += data[0]
                visible, has_code = visible_text(full, final=False)
                if has_code:
                    speech.mute()
                    if not announced_code:
                        announced_code = True
                        yield event(t="status", text="writing code...")
                new = visible[spoken_len:]
                spoken_len = len(visible)
                if new:
                    speech.feed(new)
                    yield event(t="caption", text=re.sub(r"\s+", " ", visible).strip())
                # clearly a spoken answer (a finished sentence followed by more prose,
                # not a "here's the code:" intro) -> start talking now
                if not has_code and (len(visible) > 160 or re.search(r"[.!?]\s+[^\s`]", visible)):
                    speech.released = True
            elif kind == "tool":
                name, args = data
                yield event(t="status", text=TOOL_STATUS.get(name, lambda a: name.replace("_", " "))(args))
                if name in TOOL_FILLERS:
                    speech.filler(TOOL_FILLERS[name])
                if not has_code:
                    speech.released = True
            elif kind == "step":
                yield event(t="step", **data[0])
            elif kind == "status":
                yield event(t="status", text=data[0])
            yield from audio()

        spoken, code = split_code(full)
        if code:
            speech.mute()
            save_code(code)
            yield event(t="code", blocks=code)
        else:
            visible, _ = visible_text(full, final=True)
            speech.feed(visible[spoken_len:])
            speech.flush()
            speech.released = True
        yield event(t="caption", text=spoken or ("Here's the code." if code else ""))
        print(f"Karen > {spoken}" + (f"  [+{len(code)} code block(s)]" if code else ""))

        for clip in speech.take(block=True):
            marks.setdefault("first_audio", time.time() - t0)
            yield clip_event(clip)
        yield event(t="done")
        timing = "  ".join(f"{k}={v:.1f}s" for k, v in marks.items())
        print(f"  timing: {timing}  total={time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e)
        yield event(t="error", error=friendly_error(e))
    finally:
        stop.set()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
app = FastAPI(title="Karen")
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

UNPAIRED_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>Karen</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#080b14;
color:#d6e4ee;font:15px/1.6 system-ui,sans-serif;text-align:center;padding:24px">
<div><div style="color:#5fe3ff;letter-spacing:.3em;font-size:12px">Karen</div>
<h2 style="font-weight:400">This device isn't paired</h2>
<p>On your PC, open Karen and tap the <b>&#128241;</b> button,<br>then scan the QR code with this phone.</p></div>"""


def is_local(request: Request) -> bool:
    client = request.client.host if request.client else ""
    # a local reverse proxy (e.g. `tailscale serve`) also connects from loopback,
    # but its visitors are other devices: they must pair like any phone
    proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "tailscale-user-login"))
    return client in ("127.0.0.1", "::1") and request.headers.get("host", "") in LOCAL_HOSTS and not proxied


@app.middleware("http")
async def access_control(request: Request, call_next):
    # Karen can control this PC. The PC's own browser (loopback, correct Host
    # header, which also stops DNS rebinding) is trusted; any other device must
    # have paired by scanning the QR code.
    path = request.url.path
    if path == "/pair":
        return await call_next(request)
    if not (is_local(request) or phone.is_paired(request.cookies.get(phone.COOKIE_NAME))):
        if path.startswith("/api/"):
            return JSONResponse({"error": "this device isn't paired with Karen"}, status_code=401)
        return HTMLResponse(UNPAIRED_PAGE, status_code=401)
    # other websites open in the same browser must not drive the API
    origin = request.headers.get("origin")
    if path.startswith("/api/") and origin and origin.split("://", 1)[-1] != request.headers.get("host"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    response = await call_next(request)
    # revalidate page files every load so phones pick up updates immediately
    if not path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@app.get("/pair")
def pair(token: str = ""):
    if not phone.is_paired(token):
        return HTMLResponse(UNPAIRED_PAGE, status_code=403)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(phone.COOKIE_NAME, token, max_age=365 * 24 * 3600, httponly=True, secure=True,
                        samesite="lax")
    return response


@app.get("/api/phone")
def phone_link(request: Request):
    if not is_local(request):  # only the PC itself may hand out pairing links
        return JSONResponse({"error": "open this on the PC"}, status_code=403)
    if not PHONE_ENABLED:
        return {"enabled": False}
    token = phone.pairing_secret()
    lan = f"https://{phone.lan_ip()}:{PHONE_PORT}"
    links = {"enabled": True, "lan": {"address": lan, "pair_url": f"{lan}/pair?token={token}"}}
    if PUBLIC_URL:  # e.g. Tailscale: reachable from any network with a real certificate
        links["public"] = {"address": PUBLIC_URL, "pair_url": f"{PUBLIC_URL}/pair?token={token}"}
    return links


def api_error(e: Exception) -> JSONResponse:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        host = requests.utils.urlparse(e.response.url).hostname
        detail = f"{host} {e.response.status_code}: {e.response.text[:200]}"
    else:
        detail = friendly_error(e)
    print("ERROR:", e)
    return JSONResponse({"error": detail}, status_code=502)


class ChatIn(BaseModel):
    text: str
    speak: bool = True


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "model": turn_models(wants_action=True)[0][1],
        "brain": brain_name(),
        "deepgram_key": bool(DEEPGRAM_API_KEY),
        "groq_key": bool(GROQ_API_KEY),
        "openrouter_key": bool(OPENROUTER_API_KEY),
    }


def _timed_check(name: str, fn) -> dict:
    started = time.time()
    try:
        ok, detail = fn()
    except Exception as e:  # noqa: BLE001
        ok, detail = False, friendly_error(e)
    return {"name": name, "ok": ok, "detail": detail, "ms": int((time.time() - started) * 1000)}


def _check_groq():
    if not GROQ_API_KEY:
        return False, "GROQ_API_KEY missing in .env"
    r = http.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                 timeout=(6, 12))
    if r.status_code == 401:
        return False, "Groq key rejected (401)"
    if r.status_code != 200:
        return False, f"Groq answered {r.status_code}"
    models = {m["id"] for m in r.json().get("data", [])}
    wanted = BRAINS["groq"]["models"] + ([vision.model()] if brain_name() == "groq" else [])
    missing = [m for m in wanted if m not in models]
    return not missing, "reachable" + (f"; models not available: {', '.join(missing)}" if missing else "")


def _check_deepgram():
    if not DEEPGRAM_API_KEY:
        return False, "DEEPGRAM_API_KEY missing in .env"
    r = http.get("https://api.deepgram.com/v1/projects", headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"},
                 timeout=(6, 12))
    if r.status_code in (401, 403):
        return False, f"Deepgram key rejected ({r.status_code})"
    return r.status_code == 200, "reachable" if r.status_code == 200 else f"Deepgram answered {r.status_code}"


def _check_openrouter():
    r = http.get("https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                 timeout=(6, 12))
    if r.status_code in (401, 403):
        return False, "key rejected"
    if r.status_code != 200:
        return False, f"answered {r.status_code}"
    data = r.json().get("data", {})
    left = float(data.get("total_credits", 0)) - float(data.get("total_usage", 0))
    free_note = " — with no credit only the free models work (about 50 requests a day)" if left <= 0 else ""
    return True, f"key works · ${left:.2f} credit left{free_note}"


def _check_state():
    store.STATE_DIR.mkdir(parents=True, exist_ok=True)
    probe = store.STATE_DIR / ".write_test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()
    return True, str(store.STATE_DIR)


@app.get("/api/diagnostics")
def diagnostics():
    """Checks for the diagnostics panel: keys, services, storage, phone access."""
    checks = [("Groq", _check_groq), ("Deepgram (hearing + voice)", _check_deepgram),
              ("Saved settings/memory", _check_state)]
    if OPENROUTER_API_KEY:
        checks.insert(1, ("OpenRouter", _check_openrouter))
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        results = list(pool.map(lambda c: _timed_check(*c), checks))
    results += [
        {"name": "Telugu/Hindi voice", "ok": True,
         "detail": "Sarvam AI" if SARVAM_API_KEY else "device voice (add SARVAM_API_KEY for a better one)"},
        {"name": "Web search", "ok": True,
         "detail": "Tavily" if os.getenv("TAVILY_API_KEY") else "free search engines (no key)"},
        {"name": "Phone access", "ok": True,
         "detail": f"https://{phone.lan_ip()}:{PHONE_PORT}" if PHONE_ENABLED else "off (ANVI_PHONE=0)"},
    ]
    chosen = brain()
    order = turn_models(wants_action=True)
    spent = dict(spending)
    if spent["requests"]:
        saved = spent["cached_in"] / max(1, spent["in"])
        spent["summary"] = (f"${spent['cost']:.3f} in {spent['requests']} requests today"
                            f" ({saved:.0%} of the prompt came from the cache)")
    return {"checks": results, "settings": {**store.settings(), "brain": brain_name()}, "spending": spent, "models": {
        "chat": f"{order[0][1]} ({chosen['label']})",
        "fallback": " → ".join(m for _, m in order[1:]) or "-",
        "vision": vision.model(), "hearing": f"{STT_MODEL} ({STT_LANGUAGE})"}}


@app.post("/api/voice-test")
def voice_test():
    """A short spoken sentence, to check the speakers and the voice service."""
    try:
        clip = synthesize(f"Hi, this is {store.settings()['wake_word']}. If you can hear me, my voice is working.")
    except Exception as e:  # noqa: BLE001
        return api_error(e)
    return {"clip": clip}


# Plain `def` endpoints run in FastAPI's threadpool, so blocking HTTP calls
# don't freeze the server.
@app.post("/api/transcribe")
def api_transcribe(audio: UploadFile = File(...), mime_type: str = Form("audio/webm"), purpose: str = Form("")):
    try:
        audio_bytes = audio.file.read()
        if len(audio_bytes) < 1000:
            return {"transcript": ""}
        started = time.time()
        transcript = transcribe(audio_bytes, mime_type, purpose)
        took = f"(speech-to-text {time.time() - started:.1f}s, {len(audio_bytes) // 1024} KB)"
        if transcript and purpose == "wake":
            print(f"  asleep, heard: {transcript}   {took}")
        elif transcript:
            print(f"\nYOU  > {transcript}   {took}")
        return {"transcript": transcript}
    except Exception as e:  # noqa: BLE001
        return api_error(e)


@app.post("/api/chat")
def api_chat(body: ChatIn, request: Request):
    text = body.text.strip()
    if not text:
        return JSONResponse({"error": "empty message"}, status_code=400)
    return StreamingResponse(
        chat_events(text, body.speak and store.settings()["speak_replies"], on_phone=not is_local(request)),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class ToolIn(BaseModel):
    name: str
    args: dict = {}
    user_text: str = ""
    last_reply: str = ""
    turn: int = 0


# PC actions the Android app may ask this PC to perform (its own brain runs on the phone)
# the phone does these itself (its own search, memory and briefing)
PHONE_APP_TOOLS = set(TOOL_FUNCS) - {"web_search", "get_news", "get_weather", "speed_test", "remember", "forget",
                                     "daily_briefing"}


@app.post("/api/tool")
def api_tool(body: ToolIn):
    if body.name not in PHONE_APP_TOOLS:
        return JSONResponse({"error": f"{body.name} can't be run remotely"}, status_code=403)
    result = run_tool(body.name, body.args, {"turn": body.turn, "user_text": body.user_text, "source": "phone",
                                                 "last_reply": body.last_reply})
    print(f"  phone app -> {body.name}({json.dumps(body.args, ensure_ascii=False)}) -> {json.dumps(result)[:160]}")
    return {"result": result}


@app.get("/api/tool-defs")
def tool_defs():
    """PC tool definitions for the phone app's own brain (descriptions say they act on the PC)."""
    defs = []
    for t in TOOLS:
        fn = t["function"]
        if fn["name"] in PHONE_APP_TOOLS:
            defs.append({"type": "function", "function": {**fn, "name": fn["name"],
                                                          "description": "[on the PC] " + fn["description"]}})
    return {"tools": defs}


@app.get("/api/app-config")
def app_config(request: Request):
    """Everything the Android app needs, packed into the setup QR code (PC only)."""
    if not is_local(request):
        return JSONResponse({"error": "open this on the PC"}, status_code=403)
    config = {
        "anvi": 1,
        "groq": GROQ_API_KEY or "",
        "deepgram": DEEPGRAM_API_KEY or "",
        "tavily": os.getenv("TAVILY_API_KEY", ""),
        "sarvam": SARVAM_API_KEY,
        "openrouter": OPENROUTER_API_KEY,
        "token": phone.pairing_secret(),
        "lan": f"https://{phone.lan_ip()}:{PHONE_PORT}" if PHONE_ENABLED else "",
        "public": PUBLIC_URL,
    }
    return {"qr": json.dumps(config, separators=(",", ":"))}


# set by desktop.py: brings the app window to the front
show_window_hook = None


@app.post("/api/desktop/show")
def desktop_show(request: Request):
    """A second launch of Karen.exe asks the running one to show its window."""
    if not is_local(request) or show_window_hook is None:
        return JSONResponse({"shown": False}, status_code=404)
    show_window_hook()
    return {"shown": True}


class SettingsIn(BaseModel):
    brain: str | None = None
    wake_word: str | None = None
    voice: str | None = None
    language: str | None = None
    speak_replies: bool | None = None
    barge_in: bool | None = None
    sounds: bool | None = None


@app.get("/api/settings")
def get_settings():
    return {"settings": {**store.settings(), "brain": brain_name()}, "voices": store.VOICES,
            "languages": {k: v["label"] for k, v in store.LANGUAGES.items()},
            "indian_voice": "sarvam" if SARVAM_API_KEY else "device",
            "brains": {k: {"label": v["label"],
                           "models": [m for _, m in turn_models(wants_action=True)] if k == "mix" else v["models"],
                           "available": bool(brain_key(k)) and (k != "mix" or bool(GROQ_API_KEY))}
                       for k, v in BRAINS.items()}}


@app.post("/api/settings")
def post_settings(body: SettingsIn):
    try:
        return {"settings": store.update_settings({k: v for k, v in body.model_dump().items() if v is not None})}
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)


@app.get("/api/history")
def get_history():
    return {"conversations": store.history()}


@app.delete("/api/history")
def delete_history():
    store.clear_history()
    return {"ok": True}


@app.get("/api/memory")
def get_memory():
    return {"memories": store.memories()}


class ForgetIn(BaseModel):
    fact: str


@app.post("/api/memory/forget")
def post_forget(body: ForgetIn):
    return store.forget(body.fact)


@app.post("/api/extract")
def api_extract(file: UploadFile = File(...)):
    """Text of a shared document (the phone app sends PDFs/Word files here to read them)."""
    import tempfile

    name = Path(file.filename or "file").name
    data = file.file.read(30 * 1024 * 1024 + 1)
    if len(data) > 30 * 1024 * 1024:
        return JSONResponse({"error": "file is larger than 30 MB"}, status_code=413)
    if vision.is_image(name):
        result = vision.describe_image_bytes(data, name, "Describe this image and read out any text in it.")
        if result.get("error"):
            return JSONResponse(result, status_code=422)
        return {"name": name, "text": result["answer"], "truncated": False, "kind": "image description"}
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / name
        path.write_bytes(data)
        try:
            text, info = docs.extract_text(path)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)}, status_code=422)
    return {"name": name, "text": text[:60000], "truncated": len(text) > 60000, **info}


@app.post("/api/reset")
def api_reset():
    with history_lock:
        history.clear()
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html", headers={"Cache-Control": "no-store"})


app.mount("/", StaticFiles(directory=WEB_DIR), name="web")


def _warm_up():
    """Slow lookups done once in the background so the first request is fast."""
    for fn in (weather.home_location, pc._installed_apps, lambda: [_filler_clip(f) for f in FILLERS]):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print("warm-up:", e)


async def serve() -> None:
    import uvicorn

    configs = [uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")]
    if PHONE_ENABLED:
        # phone access is optional: a certificate problem must not stop Karen on this PC
        for attempt in range(3):
            try:
                cert, key = phone.ensure_certificate(phone.lan_ip())
                configs.append(uvicorn.Config(app, host="0.0.0.0", port=PHONE_PORT, log_level="warning",
                                              ssl_certfile=cert, ssl_keyfile=key))
                break
            except OSError as e:  # e.g. antivirus briefly locking a file in .anvi
                print(f"phone access unavailable ({e}); retrying" if attempt < 2 else f"phone access off: {e}")
                await asyncio.sleep(1)
    await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 60)
    print(f"  Karen online  ->  {url}")
    if PHONE_ENABLED:
        print(f"  phone (same network) -> https://{phone.lan_ip()}:{PHONE_PORT}   pair via the phone button on the PC")
        if PUBLIC_URL:
            print(f"  phone (anywhere)     -> {PUBLIC_URL}")
    print(f"  brain: {brain()['label']} ({', '.join(m for _, m in turn_models(True))})   voice: {TTS_VOICE}")
    print(f"  search: {'Tavily' if os.getenv('TAVILY_API_KEY') else 'Brave API' if os.getenv('BRAVE_API_KEY') else 'keyless (Bing/DuckDuckGo/Brave/Google News)'}")
    print("=" * 60)
    threading.Thread(target=_warm_up, daemon=True).start()
    if os.getenv("ANVI_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
