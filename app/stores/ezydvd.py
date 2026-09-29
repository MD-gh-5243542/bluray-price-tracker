from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from .. import fetch
from ..matching import is_4k, parse_price, search_query
from .base import Offer, Query

BASE = "https://www.ezydvd.com.au"
STORE = "ezydvd"


def _parse_list(html: str) -> list[Offer]:
    s = BeautifulSoup(html, "lxml")
    out = []
    for item in s.select(".product-item"):
        a = item.select_one("a.title")
        if not a:
            continue
        price_el = item.select_one(".price")
        img = item.select_one(".list_image img")
        text = item.get_text(" ", strip=True).lower()
        in_stock = "buy now" in text or "pre-order" in text or "preorder" in text
        if "out of stock" in text or "sold out" in text:
            in_stock = False
        href = a["href"]
        name = a.get_text(strip=True)
        if "/dvd/" in href.lower() and "dvd" not in name.lower():
            name += " [DVD]"
        out.append(Offer(STORE, urljoin(BASE, href), name,
                         parse_price(price_el.get_text()) if price_el else None,
                         in_stock=in_stock, image=img.get("src") if img else None))
    return out


def _search(text: str) -> list[Offer]:
    try:
        return _parse_list(fetch.get(f"{BASE}/search", params={"q": text}))
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:  # EzyDVD answers "no results" with a 404
            return []
        raise


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    if q.is_4k and "4k" not in text.lower():
        text += " 4k"
    results = _search(text)
    if not results and not q.search_terms:
        # Retry with just the core name (EzyDVD search is strict)
        results = _search(search_query(q.name))
    for o in results:
        o.score = q.score(o.title)
    return results


def bargains() -> list[Offer]:
    return sorted(
        [offer for offer in _search("4k") if is_4k(offer.title) and offer.price is not None],
        key=lambda offer: offer.price or 0,
    )[:10]


def refresh(url: str) -> Offer | None:
    html = fetch.get(url)
    s = BeautifulSoup(html, "lxml")
    h1 = s.select_one("h1")
    og = s.find("meta", property="og:title")
    title = h1.get_text(strip=True) if h1 else (og["content"] if og else "")
    price = None
    for sel in (".product-info .price", ".product_details .price", ".price-box .price", ".price"):
        e = s.select_one(sel)
        if e and parse_price(e.get_text()):
            price = parse_price(e.get_text())
            break
    text = s.get_text(" ", strip=True).lower()
    in_stock = price is not None and not any(
        x in text for x in ("out of stock", "sold out", "currently unavailable"))
    img = s.find("meta", property="og:image")
    return Offer(STORE, url, title, price, in_stock=in_stock,
                 image=img["content"] if img else None)
