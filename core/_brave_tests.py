"""
Self-test for Brave Search with the user's own key (core/brave.py) and its
use in actions/web_search.py. No request leaves the computer: httpx gets a
stand-in transport.

    python -m core._brave_tests            # offline
    python -m core._brave_tests --live     # also one real query, if a key is set
"""
from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core import brave  # noqa: E402

KEY = "BSA-test-key-0123456789abcdef"
_failures: list[str] = []
_log = io.StringIO()


def check(name: str, cond: bool, detail: str = "") -> None:
    sys.__stdout__.write(f"{'PASS' if cond else 'FAIL'}  {name}"
                         + (f"  — {detail}" if detail and not cond else "") + "\n")
    if not cond:
        _failures.append(name)


class Fake:
    """A transport that records each request and answers with `reply`."""

    def __init__(self, reply):
        self.reply, self.requests = reply, []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.reply(request) if callable(self.reply) else self.reply
        if isinstance(r, Exception):
            raise r
        return r

    def install(self):
        brave._transport = httpx.MockTransport(self)
        return self


def ok(data) -> httpx.Response:
    return httpx.Response(200, json=data)


WEB = {"web": {"results": [
    {"title": "Eclipse <strong>guide</strong>", "url": "https://www.example.org/eclipse",
     "description": "When &amp; where to look", "meta_url": {"hostname": "www.example.org"}},
    {"title": "No host given", "url": "http://news.example.net/a", "description": "b"},
    {"title": "Not a web link", "url": "javascript:alert(1)", "description": "c"},
    {"title": "FTP", "url": "ftp://example.com/x", "description": "d"},
]}}
NEWS = {"results": [
    {"title": "Moon story", "url": "https://press.example.com/moon",
     "description": "Fresh", "meta_url": {"hostname": "press.example.com"}},
]}
IMAGES = {"results": [
    {"title": "Picture", "url": "https://photos.example.com/p",
     "properties": {"image": "https://cdn.example.com/p.jpg"}},
    {"title": "Picture, no image", "url": "https://photos.example.com/q", "properties": {}},
]}


def q(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(str(request.url)).query).items()}


def test_request_shape() -> None:
    f = Fake(ok(WEB)).install()
    rows = brave.search("web", "  solar   eclipse  ", count=500, country="ch", language="fr-CH", key=KEY)
    r = f.requests[0]
    p = q(r)
    check("request: web endpoint", str(r.url).startswith("https://api.search.brave.com/res/v1/web/search?"), str(r.url))
    check("request: headers", r.headers.get("accept") == "application/json"
          and r.headers.get("accept-encoding") == "gzip"
          and r.headers.get("x-subscription-token") == KEY, str(dict(r.headers)))
    check("request: key only in the header", KEY not in str(r.url))
    check("request: query trimmed and spaces folded", p.get("q") == "solar eclipse", str(p))
    check("request: count capped for web (20)", p.get("count") == "20", str(p))
    check("request: country upper-case", p.get("country") == "CH", str(p))
    check("request: language from the code", p.get("search_lang") == "fr", str(p))
    check("request: no text decorations", p.get("text_decorations") == "false", str(p))
    check("request: rows came back", isinstance(rows, list) and len(rows) == 2, str(rows))

    brave.search("news", "x", count=999, country="", language="", key=KEY)
    p = q(f.requests[-1])
    check("request: news endpoint + cap 50", "/res/v1/news/search" in str(f.requests[-1].url)
          and p.get("count") == "50", str(p))
    check("request: no country/lang when unknown", "country" not in p and "search_lang" not in p, str(p))

    brave.search("images", "x", count=0, country="usa", language="ja", key=KEY)
    p = q(f.requests[-1])
    check("request: images endpoint, count at least 1, cap respected",
          "/res/v1/images/search" in str(f.requests[-1].url) and p.get("count") == "1", str(p))
    check("request: bad country dropped, Brave's name for Japanese",
          "country" not in p and p.get("search_lang") == "jp", str(p))

    brave.search("web", "y" * 1000, key=KEY, country="", language="")
    check("request: query capped at 400 characters", len(q(f.requests[-1])["q"]) == brave.MAX_QUERY_CHARS)

    n = len(f.requests)
    check("empty query refused", brave.search("web", "   ", key=KEY) is None and len(f.requests) == n)
    check("unknown kind refused", brave.search("video", "x", key=KEY) is None and len(f.requests) == n)
    check("no key: nothing sent", brave.search("web", "x", key="") is None and len(f.requests) == n)


