"""Web search and news for ANVI (no API keys needed).

web_search: Bing results + the most relevant lines from the top pages, so the
            model gets actual facts/numbers, not just headlines.
get_news:   Google News RSS headlines.
"""

import base64
import html
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, unquote, urlparse
from xml.etree import ElementTree

from net import BROWSER_UA, http

HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9"}
PAGE_BUDGET = 900  # chars of page text per page (Groq free tier allows ~8k tokens/min)
PAGES_TO_READ = 3

_pool = ThreadPoolExecutor(max_workers=6)


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _real_url(href: str) -> str:
    """Bing wraps links as /ck/a?...&u=a1<base64url>; unwrap to the target URL."""
    href = html.unescape(href)
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if u.startswith("a1"):
        u = u[2:]
        try:
            return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode("utf-8", "replace")
        except ValueError:
            pass
    return href


STOPWORDS = {"the", "a", "an", "of", "in", "on", "for", "to", "is", "are", "was", "what", "who", "whats",
              "how", "much", "many", "today", "now", "current", "latest", "me", "tell", "and", "price", "rate"}


def _relevance(query: str, results: list[dict]) -> float:
    """Best share of query keywords found in any of the top results (0..1)."""
    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if w not in STOPWORDS and len(w) > 1}
    if not terms or not results:
        return 1.0 if results else 0.0
    best = 0.0
    for res in results[:4]:
        hay = f"{res['title']} {res['snippet']} {res['url']}".lower()
        best = max(best, sum(t in hay for t in terms) / len(terms))
    return best


# --- keyed providers (reliable; used first when a key is in .env) ----------
def _tavily(query: str) -> list[dict]:
    key = os.getenv("TAVILY_API_KEY")
    if not key:
        return []
    r = http.post("https://api.tavily.com/search", timeout=20, json={
        "api_key": key, "query": query, "max_results": 5, "search_depth": "basic",
        "include_answer": True,
    })
    r.raise_for_status()
    data = r.json()
    results = [{"title": x.get("title", ""), "url": x.get("url", ""), "snippet": x.get("content", "")[:PAGE_BUDGET],
                "page_extract": x.get("content", "")[:PAGE_BUDGET]} for x in data.get("results", [])]
    if data.get("answer") and results:
        results[0]["summary_answer"] = data["answer"]
    return results


def _brave_api(query: str) -> list[dict]:
    key = os.getenv("BRAVE_API_KEY")
    if not key:
        return []
    r = http.get("https://api.search.brave.com/res/v1/web/search", timeout=12,
                 params={"q": query, "count": 6, "country": "IN"},
                 headers={"X-Subscription-Token": key, "Accept": "application/json"})
    r.raise_for_status()
    return [{"title": _text(x.get("title", "")), "url": x.get("url", ""), "snippet": _text(x.get("description", ""))}
            for x in r.json().get("web", {}).get("results", [])]


# --- keyless providers (scrape result pages; any one may be blocked) ---------
ENGINE_HOSTS = ("bing.com", "microsoft.com", "duckduckgo.com", "brave.com", "google.com", "yahoo.com")


def _unwrap(href: str) -> str:
    href = _real_url(href)
    if "duckduckgo.com/l/" in href:
        return unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
    return href


