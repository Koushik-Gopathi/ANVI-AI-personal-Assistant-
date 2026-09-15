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
import pc  # noqa: E402
import phone  # noqa: E402
import search  # noqa: E402
import weather  # noqa: E402
from net import http  # noqa: E402

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
PHONE_ENABLED = os.getenv("ANVI_PHONE", "1") == "1"
PHONE_PORT = int(os.getenv("ANVI_PHONE_PORT", "8443"))
# public HTTPS address that forwards to this PC, e.g. https://my-laptop.tail1234.ts.net (Tailscale)
PUBLIC_URL = os.getenv("ANVI_PUBLIC_URL", "").strip().rstrip("/")


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------
def transcribe(audio_bytes: bytes, mime_type: str) -> str:
    if not DEEPGRAM_API_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY missing in .env")

    params = {"model": STT_MODEL, "smart_format": "true", "punctuate": "true", "language": STT_LANGUAGE}
    # help it spell the wake/sleep word
    if STT_MODEL.startswith("nova-3"):
        params["keyterm"] = "Karen"
    else:
        params["keywords"] = "Karen:2"

    response = http.post(
        "https://api.deepgram.com/v1/listen",
        params=params,
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
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"^\s*[-*]\s+", "", text, flags=re.MULTILINE)
    text = text.replace("°C", " degrees").replace("°F", " degrees Fahrenheit").replace("°", " degrees")
    text = re.sub(r"₹\s?", "rupees ", text)
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
    _tool("open_app", "Open an app.", {"name": _str("app name, e.g. chrome, whatsapp, notepad, settings")}, ("name",)),
    _tool("close_app", "Close an app when the user says close/quit it.", {"name": _str("app name")}, ("name",)),
    _tool("open_website", "Open a website or a Google search in the browser.",
          {"url_or_query": _str("URL/domain or search text")}, ("url_or_query",)),
    _tool("play_youtube", "Play a song or video on YouTube.", {"query": _str("song or video")}, ("query",)),
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
]


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
}
TOOL_PARAMS = {t["function"]["name"]: set(t["function"]["parameters"]["properties"]) for t in TOOLS}
# tools that return {"needs_confirmation"} until called again after the user says yes
CONFIRMABLE = {"run_command", "write_file", "move_path", "delete_path", "power_action"}


def _short(value, n: int = 60) -> str:
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= n else text[: n - 1] + "…"


TOOL_STATUS = {
    "web_search": lambda a: f"searching: {a.get('query', '')}",
    "get_news": lambda a: f"checking news{': ' + a['topic'] if a.get('topic') else ''}",
    "get_weather": lambda a: "checking the weather",
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
    "close_app": re.compile(r"\b(close|quit|exit|kill|stop)\b", re.I),
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
MODELS = [GROQ_MODEL] + [m.strip() for m in os.getenv("GROQ_FALLBACK_MODELS", FALLBACK_MODEL).split(",")
                         if m.strip() and m.strip() != GROQ_MODEL]
MODELS = list(dict.fromkeys(MODELS))


def system_prompt(on_phone: bool = False) -> str:
    now = datetime.now().astimezone()
    loc = weather.home_location().get("name") or "unknown"
    folders = agent.known_folders()
    return (
        "You are Karen, the user's personal AI agent running on their Windows PC. You don't just chat: you get "
        "work done with your tools - running PowerShell commands, managing files and folders, opening apps and "
        "websites, typing and pressing keys in apps, speed tests, reminders, web search. "
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
        "write one short sentence. When they want a file created, use write_file instead. "
        f"User's folders: Desktop {folders['desktop']}, Documents {folders['documents']}, "
        f"Downloads {folders['downloads']}. "
        + ("The user is talking to you from their phone, not at the PC: PC actions happen on the PC, so say "
           "'on your PC'. " if on_phone else "")
        + f"Current local date and time: {now:%A, %d %B %Y, %I:%M %p}. User's location: {loc}."
    )


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
    while len(msgs) > 1 and est_tokens([system, *msgs]) > INPUT_BUDGET:
        msgs.pop(0)
    return [system, *msgs]


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


def groq_stream(messages: list[dict], use_tools: bool, model: str):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing in .env")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.5,
        "max_completion_tokens": max(600, min(3000, TOKEN_LIMIT - est_tokens(messages))),
        "stream": True,
    }
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    elif model.startswith("qwen/"):
        payload["reasoning_format"] = "hidden"
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    r = http.post("https://api.groq.com/openai/v1/chat/completions",
                  headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload, stream=True, timeout=(10, 90))
    if r.status_code in (413, 429, 503) or (r.status_code == 400 and "tool_use_failed" in r.text):
        wait = _retry_after(r) if r.status_code == 429 else 0
        body = r.text[:200]
        r.close()
        raise RetryableGroqError(f"{r.status_code} {body}", wait)
    if not r.ok:
        body = r.text[:400]
        r.close()
        raise RuntimeError(f"Groq {r.status_code}: {body}")

    r.encoding = "utf-8"
    try:
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            chunk = json.loads(data)
            if chunk.get("error"):
                raise RuntimeError(f"Groq: {chunk['error']}")
            choices = chunk.get("choices") or []
            if choices:
                yield choices[0].get("delta") or {}
    finally:
        r.close()


