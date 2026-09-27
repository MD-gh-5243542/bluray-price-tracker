"""eBay AU. Uses the Browse API if credentials are configured, otherwise the
headless browser (plain HTTP requests get a 403)."""
import base64
import logging
import re
import time
from urllib.parse import urlencode

from bs4 import BeautifulSoup

from .. import config, fetch
from ..matching import parse_price, score, score_barcode, search_query
from .base import Offer, Query

log = logging.getLogger(__name__)
STORE = "ebay"
_token: dict = {"value": None, "exp": 0}


def _shipping_from_rows(rows: list[str]) -> float | None:
    for r in rows:
        low = r.lower()
        if "postage" in low or "delivery" in low or "shipping" in low:
            if "free" in low:
                return 0.0
            m = re.search(r"\+\s*(?:AU\s*)?\$\s*([\d,.]+)", r)
            if m:
                return float(m.group(1).replace(",", ""))
    return None


def _scrape(query: str) -> list[Offer]:
    params = {"_nkw": query, "LH_BIN": "1", "_sop": "15", "_ipg": "25", "LH_PrefLoc": "1"}
    if not config.EBAY_INCLUDE_USED:
        params["LH_ItemCondition"] = "1000"
    url = "https://www.ebay.com.au/sch/i.html?" + urlencode(params)
    html = fetch.browser_get(url, wait_selector="li.s-card, li.s-item",
                             block_resources={"image", "media", "font"})
    s = BeautifulSoup(html, "lxml")
    out = []
    for card in s.select("li.s-card, li.s-item"):
        a = card.select_one("a.su-link[href*='/itm/'], a.s-card__link[href*='/itm/'], a.s-item__link")
        if not a:
            continue
        href = a["href"].split("?")[0]
        if href.endswith("/itm/123456"):
            continue  # "Shop on eBay" placeholder
        t = card.select_one(".s-card__title, .s-item__title")
        title = t.get_text(" ", strip=True) if t else ""
        title = re.sub(r"Opens in a new window or tab|^New listing", "", title, flags=re.I).strip()
        p = card.select_one(".s-card__price, .s-item__price")
        rows = [r.get_text(" ", strip=True) for r in card.select(
            ".s-card__attribute-row, .s-item__shipping, .s-item__logisticsCost")]
        img = card.find("img")
        out.append(Offer(STORE, href, title, parse_price(p.get_text()) if p else None,
                         shipping=_shipping_from_rows(rows), image=img.get("src") if img else None,
                         condition="Used" if config.EBAY_INCLUDE_USED else "New"))
    return out


def _api_token() -> str:
    if _token["value"] and _token["exp"] > time.time() + 60:
        return _token["value"]
    auth = base64.b64encode(f"{config.EBAY_CLIENT_ID}:{config.EBAY_CLIENT_SECRET}".encode()).decode()
    r = fetch.post(
        "https://api.ebay.com/identity/v1/oauth2/token",
        headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": "https://api.ebay.com/oauth/api_scope"},
    )
    r.raise_for_status()
    j = r.json()
    _token.update(value=j["access_token"], exp=time.time() + int(j.get("expires_in", 7200)))
    return _token["value"]


def _api(query: str | None, gtin: str | None) -> list[Offer]:
    filters = ["buyingOptions:{FIXED_PRICE}", "deliveryCountry:AU", "priceCurrency:AUD"]
    if not config.EBAY_INCLUDE_USED:
        filters.append("conditions:{NEW}")
    params = {"limit": "50", "sort": "price", "filter": ",".join(filters)}
    if gtin:
        params["gtin"] = gtin
    if query:
        params["q"] = query
    j = fetch.get_json(
        "https://api.ebay.com/buy/browse/v1/item_summary/search",
        params=params,
        headers={"Authorization": f"Bearer {_api_token()}", "X-EBAY-C-MARKETPLACE-ID": "EBAY_AU",
                 "X-EBAY-C-ENDUSERCTX": "contextualLocation=country=AU"},
    )
    out = []
    for it in j.get("itemSummaries", []):
        ship = None
        so = it.get("shippingOptions") or []
        if so and so[0].get("shippingCost"):
            ship = float(so[0]["shippingCost"].get("value", 0))
        out.append(Offer(STORE, it.get("itemWebUrl", "").split("?")[0], it.get("title", ""),
                         float(it["price"]["value"]) if it.get("price") else None, shipping=ship,
                         image=(it.get("image") or {}).get("imageUrl"), condition=it.get("condition")))
    return out


def search(q: Query) -> list[Offer]:
    use_api = bool(config.EBAY_CLIENT_ID and config.EBAY_CLIENT_SECRET)
    if q.upc:
        results = _api(None, q.upc) if use_api else _scrape(q.upc)
        for o in results:
            o.by_barcode = True
    else:
        text = q.search_terms or (search_query(q.name) + (" 4k" if q.is_4k else " blu-ray"))
        results = _api(text, None) if use_api else _scrape(text)
    for o in results:
        o.score = (score_barcode(q.name, q.is_4k, q.edition, o.title, loose=True)
                   if o.by_barcode else score(q.name, q.is_4k, q.edition, o.title, loose=True))
        if o.by_barcode and o.score >= 55:
            o.score = max(o.score, 85.0)
    return results


def refresh(url: str) -> Offer | None:
    # eBay listings are transient; always re-search instead.
    return None