def test_region() -> None:
    import memory.config_manager as cm
    saved_env = {k: os.environ.get(k) for k in ("LC_ALL", "LC_MESSAGES", "LANG")}
    saved_active = cm.get_active_languages
    try:
        os.environ.pop("LC_ALL", None)
        os.environ.pop("LC_MESSAGES", None)
        os.environ["LANG"] = "de_AT.UTF-8"
        cm.get_active_languages = lambda: []
        check("region: from the locale", brave.region() == ("AT", "de"), str(brave.region()))
        cm.get_active_languages = lambda: ["fr", "de"]
        check("region: first active language wins", brave.region() == ("AT", "fr"), str(brave.region()))
        f = Fake(ok(WEB)).install()
        brave.search("web", "x", key=KEY)
        p = q(f.requests[0])
        check("region: used when not given", p.get("country") == "AT" and p.get("search_lang") == "fr", str(p))
    finally:
        cm.get_active_languages = saved_active
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_normalise() -> None:
    Fake(ok(WEB)).install()
    rows = brave.search("web", "x", key=KEY, country="", language="")
    check("normalise: fields", rows[0] == {"title": "Eclipse guide", "url": "https://www.example.org/eclipse",
                                           "snippet": "When & where to look", "source": "example.org"}, str(rows[0]))
    check("normalise: host from the url when meta_url is missing", rows[1]["source"] == "news.example.net", str(rows[1]))
    check("normalise: only http(s) links", all(r["url"].startswith("http") for r in rows) and len(rows) == 2)
    Fake(ok(NEWS)).install()
    rows = brave.search("news", "x", key=KEY, country="", language="")
    check("normalise: news", rows == [{"title": "Moon story", "url": "https://press.example.com/moon",
                                       "snippet": "Fresh", "source": "press.example.com"}], str(rows))
    Fake(ok(IMAGES)).install()
    rows = brave.search("images", "x", key=KEY, country="", language="")
    check("normalise: images use properties.image, rows without one dropped",
          len(rows) == 1 and rows[0]["image"] == "https://cdn.example.com/p.jpg", str(rows))
    Fake(ok({"web": {}})).install()
    check("normalise: nothing found is an empty list", brave.search("web", "x", key=KEY) == [])
    Fake(ok({"web": {"results": "nope"}})).install()
    check("normalise: wrong shape is None", brave.search("web", "x", key=KEY) is None)


def test_errors() -> None:
    for code in (401, 403, 429, 500):
        Fake(httpx.Response(code, json={"error": "x"})).install()
        check(f"error: HTTP {code} -> None", brave.search("web", "x", key=KEY) is None)
    Fake(httpx.Response(401)).install()
    brave.search("web", "x", key=KEY)
    check("error: refused key is explained", "refused the key" in brave.last_error(), brave.last_error())
    Fake(httpx.ReadTimeout("timed out")).install()
    check("error: timeout -> None", brave.search("web", "x", key=KEY) is None)
    Fake(httpx.ConnectError("no route")).install()
    check("error: no connection -> None", brave.search("web", "x", key=KEY) is None)
    Fake(httpx.Response(200, content=b"<html>not json</html>")).install()
    check("error: not JSON -> None", brave.search("web", "x", key=KEY) is None)
    check("timeout is 10 s or less", brave.TIMEOUT_S <= 10)

    calls = []

    def reply(req):
        calls.append(q(req))
        return httpx.Response(422) if len(calls) == 1 else ok(WEB)
    Fake(reply).install()
    rows = brave.search("web", "x", key=KEY, country="ZZ", language="xx")
    check("422 on country/lang: asked again without them",
          rows and len(calls) == 2 and "country" not in calls[1] and "search_lang" not in calls[1], str(calls))

    # The shape Brave really sends for an unknown key (seen against the live API).
    bad_key = {"error": {"code": "SUBSCRIPTION_TOKEN_INVALID", "detail": "invalid",
                         "meta": {"component": "authentication"}, "status": 422},
               "type": "ErrorResponse"}
    f = Fake(httpx.Response(422, json=bad_key)).install()
    rows = brave.search("web", "x", key=KEY, country="US", language="en")
    check("422 for an unknown key: refused, not asked again",
          rows is None and len(f.requests) == 1 and "refused the key" in brave.last_error(),
          f"{len(f.requests)} {brave.last_error()}")

    ok_, msg = (Fake(ok(WEB)).install() and brave.probe(key=KEY))
    check("probe: works", ok_ and "Brave answered" in msg, msg)
    Fake(httpx.Response(401)).install()
    ok_, msg = brave.probe(key=KEY)
    check("probe: refused key", not ok_ and "refused" in msg, msg)


