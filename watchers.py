"""Watchers: Karen speaks up on her own.

Three kinds, all saved in %LOCALAPPDATA%\\Karen\\watchers.json so they survive a restart:

- alert      - say something at a date and time ("3 days before my exam")
- routine    - run a request on a schedule, as if the user had said it ("every morning at 8: good morning")
- condition  - keep checking something and speak up when it becomes true ("gold below 7,000")

A scheduler thread wakes every few seconds while Karen is open. What fires goes to every open Karen window
(over /api/events); when no window is open, alerts fall back to a Windows pop-up so nothing is missed silently.
"""

import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta

import store

FILE = store.STATE_DIR / "watchers.json"
TICK_SECONDS = 10
MIN_CHECK_MINUTES = 15
CONDITION_DAYS = 30  # condition watchers stop on their own after this long

_lock = threading.Lock()
_listeners: list[queue.Queue] = []
_backlog: list[dict] = []  # fired while no window was open
_running: set[str] = set()
_started = False

# set by main.py at start-up (avoids importing main here)
_check_condition = None  # (watcher) -> (met: bool, detail: str)
_popup = None  # (title, message) -> None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------
def _load() -> list[dict]:
    return store._read_json(FILE, [])


def _save(items: list[dict]) -> None:
    store._write_json(FILE, items)


def _update(wid: str, **changes) -> None:
    with _lock:
        items = _load()
        for w in items:
            if w["id"] == wid:
                w.update(changes)
        _save(items)


# ---------------------------------------------------------------------------
# Times
# ---------------------------------------------------------------------------
_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*$", re.I)


def _clock(text: str) -> tuple[int, int] | None:
    m = _TIME_RE.match(str(text or ""))
    if not m:
        return None
    h, mi, ampm = int(m.group(1)), int(m.group(2) or 0), (m.group(3) or "").lower()
    if ampm == "pm" and h < 12:
        h += 12
    if ampm == "am" and h == 12:
        h = 0
    return (h, mi) if 0 <= h < 24 and 0 <= mi < 60 else None


def parse_when(text: str, now: datetime | None = None) -> datetime | None:
    """'2026-11-17T09:00', '2026-11-17 9:00', '17 Nov 2026 9am', or just '21:30' (next time that comes)."""
    now = now or datetime.now()
    text = str(text or "").strip().replace("T", " ")
    clock = _clock(text)
    if clock:
        at = now.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        return at if at > now else at + timedelta(days=1)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d %b %Y %H:%M", "%d %B %Y %H:%M",
                "%d %b %Y", "%d %B %Y", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            at = datetime.strptime(text, fmt)
            return at.replace(hour=9) if "%H" not in fmt else at  # a date alone means 9 in the morning
        except ValueError:
            continue
    # "17 Nov 2026 9am"
    m = re.match(r"^(.*?)[\s,]+(\d{1,2}(?::\d{2})?\s*(?:am|pm))$", text, re.I)
    if m:
        day = parse_when(m.group(1), now)
        clock = _clock(m.group(2))
        if day and clock:
            return day.replace(hour=clock[0], minute=clock[1])
    return None


def _next_daily(hhmm: str, after: datetime) -> datetime:
    h, m = _clock(hhmm) or (8, 0)
    at = after.replace(hour=h, minute=m, second=0, microsecond=0)
    return at if at > after else at + timedelta(days=1)


def _fmt(at: datetime) -> str:
    today = datetime.now().date()
    day = ("today" if at.date() == today else "tomorrow" if at.date() == today + timedelta(days=1)
           else f"{at:%a %d %b}")
    return f"{day} at {at:%I:%M %p}".replace(" 0", " ")


def _new(kind: str, **fields) -> dict:
    return {"id": uuid.uuid4().hex[:6], "kind": kind, "active": True, "created": time.time(), "fired": 0, **fields}


def _add(w: dict) -> dict:
    with _lock:
        items = _load()
        items.append(w)
        _save(items)
    return w


# ---------------------------------------------------------------------------
# Creating (these are Karen's tools)
# ---------------------------------------------------------------------------
def _when(at: str, daily_at: str, every_minutes: float, min_minutes: float = 1) -> tuple[dict, datetime] | dict:
    """(repeat rule, first time), or {"error": ...}."""
    now = datetime.now()
    if daily_at:
        if not _clock(daily_at):
            return {"error": f"couldn't understand the time '{daily_at}'; use HH:MM like 08:00"}
        return {"daily_at": daily_at}, _next_daily(daily_at, now)
    if every_minutes:
        minutes = max(min_minutes, min(7 * 24 * 60, float(every_minutes)))
        return {"every_minutes": minutes}, now + timedelta(minutes=minutes)
    if at:
        first = parse_when(at)
        if not first:
            return {"error": f"couldn't understand the time '{at}'; use a date and time like 2026-11-17 09:00"}
        if first < now - timedelta(minutes=1):
            return {"error": f"{first:%d %b %Y %I:%M %p} is in the past"}
        return {}, first
    return {"error": "say when: at (a date and time), daily_at (HH:MM), or every_minutes"}


