"""
core/sitesearch.py — "search YouTube for …" searches ON that site.

Turns (site, query) into the address of that site's own results page, so the
user lands on YouTube's results, Amazon's results, Wikipedia's article search —
not on a Google page about it.

How a site is resolved, in order:
  1. A small table of well-known sites (fast, no network).
  2. The site's OpenSearch description — the standard
     <link rel="search" type="application/opensearchdescription+xml"> that
     browsers use to add a site's search box. Works for any site that
     publishes one (most shops, wikis, forums, news sites). Results are cached.
  3. Otherwise a web search restricted to the site (`site:example.com`), and
     the caller is told it is that, so nobody claims it searched the site.

Network calls use short timeouts and never raise; this is called from a
worker thread (browser_control), never the Qt thread.
"""
from __future__ import annotations

import re
import threading
import urllib.parse
from html.parser import HTMLParser
from xml.etree import ElementTree as ET

# Well-known sites: key words people say -> results URL with {q}.
_KNOWN: dict[str, str] = {
    "youtube":       "https://www.youtube.com/results?search_query={q}",
    "youtube music": "https://music.youtube.com/search?q={q}",
    "google":        "https://www.google.com/search?q={q}",
    "google images": "https://www.google.com/search?tbm=isch&q={q}",
    "google maps":   "https://www.google.com/maps/search/{q}",
    "maps":          "https://www.google.com/maps/search/{q}",
    "google news":   "https://news.google.com/search?q={q}",
    "wikipedia":     "https://en.wikipedia.org/w/index.php?search={q}",
    "github":        "https://github.com/search?q={q}",
    "stack overflow": "https://stackoverflow.com/search?q={q}",
    "stackoverflow": "https://stackoverflow.com/search?q={q}",
    "reddit":        "https://www.reddit.com/search/?q={q}",
    "x":             "https://x.com/search?q={q}",
    "twitter":       "https://x.com/search?q={q}",
    "amazon":        "https://www.amazon.com/s?k={q}",
    "ebay":          "https://www.ebay.com/sch/i.html?_nkw={q}",
    "etsy":          "https://www.etsy.com/search?q={q}",
    "aliexpress":    "https://www.aliexpress.com/wholesale?SearchText={q}",
    "imdb":          "https://www.imdb.com/find/?q={q}",
    "spotify":       "https://open.spotify.com/search/{q}",
    "soundcloud":    "https://soundcloud.com/search?q={q}",
    "twitch":        "https://www.twitch.tv/search?term={q}",
    "netflix":       "https://www.netflix.com/search?q={q}",
    "pinterest":     "https://www.pinterest.com/search/pins/?q={q}",
    "linkedin":      "https://www.linkedin.com/search/results/all/?keywords={q}",
    "instagram":     "https://www.instagram.com/explore/search/keyword/?q={q}",
    "tiktok":        "https://www.tiktok.com/search?q={q}",
    "facebook":      "https://www.facebook.com/search/top/?q={q}",
    "duckduckgo":    "https://duckduckgo.com/?q={q}",
    "bing":          "https://www.bing.com/search?q={q}",
    "pypi":          "https://pypi.org/search/?q={q}",
    "npm":           "https://www.npmjs.com/search?q={q}",
    "steam":         "https://store.steampowered.com/search/?term={q}",
}

# Domain (without www.) -> key above, so "amazon.com" or "youtu.be" also hit
# the table. A country domain of a known shop keeps the user's domain.
_DOMAIN_KEYS = {
    "youtube.com": "youtube", "youtu.be": "youtube", "m.youtube.com": "youtube",
    "music.youtube.com": "youtube music", "github.com": "github",
    "stackoverflow.com": "stackoverflow", "reddit.com": "reddit",
    "x.com": "x", "twitter.com": "x", "imdb.com": "imdb",
    "open.spotify.com": "spotify", "spotify.com": "spotify",
    "twitch.tv": "twitch", "netflix.com": "netflix", "etsy.com": "etsy",
    "pinterest.com": "pinterest", "linkedin.com": "linkedin",
    "instagram.com": "instagram", "tiktok.com": "tiktok",
    "facebook.com": "facebook", "pypi.org": "pypi", "npmjs.com": "npm",
    "store.steampowered.com": "steam", "soundcloud.com": "soundcloud",
}
# Shops whose search path is the same on every country domain.
_SAME_PATH_ON_EVERY_TLD = {
    "amazon": "/s?k={q}",
    "ebay": "/sch/i.html?_nkw={q}",
    "wikipedia": "/w/index.php?search={q}",
}