def test_web_search() -> None:
    from actions import web_search as ws
    saved = (ws._ddg_search, ws._ddg_news, ws._gemini_search, brave._key)
    used = []

    def ddg(query, max_results=6):
        used.append("ddg")
        return [{"title": "From DDG", "snippet": "s", "url": "https://ddg.example/x"}]

    def ddg_news(query, max_results=8):
        used.append("ddg_news")
        return [{"title": "DDG news headline here", "snippet": "s" * 80,
                 "url": "https://ddg.example/n", "source": "ddg"}]

    def gemini(query):
        used.append("gemini")
        raise RuntimeError("stand-in: grounding unavailable")

    ws._ddg_search, ws._ddg_news, ws._gemini_search = ddg, ddg_news, gemini
    try:
        brave._key = lambda: KEY
        Fake(ok(WEB)).install()
        used.clear()
        out = ws.web_search({"query": "eclipse"})
        check("web_search: Brave first when a key is set", "Eclipse guide" in out and used == [], f"{used} {out[:200]}")
        check("web_search: result marked untrusted", out != ws.outcome.unwrap(out) or "untrusted" in out.lower(), out[:120])

        Fake(ok(NEWS)).install()
        used.clear()
        out = ws.web_search({"query": "moon", "mode": "news"})
        check("web_search news: Brave first", "Moon story" in out and used == [], f"{used} {out[:200]}")

        Fake(httpx.Response(500)).install()
        used.clear()
        out = ws.web_search({"query": "eclipse"})
        check("web_search: Brave fails -> Gemini, then DDG", used == ["gemini", "ddg"] and "From DDG" in out, f"{used}")
        used.clear()
        out = ws.web_search({"query": "moon", "mode": "news"})
        check("web_search news: Brave fails -> DDG news", "ddg_news" in used and "DDG news headline" in out, f"{used}")

        Fake(ok({"web": {"results": []}})).install()
        used.clear()
        ws.web_search({"query": "nothing"})
        check("web_search: Brave found nothing -> next source", used == ["gemini", "ddg"], f"{used}")

        Fake(ok(WEB)).install()
        used.clear()
        out = ws.web_search({"query": "phone", "mode": "price"})
        check("web_search price: Brave replaces the DDG fallback", used == ["gemini"] and "Eclipse guide" in out, f"{used}")

        brave._key = lambda: ""
        f = Fake(ok(WEB)).install()
        used.clear()
        ws.web_search({"query": "eclipse"})
        check("web_search: no key -> unchanged (Gemini, DDG; Brave not asked)",
              used == ["gemini", "ddg"] and not f.requests, f"{used} {len(f.requests)}")
    finally:
        ws._ddg_search, ws._ddg_news, ws._gemini_search, brave._key = saved


def test_config_and_check() -> None:
    import memory.config_manager as cm
    check("config: getter and setter exist", callable(cm.get_brave_key) and callable(cm.save_brave_key))
    from core import selftest
    saved = brave._key
    try:
        brave._key = lambda: ""
        st, detail, fix = selftest.check_brave(True)
        check("selftest: OFF without a key", st == selftest.OFF, detail)
        brave._key = lambda: KEY
        Fake(ok(WEB)).install()
        st, detail, _ = selftest.check_brave(True)
        check("selftest: PASS when Brave answers", st == selftest.PASS, detail)
        Fake(httpx.Response(401)).install()
        st, detail, _ = selftest.check_brave(True)
        check("selftest: FAIL when the key is refused", st == selftest.FAIL, detail)
        st, _, _ = selftest.check_brave(False)
        check("selftest: offline does not ask", st == selftest.PASS)
        check("selftest: listed", "brave" in [n for n, _f, _ in selftest.CHECKS])
    finally:
        brave._key = saved
    from core import setup_guides
    name, g = setup_guides.find("how do I set up brave search")
    check("setup guide: found by voice", name == "brave_search" and g is not None, str(name))


def live() -> None:
    brave._transport = None
    if not brave.available():
        sys.__stdout__.write("SKIP  live: no Brave Search key is set\n")
        return
    rows = brave.search("web", "Brave Search API", count=3)
    check("live: Brave answered", bool(rows), brave.last_error())
    for r in rows or []:
        sys.__stdout__.write(f"INFO  {r['source']}: {r['title'][:70]}\n")


def main_() -> int:
    try:
        with contextlib.redirect_stdout(_log):
            test_request_shape()
            test_region()
            test_normalise()
            test_errors()
            test_web_search()
            test_config_and_check()
    finally:
        brave._transport = None
    log = _log.getvalue()
    check("the key never appears in a log line", KEY not in log and "0123456789abcdef" not in log)
    check("something was logged (the check above saw real output)", "[Brave]" in log)
    if "--live" in sys.argv:
        live()
    print(f"\n{len(_failures)} failed" if _failures else "\nALL PASSED")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main_())
