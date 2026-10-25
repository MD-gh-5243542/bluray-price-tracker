"""CeX AU listings for the main wishlist matcher."""
import json
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import httpx
from sqlmodel import select

from .. import cex as catalogue
from .. import config
from ..db import CexProduct, get_session
from ..matching import search_query
from .base import Offer, Query

STORE = "cex"
_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
_BASE_FILTER = "boxVisibilityOnWeb=1 AND boxSaleAllowed=1 AND sellPrice > 0"


def _is_supported_category(category: str, wanted_4k: bool | None) -> bool:
    if wanted_4k is None:
        return category.startswith("Blu-Ray")
    if wanted_4k:
        return category == "Blu-Ray 4K"
    return category.startswith("Blu-Ray") and "4K" not in category


def _offer(q: Query | None, hit: dict) -> Offer | None:
    product = catalogue._normalise_hit(hit)
    wanted_4k = q.is_4k if q is not None else None
    if not product["box_id"] or not _is_supported_category(product["category"], wanted_4k):
        return None
    barcode_match = q is not None and any(
        catalogue._same_barcode(product["box_id"], upc) for upc in q.upcs
    )
    candidate = catalogue._match_candidate(product, q.is_4k if q is not None else ("4K" in product["category"]))
    score = (q.score_barcode(candidate) if barcode_match
             else q.score(candidate, loose=True) if q else 0.0)
    if barcode_match and score >= 50:
        score = max(score, 90)

    try:
        price = float(product["sell_price"]) if product["sell_price"] is not None else None
    except (TypeError, ValueError):
        price = None
    branches = product["branch_stock"] or {}
    local_stock = any(branches.get(store) is True for store in catalogue.BRANCHES)
    online_stock = product["online_stock"] is True
    in_stock = local_stock or online_stock
    if local_stock:
        shipping = 0.0
    elif online_stock and price is not None:
        shipping = (
            catalogue.DELIVERY_PER_ITEM
            if price <= catalogue.FREE_DELIVERY_ITEM_OVER else 0.0
        )
    else:
        shipping = None
    return Offer(
        store=STORE,
        url=catalogue._box_url(product["box_id"], product["category"]),
        title=product["name"],
        price=price,
        shipping=shipping,
        in_stock=in_stock,
        image=product["image"],
        score=score,
        by_barcode=barcode_match,
    )


def search(q: Query) -> list[Offer]:
    hits: dict[str, dict] = {}
    wanted_filter = catalogue.FILTER_4K if q.is_4k else _BASE_FILTER
    headers = {"user-agent": config.USER_AGENT, "accept": "application/json"}
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        for upc in q.upcs:
            payload = catalogue._query(
                client, upc, filters=wanted_filter, hits_per_page=10,
            )
            for hit in payload["hits"]:
                box_id = str(hit.get("boxId") or hit.get("objectID") or "")
                if box_id and any(catalogue._same_barcode(box_id, upc) for upc in q.upcs):
                    hits[box_id] = hit

        if q.title_search_too:
            text = q.search_terms or search_query(q.name)
            payload = catalogue._query(
                client, text, filters=wanted_filter, hits_per_page=20,
            )
            for hit in payload["hits"]:
                box_id = str(hit.get("boxId") or hit.get("objectID") or "")
                if box_id:
                    hits.setdefault(box_id, hit)

    offers = []
    for hit in hits.values():
        offer = _offer(q, hit)
        if offer:
            offers.append(offer)
    return offers


def refresh(url: str) -> Offer | None:
    box_id = parse_qs(urlsplit(url).query).get("id", [""])[0]
    if not box_id:
        return None
    headers = {"user-agent": config.USER_AGENT, "accept": "application/json"}
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        payload = catalogue._query(
            client, box_id, filters=_BASE_FILTER, hits_per_page=10,
        )
    hit = next((item for item in payload["hits"]
                if str(item.get("boxId") or item.get("objectID") or "") == box_id), None)
    if not hit:
        return None
    return _offer(None, hit)


def bargains() -> list[Offer]:
    cutoff = datetime.utcnow() - timedelta(hours=48)
    with get_session() as session:
        products = session.exec(select(CexProduct).where(
            CexProduct.category == "Blu-Ray 4K",
            CexProduct.last_seen >= cutoff,
        )).all()
    offers = []
    for product in products:
        if product.sell_price is None:
            continue
        branches = json.loads(product.branch_stock_json or "{}")
        local_stock = any(branches.get(store) is True for store in catalogue.BRANCHES)
        online_stock = product.online_stock is True
        if not local_stock and not online_stock:
            continue
        shipping = (
            0.0 if local_stock or product.sell_price > catalogue.FREE_DELIVERY_ITEM_OVER
            else catalogue.DELIVERY_PER_ITEM
        )
        offers.append(Offer(
            store=STORE, url=product.url or catalogue._box_url(product.box_id, product.category),
            title=product.name, price=product.sell_price, shipping=shipping,
            in_stock=True, image=product.image,
        ))
    return sorted(offers, key=lambda offer: offer.price or float("inf"))[:10]