_cache: dict[str, str | None] = {}
_cache_lock = threading.Lock()
_UA = "Mozilla/5.0 (assistant site search)"


class _LinkFinder(HTMLParser):
    """Collects <link rel="search" type="application/opensearchdescription+xml">."""

    def __init__(self):
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if ("search" in a.get("rel", "").lower().split()
                and "opensearchdescription" in a.get("type", "").lower()
                and a.get("href")):
            self.hrefs.append(a["href"])


def _norm_site(site: str) -> str:
    s = (site or "").strip().lower()
    s = re.sub(r"^https?://", "", s).split("/")[0]
    return s[4:] if s.startswith("www.") else s


def _looks_like_domain(s: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)+", s))


def _from_table(site: str) -> str | None:
    if site in _KNOWN:
        return _KNOWN[site]
    if site in _DOMAIN_KEYS:
        return _KNOWN[_DOMAIN_KEYS[site]]
    if _looks_like_domain(site):
        # amazon.fr, fr.wikipedia.org, ebay.de … keep the user's country.
        labels = site.split(".")
        for brand, path in _SAME_PATH_ON_EVERY_TLD.items():
            if brand in labels:
                # Shops answer on www.; Wikipedia's language is the subdomain.
                host = site if brand == "wikipedia" else "www." + site
                return f"https://{host}{path}"
    return None


def _opensearch(domain: str) -> str | None:
    """The site's own search template from its OpenSearch description."""
    with _cache_lock:
        if domain in _cache:
            return _cache[domain]
    template = None
    try:
        import requests
        home = f"https://{domain}/"
        r = requests.get(home, timeout=4, headers={"User-Agent": _UA})
        finder = _LinkFinder()
        finder.feed(r.text[:400_000])
        for href in finder.hrefs[:2]:
            osd_url = urllib.parse.urljoin(r.url or home, href)
            x = requests.get(osd_url, timeout=4, headers={"User-Agent": _UA})
            root = ET.fromstring(x.content)
            for el in root.iter():
                if el.tag.split("}")[-1] != "Url":
                    continue
                typ = (el.get("type") or "").lower()
                tpl = el.get("template") or ""
                if typ == "text/html" and "{searchTerms}" in tpl:
                    template = tpl
                    break
            if template:
                break
    except Exception as e:
        print(f"[sitesearch] no OpenSearch for {domain}: {e}")
    if template:
        # Drop optional parameters ({startPage?} …) and fill required ones we
        # know nothing about with nothing.
        template = re.sub(r"\{[^}]*\?\}", "", template)
        template = template.replace("{searchTerms}", "{q}")
        template = re.sub(r"\{(?!q\})[^}]*\}", "", template)
    with _cache_lock:
        _cache[domain] = template
    return template


def resolve(site: str, query: str) -> tuple[str, str]:
    """(url, how). how is 'site' when the URL is the site's own search page,
    'web' when it fell back to a web search restricted to that site."""
    s = _norm_site(site)
    q_enc = urllib.parse.quote_plus((query or "").strip())
    if not s:
        return f"https://www.google.com/search?q={q_enc}", "web"

    tpl = _from_table(s)
    if tpl is None and _looks_like_domain(s):
        tpl = _opensearch(s)
    if tpl is None and not _looks_like_domain(s) and re.fullmatch(r"[a-z0-9-]+", s):
        # A bare brand name we do not know ("search Decathlon for …"): try
        # its .com — only accepted if that site really publishes a search.
        tpl = _opensearch(f"{s}.com")
        if tpl:
            s = f"{s}.com"
    if tpl:
        # quote_plus suits query strings; a path segment ({q} after '/') wants %20.
        before = tpl.split("{q}")[0]
        q = (urllib.parse.quote((query or "").strip())
             if "?" not in before and "#" not in before else q_enc)
        return tpl.replace("{q}", q), "site"

    target = s if _looks_like_domain(s) else site.strip()
    restrict = (f"site:{target} " if _looks_like_domain(s) else f"{target} ")
    return ("https://www.google.com/search?q="
            + urllib.parse.quote_plus(restrict + (query or "").strip())), "web"