def _repeat_text(rule: dict, first: datetime) -> str:
    if rule.get("daily_at"):
        return f"every day at {first:%I:%M %p}".replace(" 0", " ")
    if rule.get("every_minutes"):
        return f"every {int(rule['every_minutes'])} minutes"
    return "once"


def add_alert(message: str, at: str = "", daily_at: str = "", every_minutes: float = 0) -> dict:
    """Say fixed words at a time, once or on repeat."""
    if re.search(r"(\.\.\.|…|\[[^\]]*\])\s*$", message.strip()) or not message.strip():
        return {"error": "'say' must be the actual words. To look something up or do something at that time, "
                         "use 'do' instead."}
    when = _when(at, daily_at, every_minutes)
    if isinstance(when, dict):
        return when
    rule, first = when
    w = _add(_new("alert", message=message.strip(), next_at=first.timestamp(), **rule))
    return {"will_say": w["message"], "first": _fmt(first), "repeats": _repeat_text(rule, first), "id": w["id"],
            "note": "if Karen is closed at that moment, she says it when she next opens"}


def add_routine(request: str, at: str = "", daily_at: str = "", every_minutes: float = 0) -> dict:
    """Do something at a time, once or on repeat, as if the user had asked then."""
    when = _when(at, daily_at, every_minutes, MIN_CHECK_MINUTES)
    if isinstance(when, dict):
        return when
    rule, first = when
    w = _add(_new("routine", request=request.strip(), next_at=first.timestamp(), **rule))
    return {"will_do": w["request"], "first": _fmt(first), "repeats": _repeat_text(rule, first), "id": w["id"]}


def schedule(say: str = "", do: str = "", at: str = "", daily_at: str = "", every_minutes: float = 0) -> dict:
    """Karen's one scheduling tool: fixed words to say, or something to do, at a time."""
    if bool(say.strip()) == bool(do.strip()):
        return {"error": "give exactly one of 'say' (fixed words) or 'do' (something to look up or do then)"}
    if do.strip():
        return add_routine(do, at=at, daily_at=daily_at, every_minutes=every_minutes)
    return add_alert(say, at=at, daily_at=daily_at, every_minutes=every_minutes)


def add_condition(condition: str, check: str, every_minutes: float = 60, message: str = "") -> dict:
    minutes = max(MIN_CHECK_MINUTES, min(24 * 60, float(every_minutes or 60)))
    w = _add(_new("condition", condition=condition.strip(), check=check.strip(), message=message.strip(),
                  every_minutes=minutes, next_at=time.time() + 5,  # first check right away
                  expires=time.time() + CONDITION_DAYS * 86400, last_detail=""))
    return {"watching_for": w["condition"], "checking": w["check"], "every_minutes": int(minutes), "id": w["id"],
            "note": f"checks first now, then every {int(minutes)} minutes while Karen is open, for up to "
                    f"{CONDITION_DAYS} days; stops after telling you once"}


def list_all() -> dict:
    items = [w for w in _load() if w.get("active")]
    if not items:
        return {"watchers": [], "note": "nothing scheduled or being watched"}
    out = []
    for w in items:
        when = _fmt(datetime.fromtimestamp(w["next_at"])) if w.get("next_at") else ""
        if w["kind"] == "alert":
            repeat = (f"daily at {w['daily_at']}" if w.get("daily_at")
                      else f"every {int(w['every_minutes'])} min" if w.get("every_minutes") else "once")
            out.append({"id": w["id"], "type": "alert", "say": w["message"], "when": when, "repeats": repeat})
        elif w["kind"] == "routine":
            repeat = (f"daily at {w['daily_at']}" if w.get("daily_at")
                      else f"every {int(w['every_minutes'])} min" if w.get("every_minutes") else "once")
            out.append({"id": w["id"], "type": "routine", "does": w["request"], "repeats": repeat, "next": when})
        else:
            out.append({"id": w["id"], "type": "watching", "for": w["condition"], "check": w["check"],
                        "every_minutes": int(w["every_minutes"]), "last_seen": w.get("last_detail", "")})
    return {"watchers": out}


