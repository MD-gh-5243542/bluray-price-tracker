"""CeX AU 4K listings for the main wishlist matcher."""
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


def _same_barcode(box_id: str, upc: str) -> bool:
    if box_id == upc:
        return True
    if box_id.isdigit() and upc.isdigit():
        return (box_id.lstrip("0") or "0") == (upc.lstrip("0") or "0")
    return False


def _offer(q: Query | None, hit: dict) -> Offer | None:
    product = catalogue._normalise_hit(hit)
    if not product["box_id"] or product["category"] != "Blu-Ray 4K":
        return None
    barcode_match = q is not None and any(_same_barcode(product["box_id"], upc) for upc in q.upcs)
    candidate = catalogue._match_candidate(product, True)
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
    delivery_unknown = (online_stock and not local_stock and price is not None
                        and price <= catalogue.FREE_DELIVERY_OVER)
    shipping = 0.0 if local_stock or (online_stock and price is not None
                                     and price > catalogue.FREE_DELIVERY_OVER) else None
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
        delivery_unknown=delivery_unknown,
    )


def search(q: Query) -> list[Offer]:
    hits: dict[str, dict] = {}
    headers = {"user-agent": config.USER_AGENT, "accept": "application/json"}
    with httpx.Client(timeout=_TIMEOUT, headers=headers) as client:
        for upc in q.upcs:
            payload = catalogue._query(
                client, upc, filters=catalogue.FILTER_4K, hits_per_page=10,
            )
            for hit in payload["hits"]:
                box_id = str(hit.get("boxId") or hit.get("objectID") or "")
                if box_id and any(_same_barcode(box_id, upc) for upc in q.upcs):
                    hits[box_id] = hit

        if q.title_search_too:
            text = q.search_terms or search_query(q.name)
            payload = catalogue._query(
                client, text, filters=catalogue.FILTER_4K, hits_per_page=20,
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
            client, box_id, filters=catalogue.FILTER_4K, hits_per_page=10,
        )
    hit = next((item for item in payload["hits"]
                if str(item.get("boxId") or item.get("objectID") or "") == box_id), None)
    if not hit:
        return None
    product = catalogue._normalise_hit(hit)
    if product["category"] != "Blu-Ray 4K":
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
        if not local_stock and not (online_stock and product.sell_price > catalogue.FREE_DELIVERY_OVER):
            continue
        offers.append(Offer(
            store=STORE, url=product.url or catalogue._box_url(product.box_id, product.category),
            title=product.name, price=product.sell_price, shipping=0.0,
            in_stock=True, image=product.image,
        ))
    return sorted(offers, key=lambda offer: offer.price or float("inf"))[:10]
