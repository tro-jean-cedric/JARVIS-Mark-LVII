"""
plugins/_shop_extract.py — reading an offer out of a shop's product page.

Helper for plugins/shop_assistant.py (leading underscore: the loader skips it).
Pure functions over HTML, so they can be tested without a network.

Order of trust:
  1. schema.org Product / Offer in JSON-LD — what most shops publish for
     search engines: name, price, currency, availability, condition, seller.
  2. Microdata and Open Graph product meta tags (itemprop=price,
     product:price:amount, og:price:amount).
  3. Nothing in markup: the page text is kept so the model can read it.

The markup price is frequently NOT what a customer pays (it may exclude VAT,
or be a 'from' price), so the text around the price is kept too and the model
checks the real one. Page text is data, never an instruction.
"""
from __future__ import annotations

import json
import re
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

_TRACKING = re.compile(r"(?i)^(utm_|gclid|fbclid|yclid|msclkid|_ga|mc_|srsltid|tag$|linkcode$|"
                       r"ascsubtag$|aff_|affiliate|partner|clickid|ref$|ref_)")


def clean_url(url: str) -> str:
    """The address without tracking and affiliate parameters."""
    try:
        u = urlparse(url)
        q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _TRACKING.match(k)]
        return urlunparse(u._replace(query=urlencode(q), fragment=""))
    except Exception:
        return url


def domain(url: str) -> str:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
        return host[4:] if host.startswith("www.") else host
    except Exception:
        return ""


