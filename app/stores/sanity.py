"""Sanity AU store integration using its public Unbxd search API."""
import json
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import fetch
from ..matching import is_4k, parse_price, score, search_query
from .base import Offer, Query

BASE = "https://www.sanity.com.au"
STORE = "sanity"
API_KEY = "5c3a4f2cbd061731cf6fa614437bc8eb"
SITE_NAME = "sanity-com-au807721559915031"
SEARCH_URL = f"https://search.unbxd.io/{API_KEY}/{SITE_NAME}/search"


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    data = fetch.get_json(
        SEARCH_URL,
        params={
            "q": text,
            "rows": "32",
            "format": "json",
            "version": "V2",
            "facet.multiselect": "true",
            "device-type": "Desktop",
            "api-key": API_KEY,
        },
    )
    out = []
    for product in data.get("response", {}).get("products", []):
        title = str(product.get("title") or "")
        fmt = str(product.get("format") or "")
        if fmt:
            title += f" [{fmt}]"
        if q.is_4k and not is_4k(fmt) and not is_4k(title):
            continue
        availability = str(product.get("availability") or "").lower()
        out.append(Offer(
            STORE,
            str(product.get("productUrl") or ""),
            title,
            parse_price(str(product.get("price") or "")),
            in_stock=availability not in ("", "not in stock", "out of stock"),
            image=str(product.get("image_link") or product.get("imageUrl") or "") or None,
        ))
    for offer in out:
        offer.score = score(q.name, q.is_4k, q.edition, offer.title)
    return [offer for offer in out if offer.url]


def refresh(url: str) -> Offer | None:
    html = fetch.get(url)
    soup = BeautifulSoup(html, "lxml")
    product = None
    for node in soup.select('script[type="application/ld+json"]'):
        if not node.string:
            continue
        try:
            data = json.loads(node.string)
        except json.JSONDecodeError:
            continue
        candidates = data if isinstance(data, list) else [data]
        product = next(
            (candidate for candidate in candidates
             if isinstance(candidate, dict) and "offers" in candidate),
            None,
        )
        if product:
            break
    if not product:
        return None
    title = str(product.get("name") or "")
    offer = product.get("offers") or {}
    availability = str(offer.get("availability") or "").lower()
    image = product.get("image")
    if isinstance(image, list):
        image = image[0] if image else None
    return Offer(
        STORE, url, title, parse_price(str(offer.get("price") or "")),
        in_stock=not any(value in availability for value in ("outofstock", "soldout")),
        image=str(image) if image else None,
    )


def bargains(limit: int = 10) -> list[Offer]:
    data = fetch.get_json(
        SEARCH_URL,
        params={
            "q": "4K",
            "rows": "50",
            "sort": "price asc",
            "format": "json",
            "version": "V2",
            "api-key": API_KEY,
        },
    )
    offers = []
    for product in data.get("response", {}).get("products", []):
        fmt = str(product.get("format") or "")
        title = str(product.get("title") or "")
        if not is_4k(fmt) and not is_4k(title):
            continue
        availability = str(product.get("availability") or "").lower()
        offers.append(Offer(
            STORE, str(product.get("productUrl") or ""), f"{title} [{fmt}]" if fmt else title,
            parse_price(str(product.get("price") or "")),
            in_stock=availability not in ("", "not in stock", "out of stock"),
            image=str(product.get("image_link") or product.get("imageUrl") or "") or None,
        ))
    return [offer for offer in offers
            if offer.url and offer.price is not None and offer.in_stock][:limit]
