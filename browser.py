"""Karen's hands on websites: her own Chrome window, driven through Chrome's remote-debugging protocol.

The window is visible on purpose - the user sees every click and can take over (log in, solve a CAPTCHA,
pay). It uses its own profile in %LOCALAPPDATA%\\Karen\\browser-profile, so a site the user logs into once
stays logged in for Karen, and Karen never touches the user's everyday browser profile.

Two rules hold no matter what the model asks for: Karen never presses a button that pays or places an
order, and never types into password, card, CVV, OTP or UPI fields. Those are always the user's to do.
"""

import base64
import json
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import quote_plus

import requests
import websocket

import store

PORT = 9223
PROFILE = store.STATE_DIR / "browser-profile"
CHROME_PATHS = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path.home() / r"AppData\Local\Google\Chrome\Application\chrome.exe",
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]
MAX_ELEMENTS = 55
MAX_TEXT = 3000

# the user's job, always
PAY_WORDS = re.compile(
    r"\b(pay|payment|place (?:your )?order|confirm (?:and )?(?:order|pay|booking|purchase)|buy now|complete "
    r"(?:order|purchase|booking)|proceed to pay|checkout and pay|make payment|purchase|subscribe now|"
    r"donate|transfer|send money)\b", re.I)
SECRET_FIELD = re.compile(r"\bpassword|passcode|passwd|pwd|\botp\b|one.?time|cvv|cvc|card.?(number|no)|cc-|"
                          r"security.?code|upi|\bpin\b|mpin|ssn|aadhaar", re.I)

_lock = threading.Lock()
_elements: dict[int, dict] = {}  # number -> what was shown to the model last


# ---------------------------------------------------------------------------
# Chrome
# ---------------------------------------------------------------------------
def _chrome_exe() -> str:
    for p in CHROME_PATHS:
        if p.exists():
            return str(p)
    raise RuntimeError("Chrome isn't installed")


def _targets() -> list[dict]:
    return requests.get(f"http://127.0.0.1:{PORT}/json", timeout=2).json()


def _ensure_browser() -> dict:
    """Start Karen's Chrome window if it isn't running; return its page target."""
    try:
        pages = [t for t in _targets() if t.get("type") == "page"]
    except requests.RequestException:
        PROFILE.mkdir(parents=True, exist_ok=True)
        subprocess.Popen([
            _chrome_exe(), f"--remote-debugging-port={PORT}", f"--user-data-dir={PROFILE}",
            "--remote-allow-origins=*", "--no-first-run", "--no-default-browser-check",
            "--window-size=1280,900", "--disable-features=Translate", "about:blank",
        ])
        pages = []
        for _ in range(40):
            time.sleep(0.25)
            try:
                pages = [t for t in _targets() if t.get("type") == "page"]
                if pages:
                    break
            except requests.RequestException:
                continue
    if not pages:
        pages = [requests.put(f"http://127.0.0.1:{PORT}/json/new?about:blank", timeout=3).json()]
    # the tab the user is looking at is usually the first non-devtools page
    return next((p for p in pages if not p.get("url", "").startswith("devtools://")), pages[0])


class _Page:
    def __init__(self, target: dict):
        self.ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=30)
        self.n = 0

    def call(self, method: str, **params) -> dict:
        self.n += 1
        mine = self.n
        self.ws.send(json.dumps({"id": mine, "method": method, "params": params}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == mine:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "browser error"))
                return msg.get("result", {})

    def send(self, method: str, **params) -> None:
        """Fire a command without waiting for its answer (navigation can take a while to start)."""
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))

    def js(self, expression: str):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text", "script error"))
        return r.get("result", {}).get("value")

    def settle(self, timeout: float = 20) -> None:
        """Wait for the page to finish loading, then a moment for scripts to draw it."""
        end = time.time() + timeout
        time.sleep(0.4)
        while time.time() < end:
            try:
                if self.js("document.readyState") == "complete":
                    break
            except Exception:  # noqa: BLE001 - the page is mid-navigation
                pass
            time.sleep(0.3)
        time.sleep(0.8)

    def close(self):
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass


def _page() -> _Page:
    return _Page(_ensure_browser())


