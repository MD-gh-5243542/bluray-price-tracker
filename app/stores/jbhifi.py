"""JB Hi-Fi AU product search and product-page parsing."""
import json
import re
from urllib.parse import quote, urljoin

from bs4 import BeautifulSoup

from .. import fetch
from ..matching import is_4k, parse_price, search_query
from .base import Offer, Query

BASE = "https://www.jbhifi.com.au"
STORE = "jbhifi"


def _json_ld(soup: BeautifulSoup) -> list[dict]:
    values = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or script.get_text())
        except (TypeError, ValueError):
            continue
        values.extend(data if isinstance(data, list) else [data])
    return [value for value in values if isinstance(value, dict)]


def _format_name(name: str, fmt: str) -> str:
    if fmt and fmt.lower() not in name.lower():
        return f"{name} [{fmt}]"
    return name


def _parse_search(html: str) -> list[Offer]:
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for card in soup.select('[data-testid="product-card-content"]'):
        link = card.select_one('a[data-testid="product-card-content-link"][href*="/products/"]')
        title = card.select_one('[data-testid="product-card-title"]')
        if not link or not title:
            continue
        url = urljoin(BASE, link["href"].split("?")[0])
        if url in seen:
            continue
        seen.add(url)
        fmt_node = title.find_next_sibling()
        fmt = fmt_node.get_text(" ", strip=True) if fmt_node else ""
        text = card.get_text(" ", strip=True)
        price_match = re.search(r"\$\s*([\d,]+(?:\.\d{1,2})?)", text)
        image = card.select_one("img")
        low = text.lower()
        out.append(Offer(
            STORE, url, _format_name(title.get_text(" ", strip=True), fmt),
            parse_price(price_match.group(0)) if price_match else None,
            in_stock=not any(x in low for x in ("sold out", "out of stock", "unavailable")),
            image=image.get("src") if image else None,
        ))
    return out


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    url = f"{BASE}/search?page=1&query={quote(text)}"
    html = fetch.browser_get(url, wait_selector='[data-testid="product-card-title"]', scroll=True)
    results = _parse_search(html)
    for offer in results:
        offer.score = q.score(offer.title)
    return results


def bargains() -> list[Offer]:
    html = fetch.browser_get(
        f"{BASE}/search?page=1&query=4K",
        wait_selector='[data-testid="product-card-title"]',
        scroll=True,
    )
    return sorted(
        [offer for offer in _parse_search(html) if is_4k(offer.title) and offer.price is not None],
        key=lambda offer: offer.price or 0,
    )[:10]


def refresh(url: str) -> Offer | None:
    html = fetch.smart_get(url, wait_selector='script[type="application/ld+json"]')
    soup = BeautifulSoup(html, "lxml")
    product = next((item for item in _json_ld(soup) if item.get("@type") == "Product"), None)
    if not product:
        return None
    offers = product.get("offers") or {}
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    properties = {
        str(item.get("name", "")).lower(): str(item.get("value", ""))
        for item in product.get("additionalProperty", [])
        if isinstance(item, dict)
    }
    fmt = properties.get("primary format - movies/tv") or properties.get("in-store product group", "")
    availability = str(offers.get("availability", ""))
    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    return Offer(
        STORE,
        url,
        _format_name(str(product.get("name") or ""), fmt),
        parse_price(str(offers["price"])) if offers.get("price") is not None else None,
        in_stock="InStock" in availability or "PreOrder" in availability,
        image=image,
        by_barcode=bool(product.get("gtin")),
    )
