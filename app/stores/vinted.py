"""Vinted AU marketplace integration."""
import json
import re
from urllib.parse import quote_plus, urljoin

from bs4 import BeautifulSoup

from .. import fetch
from ..matching import is_4k, parse_price, search_query
from .base import Offer, Query

BASE = "https://www.vinted.com.au"
STORE = "vinted"


def _search_url(text: str) -> str:
    return f"{BASE}/catalog?search_text={quote_plus(text)}"


def _title_and_price(anchor) -> tuple[str, float | None]:
    title_attr = (anchor.get("title") or "").strip()
    if title_attr:
        title = title_attr.split(", Condition:", 1)[0].strip()
        price_match = re.search(r"([0-9]+(?:[.,][0-9]{2})?)\s*A\$", title_attr)
        if price_match:
            return title, parse_price(price_match.group(1))
    title = ""
    if img := anchor.find("img"):
        title = (img.get("alt") or "").strip()
    if not title:
        title = anchor.get_text(" ", strip=True)
    text = anchor.get_text(" ", strip=True)
    price_match = re.search(r"\$([0-9]+(?:[.,][0-9]{2})?)", text)
    return title, parse_price(price_match.group(1)) if price_match else None


def _is_in_stock(text: str) -> bool:
    lowered = text.lower()
    return "sold" not in lowered and "reserved" not in lowered and "unavailable" not in lowered


def _parse_list(html: str, q: Query) -> list[Offer]:
    soup = BeautifulSoup(html, "lxml")
    out: list[Offer] = []
    seen: set[str] = set()
    for anchor in soup.select('a[href*="/items/"]'):
        href = anchor.get("href") or ""
        if "/items/" not in href:
            continue
        url = urljoin(BASE, href.split("?", 1)[0])
        if url in seen:
            continue
        seen.add(url)
        title, price = _title_and_price(anchor)
        if not title:
            continue
        if q.is_4k and not is_4k(title):
            continue
        text = anchor.get_text(" ", strip=True)
        image = None
        if img := anchor.find("img"):
            image = img.get("src") or img.get("data-src")
        offer = Offer(STORE, url, title, price, in_stock=_is_in_stock(text), image=image)
        offer.score = q.score(offer.title)
        out.append(offer)
    return out


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    if q.is_4k and not is_4k(text):
        text += " 4K"
    html = fetch.browser_get(_search_url(text), wait_selector='a[href*="/items/"]', scroll=True)
    return _parse_list(html, q)


def refresh(url: str) -> Offer | None:
    html = fetch.browser_get(url, wait_selector="h1,[data-testid='item-price']")
    soup = BeautifulSoup(html, "lxml")
    title = None
    if og := soup.find("meta", attrs={"property": "og:title"}):
        title = (og.get("content") or "").strip()
    if not title and (h1 := soup.select_one("h1")):
        title = h1.get_text(" ", strip=True)
    if not title:
        return None
    price = None
    if node := soup.select_one("[data-testid='item-price']"):
        price = parse_price(node.get_text(" ", strip=True))
    if price is None:
        ld = soup.find("script", attrs={"type": "application/ld+json"})
        if ld and ld.string:
            try:
                data = json.loads(ld.string)
            except json.JSONDecodeError:
                data = None
            if isinstance(data, dict):
                offers = data.get("offers") or {}
                if isinstance(offers, dict):
                    price = parse_price(str(offers.get("price") or ""))
    image = None
    if og_img := soup.find("meta", attrs={"property": "og:image"}):
        image = (og_img.get("content") or "").strip() or None
    page_text = soup.get_text(" ", strip=True).lower()
    in_stock = "item has been sold" not in page_text and "item has been removed" not in page_text
    return Offer(STORE, url, title, price, in_stock=in_stock, image=image, delivery_unknown=True)


def bargains(limit: int = 10) -> list[Offer]:
    class _BargainTitle:
        name = "4K Blu-ray"
        is_4k = True
        edition = ""
        upc = None
        amazon_asin = None
        search_terms = None
        year = None
        match_mode = "any"
        alt_upcs = None

    q = Query(_BargainTitle())
    offers = [offer for offer in search(q) if offer.in_stock and offer.price is not None]
    offers.sort(key=lambda offer: offer.price if offer.price is not None else 1e9)
    return offers[:limit]