# ---------------------------------------------------------------------------
# Reading a page: visible text + numbered things you can click or type into
# ---------------------------------------------------------------------------
SNAPSHOT_JS = r"""
(() => {
  const MAX = %d;
  window.__karenNext = window.__karenNext || 1;
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 4 || r.height < 4) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && +s.opacity > 0.05;
  };
  const label = (el) => {
    const bits = [el.getAttribute('aria-label'), el.innerText, el.value, el.getAttribute('placeholder'),
                  el.getAttribute('title'), el.getAttribute('alt'), el.getAttribute('name')];
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) bits.unshift(l.innerText); }
    const img = el.querySelector && el.querySelector('img[alt]');
    if (img) bits.push(img.alt);
    return (bits.find(b => b && b.trim()) || '').replace(/\s+/g, ' ').trim().slice(0, 90);
  };
  const sel = 'a[href], button, input:not([type=hidden]), textarea, select, [role=button], [role=link], ' +
              '[role=tab], [role=menuitem], [role=checkbox], [role=option], [onclick], [contenteditable=true]';
  const popupSel = '[role=listbox], [role=menu], [class*=autocomplete], [class*=suggest], [class*=dropdown-menu], ' +
                   '[class*=typeahead], [class*=options], ul[id*=list]';
  const popupItems = [...document.querySelectorAll(popupSel)].filter(visible)
    .flatMap(p => [...p.querySelectorAll('li, [role=option], [role=menuitem], a, button')]).filter(visible);
  const all = [...new Set([...popupItems, ...document.querySelectorAll(sel)])].filter(visible);
  const inView = (el) => { const r = el.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight; };
  const rank = (el) => {
    if (popupItems.includes(el)) return 0;
    const tag = el.tagName.toLowerCase();
    if (['input', 'textarea', 'select'].includes(tag) || el.isContentEditable) return 1;
    if (tag === 'button' || el.getAttribute('role') === 'button') return 2;
    return 3;
  };
  all.sort((a, b) => (inView(b) - inView(a)) || (rank(a) - rank(b)));
  const items = [];
  for (const el of all) {
    if (items.length >= MAX) break;
    const text = label(el) || (popupItems.includes(el) ? (el.textContent || '').trim().slice(0, 90) : '');
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (!text && !['input', 'textarea', 'select'].includes(tag)) continue;
    let n = +el.getAttribute('data-karen');
    if (!n) { n = window.__karenNext++; el.setAttribute('data-karen', n); }
    items.push({n, tag, type, text,
                field: ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable,
                secret: [type, el.name, el.id, el.getAttribute('autocomplete'), el.getAttribute('placeholder'),
                         el.getAttribute('aria-label')].join(' ')});
  }
  // the page's own content area, not its menus: the first likely container with real text in it
  const candidates = ['main', '[role=main]', '#search', '#center_col', '#content', '#main', 'article', '.content'];
  let main = document.body;
  for (const c of candidates) {
    const el = document.querySelector(c);
    if (el && el.innerText && el.innerText.trim().length > 400) { main = el; break; }
  }
  return {title: document.title, url: location.href,
          text: (main.innerText || '').replace(/\n{3,}/g, '\n\n').slice(0, %d), items,
          scroll: Math.round(scrollY), height: document.body.scrollHeight};
})()
"""


def _snapshot(page: _Page, note: str = "") -> dict:
    data = page.js(SNAPSHOT_JS % (MAX_ELEMENTS, MAX_TEXT)) or {}
    _elements.clear()
    shown = []
    for it in data.get("items", []):
        it["secret"] = bool(SECRET_FIELD.search(it.get("secret", "")))
        _elements[it["n"]] = it
        kind = ("field" if it["field"] else "link" if it["tag"] == "a" else "button")
        extra = " (password/card/OTP - the user must fill it)" if it["secret"] and it["field"] else ""
        shown.append(f"[{it['n']}] {kind}: {it['text'] or it['type'] or 'unlabelled'}{extra}")
    out = {"title": data.get("title", ""), "url": data.get("url", ""), "page_text": data.get("text", ""),
           "elements": shown,
           "how_to_use": "Click or type by element number. Numbers stay the same while you're on this page. "
                         "After typing into a box that suggests options, click the matching suggestion (listed "
                         "first)."}
    if note:
        out["note"] = note
    if data.get("height", 0) > data.get("scroll", 0) + 1200:
        out["more_below"] = True
    return out


ELEMENT_JS = r"""
(() => {
  const el = document.querySelector('[data-karen="%d"]');
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  return {n: %d, tag, type: (el.getAttribute('type') || '').toLowerCase(),
          text: (el.getAttribute('aria-label') || el.innerText || el.value || el.getAttribute('placeholder') ||
                 '').replace(/\s+/g, ' ').trim().slice(0, 90),
          field: ['input', 'textarea', 'select'].includes(tag) || el.isContentEditable,
          secret: [el.getAttribute('type'), el.name, el.id, el.getAttribute('autocomplete'),
                   el.getAttribute('placeholder'), el.getAttribute('aria-label')].join(' ')};
})()
"""


def _target(target, page: "_Page | None" = None) -> dict:
    """An element by its number, or by words of its label."""
    if isinstance(target, (int, float)) or str(target).strip().isdigit():
        n = int(target)
        it = _elements.get(n)
        if not it and page:
            it = page.js(ELEMENT_JS % (n, n))
            if it:
                it["secret"] = bool(SECRET_FIELD.search(it.get("secret", "")))
        if not it:
            raise ValueError(f"no element [{target}] on the current page; read the page again")
        return it
    words = str(target).lower().strip()
    best = [it for it in _elements.values() if words and words in it["text"].lower()]
    if not best:
        raise ValueError(f"nothing labelled '{target}' on the page; use an element number")
    return min(best, key=lambda it: len(it["text"]))


