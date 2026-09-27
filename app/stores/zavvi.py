import json
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import fetch
from ..matching import parse_price, score, search_query
from .base import Offer, Query

BASE = "https://www.zavvi.com.au"
STORE = "zavvi"
_MEDIA = re.compile(r"^/p/(4k|blu-ray)/")


def _parse_list(html: str) -> list[Offer]:
    s = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for card in s.select("div.product-card"):
        a = card.find("a", href=_MEDIA)
        if not a:
            continue
        href = a["href"].split("?")[0]
        if href in seen:
            continue
        seen.add(href)
        img = card.find("img")
        name = ""
        for link in card.find_all("a", href=True):
            if link["href"].split("?")[0] == href and link.get_text(strip=True):
                name = link.get_text(" ", strip=True)
                break
        if not name and img:
            name = img.get("alt", "")
        text = card.get_text(" ", strip=True)
        m = re.search(r"A\$\s*([\d,]+(?:\.\d\d)?)", text)
        price = float(m.group(1).replace(",", "")) if m else None
        if href.startswith("/p/4k/") and "4k" not in name.lower():
            name += " 4K"
        low = text.lower()
        in_stock = "sold out" not in low and "out of stock" not in low
        out.append(Offer(STORE, urljoin(BASE, href), name, price, in_stock=in_stock,
                         image=urljoin(BASE, img["src"]) if img and img.get("src") else None))
    return out


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    results = _parse_list(fetch.get(f"{BASE}/search/", params={"q": text}))
    for o in results:
        o.score = score(q.name, q.is_4k, q.edition, o.title)
    return results


def refresh(url: str) -> Offer | None:
    html = fetch.get(url)
    s = BeautifulSoup(html, "lxml")
    pid = re.search(r"/(\d+)/?$", url.split("?")[0])
    pid = pid.group(1) if pid else None
    best = None
    name = None
    for sc in s.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(sc.string or "")
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for d in items:
            if d.get("@type") == "ProductGroup":
                name = d.get("name")
                variants = d.get("hasVariant", [])
            elif d.get("@type") == "Product":
                name = name or d.get("name")
                variants = [d]
            else:
                continue
            for v in variants:
                off = v.get("offers") or {}
                if isinstance(off, list):
                    off = off[0] if off else {}
                cand = {
                    "sku": str(v.get("sku") or off.get("sku") or ""),
                    "name": v.get("name") or name,
                    "price": off.get("price"),
                    "stock": "InStock" in str(off.get("availability", "")) or "PreOrder" in str(off.get("availability", "")),
                    "image": v.get("image"),
                }
                if pid and cand["sku"] == pid:
                    best = cand
                    break
                if best is None or (cand["stock"] and not best["stock"]):
                    best = cand
    if not best:
        og = s.find("meta", property="og:title")
        return Offer(STORE, url, og["content"] if og else "", None, in_stock=False) if og else None
    title = best["name"] or ""
    if "/p/4k/" in url and "4k" not in title.lower():
        title += " 4K"
    img = best["image"]
    if isinstance(img, list):
        img = img[0] if img else None
    return Offer(STORE, url, title, float(best["price"]) if best["price"] else None,
                 in_stock=best["stock"], image=img)
