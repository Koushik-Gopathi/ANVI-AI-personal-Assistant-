"""ANVI - personal voice assistant backend.

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

# BASE_DIR holds .env, .anvi/ and generated/; WEB_DIR the UI files (bundled inside ANVI.exe when packaged)
BASE_DIR = Path(os.getenv("ANVI_HOME") or Path(__file__).resolve().parent)
WEB_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "web"
load_dotenv(BASE_DIR / ".env")  # before importing modules that read the environment

import requests  # noqa: E402
from fastapi import FastAPI, File, Form, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

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
    # help it spell the wake/sleep word; nova-2's "keywords" boost was strong enough to
    # turn normal speech into "ANVI" ("Indus Valley" -> "ANVI"), so only a light hint there
    if STT_MODEL.startswith("nova-3"):
        params["keyterm"] = "ANVI"
    else:
        params["keywords"] = "ANVI:1"

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


TOOLS = [
    _tool("web_search", "Search the internet and read the top pages. Use for anything current or that you are "
          "not certain about: prices and rates (gold, silver, petrol, stocks, crypto, currency), news, sports "
          "scores, events, releases, people's current roles, 'today'/'latest' questions.",
          {"query": _str("search query; add country/city and 'today' when relevant")}, ("query",)),
    _tool("get_news", "Latest news headlines, optionally about a topic.", {"topic": _str("topic, or empty for top stories")}),
    _tool("get_weather", "Real current weather and forecast.", {"location": _str("city; empty = user's location")}),
    _tool("open_app", "Open an app on the user's PC.", {"name": _str("app name, e.g. chrome, whatsapp, notepad, settings")}, ("name",)),
    _tool("close_app", "Close a whole app (all its windows) when the user says close/quit it. To stop or pause "
          "music or a video, use media_control instead.", {"name": _str("app name")}, ("name",)),
    _tool("open_website", "Open a website, or a Google search, in the browser.", {"url_or_query": _str("URL/domain or search text")}, ("url_or_query",)),
    _tool("play_youtube", "Play a song or video on YouTube.", {"query": _str("song or video")}, ("query",)),
    _tool("set_volume", "Change the PC volume: give level (0-100) or action.",
          {"level": {"type": "integer", "description": "0-100"}, "action": {"type": "string", "enum": ["up", "down", "mute"]}}),
    _tool("media_control", "Control music/video playback.",
          {"action": {"type": "string", "enum": ["play_pause", "next", "previous", "stop"]}}, ("action",)),
    _tool("take_screenshot", "Save a screenshot to the Pictures folder. ONLY when the user explicitly asks for a "
          "screenshot in their latest message. You cannot see the image, so never use it to check your work."),
    _tool("system_info", "PC battery, CPU, RAM and disk status."),
    _tool("find_files", "Find files/folders by name in Desktop, Documents, Downloads, Pictures, Music, Videos.",
          {"name": _str("words in the file name")}, ("name",)),
    _tool("open_path", "Open a file or folder by its full path.", {"path": _str("full path")}, ("path",)),
    _tool("lock_pc", "Lock the computer. Only when the user explicitly asks to lock it."),
    _tool("power_action", "Shut down, restart or sleep the PC, or cancel a pending shutdown.",
          {"action": {"type": "string", "enum": ["shutdown", "restart", "sleep", "cancel"]}}, ("action",)),
]

TOOL_FUNCS = {
    "web_search": search.web_search, "get_news": search.get_news, "get_weather": weather.get_weather,
    "open_app": pc.open_app, "close_app": pc.close_app, "open_website": pc.open_website,
    "play_youtube": pc.play_youtube, "set_volume": pc.set_volume, "media_control": pc.media_control,
    "take_screenshot": pc.take_screenshot, "system_info": pc.system_info, "find_files": pc.find_files,
    "open_path": pc.open_path, "lock_pc": pc.lock_pc,
}

TOOL_STATUS = {
    "web_search": lambda a: f"searching: {a.get('query', '')}",
    "get_news": lambda a: f"checking news{': ' + a['topic'] if a.get('topic') else ''}",
    "get_weather": lambda a: "checking the weather",
    "open_app": lambda a: f"opening {a.get('name', 'app')}",
    "close_app": lambda a: f"closing {a.get('name', 'app')}",
    "open_website": lambda a: "opening browser",
    "play_youtube": lambda a: f"finding {a.get('query', '')} on youtube",
    "find_files": lambda a: f"looking for {a.get('name', 'files')}",
    "open_path": lambda a: "opening it",
    "set_volume": lambda a: "adjusting volume",
    "media_control": lambda a: "controlling playback",
    "take_screenshot": lambda a: "taking a screenshot",
    "system_info": lambda a: "checking your PC",
    "lock_pc": lambda a: "locking the PC",
    "power_action": lambda a: f"{a.get('action', 'power')} requested",
}

CONFIRM_RE = re.compile(r"\b(yes|yeah|yep|yup|sure|confirm(ed)?|do it|go ahead|proceed|okay|ok)\b", re.I)
_pending_power: dict = {}


def guarded_power_action(action: str, ctx: dict) -> dict:
    """Shutdown/restart/sleep only after the user says yes in a later message."""
    if action == "cancel":
        _pending_power.clear()
        return pc.power_action("cancel")
    p = _pending_power
    if (p.get("action") == action and p["turn"] < ctx["turn"] and time.time() - p["at"] < 120
            and CONFIRM_RE.search(ctx["user_text"])):
        _pending_power.clear()
        return pc.power_action(action)
    _pending_power.update(action=action, turn=ctx["turn"], at=time.time())
    return {"needs_confirmation": True,
            "instruction": f"Ask the user to confirm they want to {action} the PC. Call again only after they say yes."}


# Actions the model must not take on its own initiative: they only run when the
# user's own words in this turn ask for them (the model sometimes grabs a
# screenshot "to check its work", or closes apps it opened earlier).
EXPLICIT_ONLY = {
    "take_screenshot": re.compile(r"screen\s*shot|screen\s*grab|snap\s*shot|\bss\b|capture (the |my )?screen|print\s*screen", re.I),
    "lock_pc": re.compile(r"\block", re.I),
    "close_app": re.compile(r"\b(close|quit|exit|kill)\b", re.I),
}


def run_tool(name: str, args: dict, ctx: dict) -> dict:
    args = {k: v for k, v in args.items() if v not in (None, "")}  # models often send empty strings
    guard = EXPLICIT_ONLY.get(name)
    if guard and not guard.search(ctx["user_text"]):
        return {"error": "Not done: the user did not ask for this. Only use this tool when explicitly asked."}
    try:
        if name == "power_action":
            return guarded_power_action(args.get("action", ""), ctx)
        fn = TOOL_FUNCS.get(name)
        if not fn:
            return {"error": f"unknown tool {name}"}
        return fn(**args)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Brain (Groq, streamed)
# ---------------------------------------------------------------------------
history: list[dict] = []
history_lock = threading.Lock()
MAX_HISTORY = 16
TOKEN_LIMIT = 7600  # Groq free tier: ~8k tokens/minute per model, prompt + completion
INPUT_BUDGET = 4200
_turn_counter = 0


def system_prompt(on_phone: bool = False) -> str:
    now = datetime.now().astimezone()
    loc = weather.home_location().get("name") or "unknown"
    return (
        "You are ANVI, a smart, warm personal voice assistant running on the user's Windows PC. "
        "Your replies are spoken aloud: answer in 1-3 short natural sentences (under 60 words unless the user "
        "asks for detail), with no markdown, bullet points, numbered lists, "
        "emojis, tables or URLs. Write numbers as digits with units (like ₹15,458 per gram or 27°C) and "
        "round them sensibly; they are read aloud correctly. "
        "You can search the web, get news and weather, and control the PC with your tools. "
        "For anything that changes over time or that you are not sure about - prices and rates, news, "
        "sports, events, releases, people's current roles - call web_search first and answer with the "
        "specific facts and numbers you found, briefly naming the source. Never say you don't know or "
        "can't browse without searching first. "
        "For PC requests, call the tool, then confirm in a few words what you did. Only take PC actions "
        "the user asked for in their latest message, never extra ones. Never claim you did something unless a "
        "tool call in this turn actually did it and succeeded; if no tool can do what was asked (for example "
        "typing text into an app or creating folders), say honestly that you can't do that yet. "
        "When the user asks for code, write complete working code in fenced code blocks with the language "
        "and a filename on the opening fence line, like ```python hello.py. The code is shown on screen, not "
        "read aloud, so outside the code write only one short sentence. When changing earlier code, return "
        "the full updated file. "
        + ("The user is talking to you from their phone, not sitting at the PC: PC actions (apps, volume, "
           "media, screenshots, files, power) happen on the PC, so say 'on your PC' when you do them. "
           if on_phone else "")
        + f"Current local date and time: {now:%A, %d %B %Y, %I:%M %p}. User's location: {loc}."
    )


TOOLS_TOKENS = len(json.dumps(TOOLS)) // 3


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


class RetryableGroqError(Exception):
    pass


def groq_stream(messages: list[dict], use_tools: bool, model: str):
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY missing in .env")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": 0.6,
        "max_completion_tokens": max(400, min(3000, TOKEN_LIMIT - est_tokens(messages))),
        "stream": True,
    }
    if model.startswith("openai/gpt-oss"):
        payload["reasoning_effort"] = "low"
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    r = http.post("https://api.groq.com/openai/v1/chat/completions",
                  headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, json=payload, stream=True, timeout=(10, 60))
    if r.status_code in (413, 429) or (r.status_code == 400 and "tool_use_failed" in r.text):
        body = r.text[:300]
        r.close()
        raise RetryableGroqError(f"{r.status_code} {body}")
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


def stream_with_fallback(messages: list[dict], use_tools: bool):
    models = [GROQ_MODEL] + ([FALLBACK_MODEL] if FALLBACK_MODEL and FALLBACK_MODEL != GROQ_MODEL else [])
    for i, model in enumerate(models):
        try:
            yield from groq_stream(messages, use_tools, model)
            return
        except RetryableGroqError as e:
            print(f"  {model} unavailable ({e}); {'trying ' + models[i + 1] if i + 1 < len(models) else 'giving up'}")
    raise RuntimeError("I've hit the Groq rate limit. Give me a minute and ask again.")


def _call_key(call: dict) -> tuple[str, str]:
    try:
        args = {k: v for k, v in json.loads(call["args"] or "{}").items() if v not in (None, "")}
    except (json.JSONDecodeError, AttributeError):
        args = call["args"]
    return call["name"], json.dumps(args, sort_keys=True)


def run_turn(user_text: str, on_phone: bool = False):
    """Yield ("text", delta) and ("tool", name, args) events for one conversation turn."""
    global _turn_counter
    with history_lock:
        _turn_counter += 1
        history.append({"role": "user", "content": user_text})
        convo = build_convo(on_phone)
    ctx = {"turn": _turn_counter, "user_text": user_text}

    final = ""
    executed: set[tuple[str, str]] = set()
    no_tools = False
    try:
        for step in range(5):
            calls: dict[int, dict] = {}
            content = ""
            for delta in stream_with_fallback(convo, use_tools=step < 4 and not no_tools):
                piece = delta.get("content")
                if piece:
                    if not content and final and not final.endswith((" ", "\n")):
                        piece = " " + piece
                    content += piece
                    yield ("text", piece)
                for tc in delta.get("tool_calls") or []:
                    slot = calls.setdefault(tc.get("index", len(calls)), {"id": "", "name": "", "args": ""})
                    slot["id"] = tc.get("id") or slot["id"]
                    fn = tc.get("function") or {}
                    slot["name"] += fn.get("name") or ""
                    slot["args"] += fn.get("arguments") or ""
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
            calls = {i: c for i, c in enumerate(fresh)}
            executed.update(_call_key(c) for c in fresh)

            convo.append({"role": "assistant", "content": content, "tool_calls": [
                {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"] or "{}"}}
                for c in calls.values()
            ]})
            for c in calls.values():
                try:
                    args = json.loads(c["args"] or "{}")
                except json.JSONDecodeError:
                    args = {}
                yield ("tool", c["name"], args)
                result = json.dumps(run_tool(c["name"], args, ctx), ensure_ascii=False)
                print(f"  tool {c['name']}({json.dumps(args, ensure_ascii=False)}) -> {result[:160]}")
                convo.append({"role": "tool", "tool_call_id": c["id"], "content": result[:4500]})

        if not final.strip():
            final = "Sorry, I couldn't come up with an answer. Could you ask that again?"
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

    def filler(self) -> None:
        if not self.muted and not self.started:
            self._queue(random.choice(FILLERS), fn=_filler_clip)
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
                yield event(t="status", text=TOOL_STATUS.get(name, lambda a: "working on it")(args))
                if name in ("web_search", "get_news"):
                    speech.filler()
                if not has_code:
                    speech.released = True
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
        print(f"ANVI > {spoken}" + (f"  [+{len(code)} code block(s)]" if code else ""))

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
app = FastAPI(title="ANVI")
LOCAL_HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}

UNPAIRED_PAGE = """<!doctype html><meta name=viewport content="width=device-width,initial-scale=1">
<title>ANVI</title><body style="margin:0;min-height:100vh;display:grid;place-items:center;background:#080b14;
color:#d6e4ee;font:15px/1.6 system-ui,sans-serif;text-align:center;padding:24px">
<div><div style="color:#5fe3ff;letter-spacing:.3em;font-size:12px">ANVI</div>
<h2 style="font-weight:400">This device isn't paired</h2>
<p>On your PC, open ANVI and tap the <b>&#128241;</b> button,<br>then scan the QR code with this phone.</p></div>"""