def _center(page: _Page, n: int) -> tuple[float, float]:
    box = page.js(f"""(() => {{
      const el = document.querySelector('[data-karen="{n}"]');
      if (!el) return null;
      const link = el.closest('a');
      if (link) link.removeAttribute('target');  // keep Karen in the tab she is reading
      el.scrollIntoView({{block: 'center', inline: 'center'}});
      const r = el.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2];
    }})()""")
    if not box:
        raise ValueError("that element is gone (the page changed); read the page again")
    return box[0], box[1]


def _mouse_click(page: _Page, x: float, y: float) -> None:
    for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
        page.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)


# ---------------------------------------------------------------------------
# Karen's tools
# ---------------------------------------------------------------------------
def _run(action):
    with _lock:
        for attempt in (1, 2):
            page = None
            try:
                page = _page()
                return action(page)
            except ValueError as e:
                return {"error": str(e)}
            except Exception as e:  # noqa: BLE001 - usually a page still loading: wait and try once more
                if attempt == 2:
                    return {"error": f"browser problem: {e}"}
                time.sleep(1.5)
            finally:
                if page:
                    page.close()


def browse_open(url_or_search: str) -> dict:
    """Open a website (or a Google search) in Karen's browser window and read it."""
    target = url_or_search.strip()
    looks_like_url = re.match(r"^(https?://|data:)", target) or re.match(r"^[\w-]+(\.[\w-]+)+(/\S*)?$", target)
    url = (target if re.match(r"^(https?://|data:)", target) else f"https://{target}") if looks_like_url \
        else f"https://www.google.com/search?q={quote_plus(target)}"

    def go(page):
        page.call("Page.enable")
        page.call("Page.bringToFront")
        page.send("Page.navigate", url=url)
        time.sleep(1.0)  # let the old page go away before checking readiness
        page.settle()
        return _snapshot(page)

    return _run(go)


def browse_read() -> dict:
    """Read the current page again (after it changed, or to see the numbered elements)."""
    return _run(_snapshot)


def browse_click(target) -> dict:
    """Click a link or button by its element number (or label words)."""
    def click(page):
        it = _target(target, page)
        if PAY_WORDS.search(it["text"]):
            return {"stopped": f"'{it['text']}' would pay or place an order. Karen never does that: tell the user "
                               "everything is ready and that they need to press it themselves in the browser window."}
        x, y = _center(page, it["n"])
        _mouse_click(page, x, y)
        page.settle()
        return _snapshot(page, note=f"clicked [{it['n']}] {it['text']}")

    return _run(click)


def browse_type(target, text: str, submit: bool = False) -> dict:
    """Type into a field by its element number (or label words); submit presses Enter."""
    def type_in(page):
        it = _target(target, page)
        if not it["field"]:
            raise ValueError(f"[{it['n']}] isn't a text field")
        if it["secret"]:
            return {"stopped": "That's a password, card, OTP or PIN field. Karen never types those: ask the user to "
                               "fill it in themselves in the browser window."}
        x, y = _center(page, it["n"])
        _mouse_click(page, x, y)
        page.js(f"""(() => {{
          const el = document.querySelector('[data-karen="{it['n']}"]');
          el.focus();
          if ('value' in el) {{
            const setter = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
            setter && setter.set ? setter.set.call(el, '') : (el.value = '');
            el.dispatchEvent(new Event('input', {{bubbles: true}}));
          }}
        }})()""")
        page.call("Input.insertText", text=str(text))
        if submit:
            for kind in ("keyDown", "keyUp"):
                page.call("Input.dispatchKeyEvent", type=kind, key="Enter", code="Enter",
                          windowsVirtualKeyCode=13, nativeVirtualKeyCode=13, **({"text": "\r"} if kind == "keyDown" else {}))
            page.settle()
        else:
            time.sleep(1.4)  # suggestion lists appear while typing
        return _snapshot(page, note=f"typed into [{it['n']}] {it['text']}" + (" and pressed Enter" if submit else ""))

    return _run(type_in)


def browse_scroll(direction: str = "down") -> dict:
    """Scroll the page down or up to see more."""
    def scroll(page):
        dy = -800 if str(direction).lower().startswith("u") else 800
        page.js(f"window.scrollBy(0, {dy})")
        time.sleep(0.8)
        return _snapshot(page, note=f"scrolled {'up' if dy < 0 else 'down'}")

    return _run(scroll)


def browse_back() -> dict:
    """Go back to the previous page."""
    def back(page):
        page.js("history.back()")
        page.settle()
        return _snapshot(page, note="went back")

    return _run(back)


def browse_look(question: str = "") -> dict:
    """Screenshot the page and ask the vision model about it (images, layouts, checking what's on screen)."""
    import vision

    def look(page):
        shot = page.call("Page.captureScreenshot", format="jpeg", quality=75)
        jpeg = base64.b64decode(shot["data"])
        return {"looked_at": page.js("location.href"),
                **vision.ask_image(jpeg, question or "What does this web page show? Read the important parts.")}

    return _run(look)
