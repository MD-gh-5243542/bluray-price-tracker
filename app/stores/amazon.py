import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import fetch
from ..matching import is_4k, parse_price, score, score_barcode, search_query
from .base import Offer, Query

BASE = "https://www.amazon.com.au"
STORE = "amazon"


def _asin(url: str) -> str | None:
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url or "")
    return m.group(1) if m else None


def product_url(asin: str) -> str:
    return f"{BASE}/dp/{asin}"


def _primary_price(soup: BeautifulSoup) -> float | None:
    for sel in ("#corePrice_feature_div .a-offscreen",
                "#corePriceDisplay_desktop_feature_div .a-offscreen",
                "#apex_desktop .a-offscreen", "#price_inside_buybox", "#newBuyBoxPrice"):
        e = soup.select_one(sel)
        if e and (price := parse_price(e.get_text())) is not None:
            return price
    return None


def refresh(url: str) -> Offer | None:
    asin = _asin(url)
    if not asin:
        return None
    html = fetch.smart_get(product_url(asin), wait_selector="#productTitle")
    s = BeautifulSoup(html, "lxml")
    t = s.select_one("#productTitle")
    if not t:
        return None
    title = t.get_text(strip=True)
    price = _primary_price(s)
    offer_url = product_url(asin)
    formats = s.select_one("#formats")
    if formats:
        variant = next((li for li in formats.select("#tmmSwatches li.swatchElement")
                        if is_4k(li.get_text(" ", strip=True))), None)
        if not variant:
            return None
        if not is_4k(title):
            title += " [4K UHD]"
        price_node = variant.select_one('[aria-label*="$"], .a-color-secondary')
        price_text = price_node.get_text(" ", strip=True) if price_node else ""
        variant_price = parse_price(price_text) if "$" in price_text else None
        link = variant.select_one("a[href]")
        variant_asin = _asin(urljoin(BASE, link["href"])) if link else None
        if variant_asin:
            offer_url = product_url(variant_asin)
        # A swatch price is tied to its format; a page-level price is only safe
        # when the 4K swatch itself is selected.
        selected = "selected" in (variant.get("class") or [])
        price = variant_price if variant_price is not None else (_primary_price(s) if selected else None)
    avail = (s.select_one("#availability").get_text(" ", strip=True).lower()
             if s.select_one("#availability") else "")
    in_stock = price is not None and (formats is not None or
                                      ("unavailable" not in avail and "out of stock" not in avail))
    img = s.select_one("#landingImage")
    return Offer(STORE, offer_url, title, price,
                 in_stock=in_stock, image=img.get("src") if img else None)


def _search(q: str) -> list[Offer]:
    html = fetch.smart_get(f"{BASE}/s", params={"k": q, "i": "movies-tv"},
                           wait_selector='div[data-component-type="s-search-result"]')
    s = BeautifulSoup(html, "lxml")
    out = []
    for d in s.select('div[data-component-type="s-search-result"]'):
        asin = d.get("data-asin")
        if not asin or d.select_one(".puis-sponsored-label-text"):
            continue
        h2 = d.select_one("h2")
        p = d.select_one(".a-price .a-offscreen")
        img = d.select_one("img.s-image")
        name = h2.get_text(" ", strip=True) if h2 else ""
        fmt = d.select_one("a.a-text-bold, span.a-text-bold")
        fmt = fmt.get_text(" ", strip=True) if fmt else ""
        if fmt in ("DVD", "Blu-ray") and fmt.lower() not in name.lower():
            name += f" [{fmt}]"
        out.append(Offer(STORE, product_url(asin), name,
                         parse_price(p.get_text()) if p else None,
                         in_stock=p is not None, image=img.get("src") if img else None))
    return out


def search(q: Query) -> list[Offer]:
    results: list[Offer] = []
    if q.upc:
        for o in _search(q.upc)[:3]:
            o.by_barcode = True
            results.append(o)
    if not results:
        text = q.search_terms or search_query(q.name) + (" 4K" if q.is_4k else " blu-ray")
        results = _search(text)[:12]
    for o in results:
        o.score = (score_barcode(q.name, q.is_4k, q.edition, o.title, loose=True)
                   if o.by_barcode else score(q.name, q.is_4k, q.edition, o.title))
        if o.by_barcode:
            o.score = max(o.score, 90.0) if o.score >= 50 else o.score
    return results


def bargains() -> list[Offer]:
    return [offer for offer in sorted(_search("4K"), key=lambda item: item.price or 1e9)
            if is_4k(offer.title) and offer.price is not None][:10]
