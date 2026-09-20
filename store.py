"""Karen's saved state: settings, long-term memory and chat history.

Kept in %LOCALAPPDATA%\\Karen rather than next to the code: antivirus ransomware
protection (e.g. Avast) blocks unknown apps from changing files under Desktop
and Documents.
"""

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

STATE_DIR = Path(os.getenv("KAREN_STATE_DIR") or Path(os.getenv("LOCALAPPDATA", Path.home())) / "Karen")
SETTINGS_FILE = STATE_DIR / "settings.json"
MEMORY_FILE = STATE_DIR / "memory.json"
HISTORY_FILE = STATE_DIR / "history.jsonl"
_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
VOICES = {
    "aura-asteria-en": "Asteria (US, female)",
    "aura-luna-en": "Luna (US, female)",
    "aura-stella-en": "Stella (US, female)",
    "aura-athena-en": "Athena (UK, female)",
    "aura-hera-en": "Hera (US, female)",
    "aura-orion-en": "Orion (US, male)",
    "aura-arcas-en": "Arcas (US, male)",
    "aura-perseus-en": "Perseus (US, male)",
    "aura-angus-en": "Angus (Irish, male)",
    "aura-orpheus-en": "Orpheus (US, male)",
    "aura-helios-en": "Helios (UK, male)",
    "aura-zeus-en": "Zeus (US, male)",
}
LANGUAGES = {
    "english": {"label": "English", "code": "en", "bcp47": "en-IN"},
    "telugu": {"label": "Telugu", "code": "te", "bcp47": "te-IN"},
    "hindi": {"label": "Hindi", "code": "hi", "bcp47": "hi-IN"},
    "auto": {"label": "Same as I speak", "code": "", "bcp47": ""},
}
BRAIN_CHOICES = ("groq", "openrouter_free", "mix", "openrouter")
DEFAULTS = {
    "brain": "groq",
    "wake_word": "Karen",
    "voice": os.getenv("DEEPGRAM_TTS_VOICE", "aura-asteria-en"),
    "language": "english",
    "speak_replies": True,
    "barge_in": True,  # talking while Karen speaks interrupts her
    "sounds": True,  # small chimes for wake, tasks, done and errors
}


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, data) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def settings() -> dict:
    return {**DEFAULTS, **_read_json(SETTINGS_FILE, {})}


def update_settings(patch: dict) -> dict:
    current = settings()
    if "wake_word" in patch:
        word = re.sub(r"[^A-Za-z ]", "", str(patch["wake_word"])).strip()
        if not 2 <= len(word) <= 20:
            raise ValueError("wake word must be 2-20 letters")
        current["wake_word"] = word[:1].upper() + word[1:]
    if "voice" in patch:
        if patch["voice"] not in VOICES:
            raise ValueError("unknown voice")
        current["voice"] = patch["voice"]
    if "brain" in patch:
        if patch["brain"] not in BRAIN_CHOICES:
            raise ValueError("unknown brain")
        current["brain"] = patch["brain"]
    if "language" in patch:
        if patch["language"] not in LANGUAGES:
            raise ValueError("unknown language")
        current["language"] = patch["language"]
    for flag in ("speak_replies", "barge_in", "sounds"):
        if flag in patch:
            current[flag] = bool(patch[flag])
    with _lock:
        _write_json(SETTINGS_FILE, {k: current[k] for k in DEFAULTS})
    return current


# ---------------------------------------------------------------------------
# Long-term memory: facts the user asked Karen to remember
# ---------------------------------------------------------------------------
def memories() -> list[dict]:
    return _read_json(MEMORY_FILE, [])


def remember(fact: str) -> dict:
    fact = re.sub(r"\s+", " ", fact).strip()
    if not fact:
        return {"error": "nothing to remember"}
    with _lock:
        items = memories()
        if any(m["fact"].lower() == fact.lower() for m in items):
            return {"already_remembered": fact}
        items.append({"id": uuid.uuid4().hex[:8], "fact": fact, "added": f"{datetime.now():%d %b %Y}"})
        _write_json(MEMORY_FILE, items[-200:])
    return {"remembered": fact}


def forget(fact: str) -> dict:
    words = [w for w in re.findall(r"\w+", fact.lower()) if len(w) > 2]
    with _lock:
        items = memories()
        keep = [m for m in items if not (words and all(w in m["fact"].lower() for w in words))]
        removed = [m["fact"] for m in items if m not in keep]
        if removed:
            _write_json(MEMORY_FILE, keep)
    return {"forgot": removed} if removed else {"error": f"nothing remembered matches '{fact}'"}


def memory_prompt(limit: int = 40) -> str:
    items = memories()[-limit:]
    if not items:
        return ""
    return "Things the user asked you to remember: " + "; ".join(f"{m['fact']} (saved {m['added']})" for m in items) + ". "


# ---------------------------------------------------------------------------
# Chat history
# ---------------------------------------------------------------------------
def log_exchange(user_text: str, reply: str, source: str = "pc", steps: list | None = None) -> None:
    entry = {"at": time.time(), "source": source, "you": user_text, "karen": reply}
    if steps:
        entry["steps"] = steps
    try:
        with _lock:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            with open(HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except OSError as e:
        print("history not saved:", e)


def history(limit: int = 200) -> list[dict]:
    """Newest last, grouped into conversations (a gap of 30+ minutes starts a new one)."""
    try:
        lines = HISTORY_FILE.read_text(encoding="utf-8").splitlines()[-limit:]
    except OSError:
        return []
    conversations: list[dict] = []
    last_at = 0.0
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not conversations or e["at"] - last_at > 30 * 60:
            conversations.append({"started": datetime.fromtimestamp(e["at"]).strftime("%a %d %b, %I:%M %p"),
                                  "exchanges": []})
        conversations[-1]["exchanges"].append(
            {"time": datetime.fromtimestamp(e["at"]).strftime("%I:%M %p"), "source": e.get("source", "pc"),
             "you": e["you"], "karen": e["karen"], "steps": e.get("steps", [])})
        last_at = e["at"]
    return conversations


def clear_history() -> None:
    with _lock:
        try:
            HISTORY_FILE.unlink()
        except FileNotFoundError:
            pass