def parse_price(value) -> Optional[float]:
    """'1.299,00 €' / '1,299.00' / 1299 / '1 299,-' -> 1299.0"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value) if value > 0 else None
    s = re.sub(r"[^\d.,]", "", str(value))
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = (head.replace(",", "") + "." + tail) if len(tail) in (1, 2) else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.rpartition(".")[2]) == 3 and len(s) > 4):
        s = s.replace(".", "")
    try:
        v = float(s)
    except ValueError:
        return None
    return v if v > 0 else None


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _types(d: dict) -> set:
    t = d.get("@type")
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return {str(t).lower()} if t else set()


def _short(v) -> str:
    if isinstance(v, dict):
        v = v.get("name") or v.get("@id") or ""
    if isinstance(v, list):
        v = v[0] if v else ""
    s = str(v or "")
    return s.rsplit("/", 1)[-1] if s.startswith("http") else s


def jsonld_offers(html: str) -> list[dict]:
    """Every Product with a priced Offer found in JSON-LD blocks."""
    out = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
                         html, re.S | re.I):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r"[\x00-\x1f]", " ", raw))
            except Exception:
                continue
        for d in _walk(data):
            if "product" not in _types(d) and "productgroup" not in _types(d):
                continue
            name = str(d.get("name") or "").strip()
            offers = d.get("offers")
            if offers is None and isinstance(d.get("hasVariant"), list):
                offers = [v.get("offers") for v in d["hasVariant"] if isinstance(v, dict)]
            for o in _walk(offers or []):
                if not ({"offer", "aggregateoffer"} & _types(o)) and "price" not in o and "lowPrice" not in o:
                    continue
                price = o.get("price")
                if price is None and isinstance(o.get("priceSpecification"), (dict, list)):
                    spec = o["priceSpecification"]
                    spec = spec[0] if isinstance(spec, list) and spec else spec
                    if isinstance(spec, dict):
                        price = spec.get("price")
                aggregate = price is None and o.get("lowPrice") is not None
                if aggregate:
                    price = o.get("lowPrice")
                p = parse_price(price)
                if p is None:
                    continue
                ship = None
                sd = o.get("shippingDetails")
                for s in _walk(sd or []):
                    rate = s.get("shippingRate")
                    if isinstance(rate, dict) and rate.get("value") is not None:
                        ship = parse_price(rate.get("value")) if str(rate.get("value")) not in ("0", "0.0", "0.00") else 0.0
                        break
                out.append({
                    "name": name, "price": p,
                    "currency": str(o.get("priceCurrency") or "").upper(),
                    "availability": _short(o.get("availability")).lower(),
                    "condition": _short(o.get("itemCondition") or d.get("itemCondition")).lower(),
                    "seller": _short(o.get("seller")),
                    "aggregate": aggregate, "offer_count": o.get("offerCount"),
                    "shipping": ship, "sku": str(d.get("sku") or d.get("gtin13") or d.get("mpn") or ""),
                })
                break      # one offer per product is enough
    return out


_META = re.compile(r'<meta[^>]+(?:property|name|itemprop)=["\']([^"\']+)["\'][^>]*content=["\']([^"\']*)["\']',
                   re.I)
_META_REV = re.compile(r'<meta[^>]+content=["\']([^"\']*)["\'][^>]*(?:property|name|itemprop)=["\']([^"\']+)["\']',
                       re.I)


def meta_offer(html: str) -> Optional[dict]:
    meta: dict[str, str] = {}
    for k, v in _META.findall(html):
        meta.setdefault(k.lower(), v)
    for v, k in _META_REV.findall(html):
        meta.setdefault(k.lower(), v)
    price = (meta.get("product:price:amount") or meta.get("og:price:amount") or meta.get("price"))
    if price is None:
        m = re.search(r'itemprop=["\']price["\'][^>]*content=["\']([^"\']+)["\']', html, re.I) or \
            re.search(r'content=["\']([^"\']+)["\'][^>]*itemprop=["\']price["\']', html, re.I)
        price = m.group(1) if m else None
    p = parse_price(price)
    if p is None:
        return None
    cur = (meta.get("product:price:currency") or meta.get("og:price:currency") or
           meta.get("pricecurrency") or "")
    if not cur:
        m = re.search(r'itemprop=["\']priceCurrency["\'][^>]*content=["\']([^"\']+)', html, re.I)
        cur = m.group(1) if m else ""
    avail = meta.get("product:availability") or meta.get("og:availability") or ""
    return {"name": meta.get("og:title", ""), "price": p, "currency": cur.upper(),
            "availability": avail.lower(), "condition": meta.get("product:condition", "").lower(),
            "seller": meta.get("og:site_name", ""), "aggregate": False, "shipping": None, "sku": ""}


def page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""


def visible_text(html: str, limit: int = 60000) -> str:
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for t in soup(["script", "style", "noscript", "svg", "template", "iframe"]):
            t.decompose()
        text = soup.get_text(" ", strip=True)
    except Exception:
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text)[:limit]


def _price_strings(p: float) -> list[str]:
    whole = int(p)
    cents = round((p - whole) * 100)
    out = {f"{p:.2f}", f"{p:,.2f}", f"{p:.2f}".replace(".", ","),
           f"{p:,.2f}".replace(",", "X").replace(".", ",").replace("X", "."), str(whole)}
    if cents == 0:
        out.add(f"{whole:,}".replace(",", "."))
        out.add(f"{whole:,}")
    return [s for s in out if len(s) >= 2]


def text_near_price(text: str, price: Optional[float], width: int = 260) -> str:
    """A window of page text around where the price is written, plus any
    sentence about stock or delivery — what the model needs to check the real
    price, tax and availability without reading the whole page."""
    snippets = []
    if price:
        for s in _price_strings(price):
            i = text.find(s)
            if i >= 0:
                snippets.append(text[max(0, i - width): i + width])
                break
    for m in re.finditer(r"(?i)(ship|deliver|versand|liefer|livraison|envío|envio|spedizion|kargo|"
                         r"verzend|wysył|in stock|out of stock|sold out|lager|vorrätig|ausverkauft|"
                         r"rupture|disponib|agotado|esaurit|stokta|tükendi|vat|mwst|tax|tva|iva|kdv|btw)",
                         text):
        snippets.append(text[max(0, m.start() - 90): m.start() + 140])
        if len(snippets) >= 6:
            break
    joined = " … ".join(snippets)
    return joined[:1400]


def extract(html: str, url: str) -> dict:
    """{'offers': [...], 'title', 'text_near', 'text'} for one page."""
    offers = jsonld_offers(html)
    if not offers:
        m = meta_offer(html)
        if m:
            offers = [m]
    title = page_title(html)
    text = visible_text(html)
    best = offers[0] if offers else None
    return {"url": url, "domain": domain(url), "title": title, "offers": offers,
            "text_near": text_near_price(text, best["price"] if best else None),
            "text": text[:3500] if not offers else ""}


NOT_NEW = re.compile(r"(?i)\b(used|refurbished|renewed|open[- ]box|pre-?owned|second[- ]hand|"
                     r"b-?ware|gebraucht|generalüberholt|reconditionn[ée]|occasion|usato|"
                     r"ricondizionat|reacondicionad|segunda mano|ikinci el|yenilenmi[şs]|outlet)\b")


def looks_used(*texts: str) -> bool:
    return any(NOT_NEW.search(t or "") for t in texts)


def availability_flag(av: str) -> Optional[bool]:
    a = (av or "").lower()
    if not a:
        return None
    if any(k in a for k in ("outofstock", "soldout", "discontinued", "out of stock")):
        return False
    if any(k in a for k in ("instock", "limitedavailability", "onlineonly", "in stock", "preorder",
                            "backorder")):
        return True
    return None