def is_local(request: Request) -> bool:
    client = request.client.host if request.client else ""
    # a local reverse proxy (e.g. `tailscale serve`) also connects from loopback,
    # but its visitors are other devices: they must pair like any phone
    proxied = any(h in request.headers for h in ("x-forwarded-for", "forwarded", "tailscale-user-login"))
    return client in ("127.0.0.1", "::1") and request.headers.get("host", "") in LOCAL_HOSTS and not proxied


@app.middleware("http")
async def access_control(request: Request, call_next):
    # ANVI can control this PC. The PC's own browser (loopback, correct Host
    # header, which also stops DNS rebinding) is trusted; any other device must
    # have paired by scanning the QR code.
    path = request.url.path
    if path == "/pair":
        return await call_next(request)
    if not (is_local(request) or phone.is_paired(request.cookies.get(phone.COOKIE_NAME))):
        if path.startswith("/api/"):
            return JSONResponse({"error": "this device isn't paired with ANVI"}, status_code=401)
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
    turn: int = 0


# PC actions the Android app may ask this PC to perform (its own brain runs on the phone)
PHONE_APP_TOOLS = {"open_app", "close_app", "open_website", "play_youtube", "set_volume", "media_control",
                   "take_screenshot", "system_info", "find_files", "open_path", "lock_pc", "power_action"}


@app.post("/api/tool")
def api_tool(body: ToolIn):
    if body.name not in PHONE_APP_TOOLS:
        return JSONResponse({"error": f"{body.name} can't be run remotely"}, status_code=403)
    result = run_tool(body.name, body.args, {"turn": body.turn, "user_text": body.user_text})
    print(f"  phone app -> {body.name}({json.dumps(body.args, ensure_ascii=False)}) -> {json.dumps(result)[:160]}")
    return {"result": result}


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
        cert, key = phone.ensure_certificate(phone.lan_ip())
        configs.append(uvicorn.Config(app, host="0.0.0.0", port=PHONE_PORT, log_level="warning",
                                      ssl_certfile=cert, ssl_keyfile=key))
    await asyncio.gather(*(uvicorn.Server(c).serve() for c in configs))


if __name__ == "__main__":
    url = f"http://127.0.0.1:{PORT}"
    print("=" * 60)
    print(f"  ANVI online  ->  {url}")
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