def cancel(what: str) -> dict:
    words = [x for x in re.findall(r"\w+", str(what).lower()) if len(x) > 2]
    with _lock:
        items = _load()
        hits = [w for w in items if w.get("active") and (
            w["id"] == str(what).strip()
            or (words and all(x in " ".join(str(w.get(k, "")) for k in ("message", "request", "condition", "check"))
                              .lower() for x in words)))]
        for w in hits:
            w["active"] = False
        if hits:
            _save(items)
    if not hits:
        return {"error": f"nothing scheduled matches '{what}'", **list_all()}
    return {"cancelled": [w.get("message") or w.get("request") or w.get("condition") for w in hits]}


# ---------------------------------------------------------------------------
# Delivering
# ---------------------------------------------------------------------------
def subscribe() -> queue.Queue:
    q: queue.Queue = queue.Queue()
    with _lock:
        _listeners.append(q)
        for event in _backlog:
            q.put(event)
        _backlog.clear()
    return q


def unsubscribe(q: queue.Queue) -> None:
    with _lock:
        if q in _listeners:
            _listeners.remove(q)


def _deliver(event: dict) -> None:
    with _lock:
        listeners = list(_listeners)
        if not listeners:
            _backlog.append(event)
            del _backlog[:-20]
    for q in listeners:
        q.put(event)
    print(f"  watcher -> {event['kind']}: {event.get('say') or event.get('request')}")
    if not listeners and event["kind"] != "routine" and _popup:
        threading.Thread(target=_popup, args=("Karen", event["say"]), daemon=True).start()


# ---------------------------------------------------------------------------
# The scheduler
# ---------------------------------------------------------------------------
def _run(w: dict, now: datetime) -> None:
    try:
        late = now.timestamp() - w["next_at"] > 120
        if w["kind"] == "alert":
            say = w["message"]
            if late:
                say = f"Earlier reminder, from {datetime.fromtimestamp(w['next_at']):%I:%M %p}: {say}"
            _deliver({"id": w["id"], "kind": "alert", "say": say})
            nxt = (_next_daily(w["daily_at"], now) if w.get("daily_at")
                   else now + timedelta(minutes=w["every_minutes"]) if w.get("every_minutes") else None)
            _update(w["id"], fired=w["fired"] + 1, next_at=nxt.timestamp() if nxt else None, active=bool(nxt))

        elif w["kind"] == "routine":
            if w.get("daily_at"):
                nxt = _next_daily(w["daily_at"], now)
            elif w.get("every_minutes"):
                nxt = now + timedelta(minutes=w["every_minutes"])
            else:
                nxt = None
            # a routine that was due hours ago while Karen was closed is stale: skip to the next one
            if not late or now.timestamp() - w["next_at"] < 3600:
                _deliver({"id": w["id"], "kind": "routine", "request": w["request"]})
            _update(w["id"], fired=w["fired"] + 1, next_at=nxt.timestamp() if nxt else None, active=bool(nxt))

        elif w["kind"] == "condition":
            if time.time() > w.get("expires", 0):
                _update(w["id"], active=False)
                return
            met, detail = _check_condition(w) if _check_condition else (False, "no checker")
            nxt = now + timedelta(minutes=w["every_minutes"])
            if met:
                say = (w.get("message") or f"Heads up: {w['condition']}").rstrip()
                if detail:
                    say = f"{say}{'' if say.endswith(('.', '!', '?')) else '.'} {detail}"
                _deliver({"id": w["id"], "kind": "condition", "say": say})
                _update(w["id"], active=False, fired=w["fired"] + 1, last_detail=detail, last_checked=time.time())
            else:
                _update(w["id"], next_at=nxt.timestamp(), last_detail=detail, last_checked=time.time())
    except Exception as e:  # noqa: BLE001 - one broken watcher must not stop the others
        print(f"  watcher {w.get('id')} failed: {e}")
        _update(w["id"], next_at=time.time() + 15 * 60)
    finally:
        with _lock:
            _running.discard(w["id"])


def _loop() -> None:
    while True:
        now = datetime.now()
        for w in _load():
            if not w.get("active") or not w.get("next_at") or w["next_at"] > now.timestamp():
                continue
            with _lock:
                if w["id"] in _running:
                    continue
                _running.add(w["id"])
            threading.Thread(target=_run, args=(w, now), daemon=True).start()
        time.sleep(TICK_SECONDS)


def start(check_condition, popup) -> None:
    """Start the scheduler once (both of Karen's servers call this)."""
    global _started, _check_condition, _popup
    with _lock:
        if _started:
            return
        _started = True
    _check_condition, _popup = check_condition, popup
    threading.Thread(target=_loop, daemon=True, name="karen-watchers").start()