def _links(page: str) -> list[dict]:
    """Generic result extraction: external links with a real title, in page order."""
    results, seen = [], set()
    for href, inner in re.findall(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', page, re.S):
        url = _unwrap(href)
        if not url.startswith("http"):
            continue
        host = urlparse(url).hostname or ""
        if any(host == h or host.endswith("." + h) for h in ENGINE_HOSTS):
            continue
        title = _text(inner)
        key = url.split("#")[0].rstrip("/")
        if len(title) < 12 or key in seen:
            continue
        seen.add(key)
        results.append({"title": title[:140], "url": url, "snippet": ""})
    return results


def _bing(query: str) -> list[dict]:
    r = http.get("https://www.bing.com/search", params={"q": query, "cc": "IN"}, headers=HEADERS, timeout=10)
    r.encoding = "utf-8"
    results = []
    for block in re.findall(r'<li class="b_algo".*?</li>', r.text, re.S):
        link = re.search(r'<h2[^>]*>\s*<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if link:
            snippet = re.search(r'<p[^>]*>(.*?)</p>', block, re.S)
            results.append({"title": _text(link.group(2)), "url": _real_url(link.group(1)),
                            "snippet": _text(snippet.group(1)) if snippet else ""})
    return results


def _duckduckgo(query: str) -> list[dict]:
    r = http.post("https://html.duckduckgo.com/html/", data={"q": query, "kl": "in-en"}, headers=HEADERS, timeout=10)
    return _links(r.text) if r.status_code == 200 else []


def _brave_html(query: str) -> list[dict]:
    r = http.get("https://search.brave.com/search", params={"q": query}, headers=HEADERS, timeout=10)
    r.encoding = "utf-8"
    return _links(r.text) if r.status_code == 200 else []


def _google_news(query: str) -> list[dict]:
    return [{"title": a["headline"], "url": "", "snippet": f"{a['source']}, {a['published']}"}
            for a in get_news(query)["articles"]]


PROVIDERS = [_tavily, _brave_api, _bing, _duckduckgo, _brave_html]


def find_results(query: str) -> tuple[list[dict], str]:
    fallback: tuple[list[dict], str] = ([], "")
    for provider in PROVIDERS:
        try:
            results = provider(query)
        except Exception as e:  # noqa: BLE001
            print(f"  search {provider.__name__} failed: {e}")
            continue
        if not results:
            continue
        if _relevance(query, results) >= 0.6 or provider in (_tavily, _brave_api):
            return results, provider.__name__.strip("_")
        fallback = fallback if fallback[0] else (results, provider.__name__.strip("_"))
    try:
        news = _google_news(query)
        if news:
            return news, "google_news"
    except Exception as e:  # noqa: BLE001
        print("  search google_news failed:", e)
    return fallback


def _page_lines(raw: str) -> list[str]:
    raw = re.sub(r"(?is)<(script|style|noscript|svg|head|nav|footer|form|iframe)[^>]*>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)</t[dh]>", " | ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</(p|div|li|tr|h[1-6]|table|section|article)>", "\n", raw)
    text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"[ \t |]*\|[ \t |]*", " | ", re.sub(r"[ \t ]+", " ", line)).strip(" |")
        if len(line) >= 12:
            lines.append(line)
    return lines


def _relevant_extract(url: str, query: str) -> str:
    try:
        r = http.get(url, headers=HEADERS, timeout=(3, 5))
        if not r.ok or "html" not in r.headers.get("content-type", ""):
            return ""
        if not r.encoding or r.encoding.lower() == "iso-8859-1":
            r.encoding = r.apparent_encoding or "utf-8"
        lines = _page_lines(r.text[:1_500_000])
    except Exception:  # noqa: BLE001  (a slow or broken page just gets skipped)
        return ""

    terms = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2}
    scored = []
    for i, line in enumerate(lines):
        low = line.lower()
        score = sum(t in low for t in terms) * 2 + (2 if re.search(r"\d", line) else 0)
        score += 1 if re.search(r"[₹$€£%]|rs\.?\s*\d", low) else 0
        if len(line) > 400:
            score -= 2
        if score >= 3:
            scored.append((score, i, line[:300]))

    picked, used = [], 0
    for score, i, line in sorted(scored, key=lambda s: (-s[0], s[1])):
        if used + len(line) > PAGE_BUDGET:
            continue
        picked.append((i, line))
        used += len(line)
    return "\n".join(line for _, line in sorted(picked))


def web_search(query: str) -> dict:
    results, source = find_results(query)
    if not results:
        return {"query": query, "error": "search is unavailable right now"}

    to_read = [r for r in results[:PAGES_TO_READ] if r["url"] and "page_extract" not in r]
    futures = [(r, _pool.submit(_relevant_extract, r["url"], query)) for r in to_read]
    for res, fut in futures:
        try:
            extract = fut.result(timeout=7)
        except Exception:  # noqa: BLE001
            extract = ""
        if extract:
            res["page_extract"] = extract
    for res in results:
        res["snippet"] = res["snippet"][:220]
    print(f"  search via {source}: {len(results)} results, {sum('page_extract' in r for r in results)} pages read")
    return {"query": query, "results": results[:5]}


def get_news(topic: str = "") -> dict:
    if topic:
        url = "https://news.google.com/rss/search"
        params = {"q": topic, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    else:
        url = "https://news.google.com/rss"
        params = {"hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    r = http.get(url, params=params, headers=HEADERS, timeout=12)
    r.raise_for_status()
    root = ElementTree.fromstring(r.content)
    items = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        source = item.findtext("source") or ""
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3]
        items.append({"headline": title, "source": source, "published": (item.findtext("pubDate") or "")[:22]})
        if len(items) >= 8:
            break
    return {"topic": topic or "top stories India", "articles": items}