_model_free_at: dict[str, float] = {}


def stream_with_fallback(messages: list[dict], use_tools: bool):
    """Try each model; when all are rate limited, wait (up to 45 s) for the first to free up.

    Yields streamed deltas, plus {"_wait": seconds} while waiting.
    """
    for _ in range(4):
        for model in sorted(MODELS, key=lambda m: _model_free_at.get(m, 0) > time.time()):
            if _model_free_at.get(model, 0) > time.time():
                continue
            started = False
            try:
                for delta in groq_stream(messages, use_tools, model):
                    started = True
                    yield delta
                return
            except RetryableGroqError as e:
                if started:
                    raise
                _model_free_at[model] = time.time() + max(e.wait, 2 if "413" not in str(e) else 60)
                print(f"  {model} unavailable ({str(e)[:80]}), trying another model")
        wait = min(_model_free_at.values()) - time.time()
        if wait > 45:
            break
        if wait > 0:
            yield {"_wait": wait}
            time.sleep(wait + 0.5)
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


def run_turn(user_text: str, on_phone: bool = False):
    """Yield ("text", delta), ("tool", name, args), ("step", summary) and ("status", text) events."""
    global _turn_counter
    with history_lock:
        _turn_counter += 1
        history.append({"role": "user", "content": user_text})
        convo = build_convo(on_phone)
    turn_start = len(convo) - 1
    last_reply = next((m["content"] for m in reversed(convo[:-1]) if m["role"] == "assistant"), "")
    ctx = {"turn": _turn_counter, "user_text": user_text, "last_reply": last_reply}

    final = ""
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
            for delta in stream_with_fallback(convo, use_tools=step < MAX_STEPS - 1 and not no_tools):
                if "_wait" in delta:
                    yield ("status", f"busy, continuing in {round(delta['_wait'])}s")
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
                yield ("tool", c["name"], args)
                result = run_tool(c["name"], args, ctx)
                yield ("step", step_summary(c["name"], args, result))
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
            history.append({"role": "assistant", "content": reply})
            del history[:-MAX_HISTORY]


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
        # once per turn, and only before the answer itself has started
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

    def audio():
        nonlocal seq
        for clip in speech.take():
            marks.setdefault("first_audio", time.time() - t0)
            yield event(t="audio", seq=seq, data=clip)
            seq += 1

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
            yield event(t="audio", seq=seq, data=clip)
            seq += 1
        yield event(t="done")
        timing = "  ".join(f"{k}={v:.1f}s" for k, v in marks.items())
        print(f"  timing: {timing}  total={time.time() - t0:.1f}s")
    except Exception as e:  # noqa: BLE001
        print("ERROR:", e)
        yield event(t="error", error=str(e))
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
def api_transcribe(audio: UploadFile = File(...), mime_type: str = Form("audio/webm"), purpose: str = Form("")):
    try:
        audio_bytes = audio.file.read()
        if len(audio_bytes) < 1000:
            return {"transcript": ""}
        started = time.time()
        transcript = transcribe(audio_bytes, mime_type)
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
        chat_events(text, body.speak, on_phone=not is_local(request)),
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
PHONE_APP_TOOLS = set(TOOL_FUNCS) - {"web_search", "get_news", "get_weather", "speed_test"}  # phone does these itself


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
    print(f"  brain: {GROQ_MODEL}   voice: {TTS_VOICE}")
    print(f"  search: {'Tavily' if os.getenv('TAVILY_API_KEY') else 'Brave API' if os.getenv('BRAVE_API_KEY') else 'keyless (Bing/DuckDuckGo/Brave/Google News)'}")
    print("=" * 60)
    threading.Thread(target=_warm_up, daemon=True).start()
    if os.getenv("ANVI_OPEN_BROWSER", "1") == "1":
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass
