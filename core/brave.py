"""
core/brave.py — web, news and image results from Brave Search, paid for by
the user's OWN API key (free tier: about 2,000 queries a month).

    from core import brave
    if brave.available():
        hits = brave.search("news", "solar eclipse", count=8)
        # -> [{"title", "url", "snippet", "source"}, ...]  or None

WHY A THIRD SOURCE
    actions/web_search.py had two: Gemini grounding (its own small quota,
    which runs dry and then answers 429 for fifteen minutes) and DuckDuckGo
    through ddgs (no key, but scraped, so it rate-limits and changes shape
    without warning). A Brave key is a documented JSON API with a stable
    format. When a key is set, it is asked first; when it is not, nothing in
    the app changes.

CONTRACT
    search() never raises. It returns a list (possibly empty: Brave answered
    and found nothing) or None (no key, refused key, network error, timeout,
    a reply that is not what Brave sends). Callers treat None as "try the
    next source".

    The key travels only in the X-Subscription-Token header — never in the
    address, never in a log line, never in an error message. Messages here
    name the HTTP status, nothing else from the request.

    Results are text from whoever published the pages. This module only
    shapes them; the caller wraps whatever reaches the model in
    core.outcome.untrusted (web_search does so for every mode).
"""
from __future__ import annotations

import html
import locale
import os
import re
import threading
from typing import Optional

ENDPOINT = "https://api.search.brave.com/res/v1/{kind}/search"
KINDS = ("web", "news", "images")
MAX_QUERY_CHARS = 400            # Brave rejects longer queries
MAX_COUNT = {"web": 20, "news": 50, "images": 100}
TIMEOUT_S = 8.0

# Brave names a few languages its own way; everything else is the ISO code.
_LANG_NAMES = {"ja": "jp", "zh": "zh-hans"}

# Tests put an httpx transport here so no request leaves the computer.
_transport = None

_state = threading.local()


def last_error() -> str:
    """Why the latest search() on this thread returned None ('' if it did not)."""
    return getattr(_state, "error", "")


def _fail(why: str) -> None:
    _state.error = why
    print(f"[Brave] {why}")
    return None


def _key() -> str:
    try:
        from memory.config_manager import get_brave_key
        return get_brave_key()
    except Exception:
        return ""


def available() -> bool:
    """True when the user has stored a Brave Search key."""
    return bool(_key())


# ── where the user is ────────────────────────────────────────────────────────

def _system_locale() -> str:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        v = os.environ.get(name, "")
        if v and v not in ("C", "POSIX") and not v.startswith("C."):
            return v
    try:
        got = locale.getlocale()[0] or ""
    except Exception:
        got = ""
    return got


def region() -> tuple[str, str]:
    """(country, language) for this user: the country from the system locale
    ("fr_CH.UTF-8" -> "CH"), the language from the first active language and
    otherwise from the locale. Either may be '' when unknown."""
    loc = _system_locale().split(".")[0].replace("-", "_")
    parts = loc.split("_")
    country = parts[1] if len(parts) > 1 else ""
    language = parts[0] if parts and parts[0] else ""
    try:
        from memory.config_manager import get_active_languages
        active = get_active_languages()
        if active:
            language = active[0]
    except Exception:
        pass
    return country, language


def _country(value: str) -> str:
    v = str(value or "").strip().upper()
    return v if re.fullmatch(r"[A-Z]{2}", v) else ""


def _language(value: str) -> str:
    v = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    if not re.fullmatch(r"[a-z]{2,3}", v):
        return ""
    return _LANG_NAMES.get(v, v)


# ── the request ──────────────────────────────────────────────────────────────

def build_params(kind: str, query: str, count: int = 10,
                 country: str = "", language: str = "") -> Optional[dict]:
    """The query string for one request, or None for an empty query."""
    q = " ".join(str(query or "").split())[:MAX_QUERY_CHARS].strip()
    if not q:
        return None
    try:
        n = int(count)
    except (TypeError, ValueError):
        n = 10
    params = {"q": q, "count": max(1, min(n, MAX_COUNT.get(kind, 20)))}
    c, lang = _country(country), _language(language)
    if c:
        params["country"] = c
    if lang:
        params["search_lang"] = lang
    if kind != "images":            # images take no decorations flag
        params["text_decorations"] = "false"
    return params


def _headers(key: str) -> dict:
    return {"Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": key}


_TAGS = re.compile(r"<[^>]+>")


def _clean(text) -> str:
    return " ".join(html.unescape(_TAGS.sub("", str(text or ""))).split())


def _host(url: str) -> str:
    m = re.match(r"https?://([^/:?#]+)", url or "", re.I)
    host = (m.group(1) if m else "").lower()
    return host[4:] if host.startswith("www.") else host


def _http_link(url) -> bool:
    return isinstance(url, str) and re.match(r"https?://\S", url, re.I) is not None


def normalise(kind: str, data) -> Optional[list[dict]]:
    """Brave's reply -> [{title, url, snippet, source}] (+ "image" for
    images). Anything that is not an http(s) link is left out. None when the
    reply does not have the shape Brave sends."""
    if not isinstance(data, dict):
        return None
    rows = (data.get("web") or {}).get("results") if kind == "web" else data.get("results")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        return None
    out: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        url = r.get("url")
        if not _http_link(url):
            continue
        meta = r.get("meta_url") if isinstance(r.get("meta_url"), dict) else {}
        host = str(meta.get("hostname") or "").lower()
        if host.startswith("www."):
            host = host[4:]
        item = {"title": _clean(r.get("title")),
                "url": url,
                "snippet": _clean(r.get("description")),
                "source": host or _host(url)}
        if kind == "images":
            props = r.get("properties") if isinstance(r.get("properties"), dict) else {}
            img = props.get("image") or props.get("url")
            if not _http_link(img):
                continue
            item["image"] = img
        out.append(item)
    return out


def _auth_error(r) -> bool:
    """True when Brave's error body says the key itself is the problem."""
    try:
        err = (r.json() or {}).get("error") or {}
    except Exception:
        return False
    if not isinstance(err, dict):
        return False
    code = str(err.get("code") or "").upper()
    meta = err.get("meta") if isinstance(err.get("meta"), dict) else {}
    return "TOKEN" in code or str(meta.get("component") or "").lower() == "authentication"


def search(kind: str = "web", query: str = "", count: int = 10,
           country: Optional[str] = None, language: Optional[str] = None,
           key: Optional[str] = None) -> Optional[list[dict]]:
    """Ask Brave. `country` / `language` default to region(). Never raises;
    None means "no answer from Brave" (see last_error())."""
    _state.error = ""
    if kind not in KINDS:
        return _fail(f"unknown kind {kind!r}")
    key = (key if key is not None else _key()).strip()
    if not key:
        return _fail("no Brave Search key is set")
    if country is None or language is None:
        c, lang = region()
        country = c if country is None else country
        language = lang if language is None else language
    params = build_params(kind, query, count, country, language)
    if params is None:
        return _fail("an empty query was not sent")
    try:
        import httpx
        from core import tls
        try:
            verify = tls.context()
        except Exception:
            verify = True
        opts = {"timeout": TIMEOUT_S, "headers": _headers(key)}
        if _transport is not None:
            opts["transport"] = _transport
        else:
            opts["verify"] = verify
        with httpx.Client(**opts) as client:
            url = ENDPOINT.format(kind=kind)
            r = client.get(url, params=params)
            # Brave answers 422 both to a key it does not know (code
            # SUBSCRIPTION_TOKEN_INVALID, seen for real) and to a country or
            # language it does not know. The second is asked again without
            # them rather than losing the search.
            if r.status_code == 422 and not _auth_error(r) \
                    and ("country" in params or "search_lang" in params):
                params.pop("country", None)
                params.pop("search_lang", None)
                r = client.get(url, params=params)
            if r.status_code in (401, 403) or (r.status_code == 422 and _auth_error(r)):
                return _fail(f"Brave refused the key (HTTP {r.status_code})")
            if r.status_code == 429:
                return _fail("Brave's rate limit or monthly allowance is used up (HTTP 429)")
            if r.status_code != 200:
                return _fail(f"Brave answered HTTP {r.status_code}")
            data = r.json()
    except Exception as e:
        # Only the exception's type: some carry the request, and with it the header.
        return _fail(f"Brave could not be reached ({type(e).__name__})")
    rows = normalise(kind, data)
    if rows is None:
        return _fail("Brave's reply was not in the expected form")
    return rows


def probe(key: Optional[str] = None) -> tuple[bool, str]:
    """One tiny query, for the TEST button and the self-test."""
    rows = search("web", "weather", count=1, key=key)
    if rows is None:
        return False, last_error() or "no answer"
    return True, f"Brave answered ({len(rows)} result{'s' if len(rows) != 1 else ''})"
