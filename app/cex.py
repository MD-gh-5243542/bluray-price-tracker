"""CeX AU catalogue monitoring and collection buyback matching."""
import json
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from sqlmodel import select

from . import config
from .db import CexEvent, CexFavourite, CexProduct, CexTitleMatch, Title, get_session
from .matching import REVIEW, score, score_barcode

ALGOLIA_URL = (
    "https://search.webuy.io/1/indexes/prod_cex_au/query"
    "?x-algolia-application-id=LNNFEEWZVA"
    "&x-algolia-api-key=bf79f2b6699e60a18ae330a1248b452c"
)
ALGOLIA_BATCH_URL = ALGOLIA_URL.replace(
    "/indexes/prod_cex_au/query", "/indexes/*/queries",
)
ATTRIBUTES = [
    "boxId", "boxName", "categoryName", "categoryFriendlyName", "productLineName",
    "sellPrice", "cashBuyPrice", "exchangePrice", "cashPriceCalculated",
    "exchangePriceCalculated", "outOfStock", "inStockOnline",
    "inStockStore", "imageUrls", "priceLastChanged",
]
FILTER_4K = (
    'boxVisibilityOnWeb=1 AND boxSaleAllowed=1 AND sellPrice > 0 '
    'AND categoryFriendlyName:"Blu-Ray 4K"'
)
BRANCHES = ("Blacktown", "Rouse Hill")
TRADE_IN_MINIMUM = 3.0
FREE_DELIVERY_ITEM_OVER = config.CEX_FREE_DELIVERY_ITEM_OVER
DELIVERY_PER_ITEM = config.CEX_DELIVERY_PER_ITEM
_RATING_RE = re.compile(r"\(\s*(?:G|PG|M|MA15\+|R18\+|R13\+|R)\s*\)", re.I)
_BOX_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


def _barcodes(title: Title) -> set[str]:
    return {value for value in re.split(r"[\s,;]+", f"{title.upc or ''},{title.alt_upcs or ''}")
            if value}


def _raise_if_cancelled() -> None:
    from .tracker import Cancelled, cancel_event

    if cancel_event.is_set():
        raise Cancelled()


def _price_text(value: str | None) -> str:
    try:
        return f"${float(value):.2f}"
    except (TypeError, ValueError):
        return "—"


def _match_candidate(product: CexProduct | dict, wanted_4k: bool) -> str:
    name = product["name"] if isinstance(product, dict) else product.name
    name = _RATING_RE.sub(" ", name)
    return name + (" 4K Ultra HD" if wanted_4k else " Blu-ray")


def _box_url(box_id: str, category: str) -> str:
    category_slug = re.sub(r"[^A-Z0-9]+", "-", category.upper()).strip("-")
    return "https://au.webuy.com/product-detail?" + urlencode({
        "id": box_id, "categoryName": category_slug, "superCatName": "FILM-TV",
    })


def parse_favourite_links(text: str) -> dict[str, str]:
    """Validate pasted CeX product links and return their product IDs and URLs."""
    if len(text) > 200_000:
        raise ValueError("Paste up to 1,000 CeX product links at a time.")
    tokens = [token for token in re.split(r"[\s,;]+", text.strip()) if token]
    if not tokens:
        raise ValueError("Paste CeX product links, one per line.")

    links: dict[str, str] = {}
    for token in tokens:
        if token.lower().startswith(("au.webuy.com/", "www.au.webuy.com/")):
            token = f"https://{token}"
        parts = urlsplit(token)
        if parts.scheme or parts.netloc:
            if (parts.scheme.lower() not in ("http", "https")
                    or (parts.hostname or "").lower() not in ("au.webuy.com", "www.au.webuy.com")
                    or parts.path.rstrip("/") != "/product-detail"):
                raise ValueError("Only CeX AU product-detail links are supported; paste individual product links.")
            ids = parse_qs(parts.query).get("id", [])
            if len(ids) != 1:
                raise ValueError(f"CeX product link is missing a valid product ID: {token}")
            box_id = ids[0]
            url = token
        else:
            box_id = token
            url = f"https://au.webuy.com/product-detail?id={box_id}"
        if not _BOX_ID_RE.fullmatch(box_id):
            raise ValueError(f"Invalid CeX product ID: {box_id}")
        links[box_id] = url

    if len(links) > 1_000:
        raise ValueError("Paste no more than 1,000 distinct CeX product links at a time.")
    return links


def _normalise_hit(hit: dict) -> dict:
    images = hit.get("imageUrls") or {}
    out_of_stock = hit.get("outOfStock")
    branch_stock = None
    if isinstance(out_of_stock, list):
        branch_stock = {store: store not in out_of_stock for store in BRANCHES}
    return {
        "box_id": str(hit.get("boxId") or hit.get("objectID") or ""),
        "name": str(hit.get("boxName") or ""),
        "category": str(hit.get("categoryFriendlyName") or hit.get("categoryName") or ""),
        "sell_price": hit.get("sellPrice"),
        "cash_buy_price": (
            hit.get("cashPriceCalculated")
            if hit.get("cashPriceCalculated") is not None else hit.get("cashBuyPrice")
        ),
        "exchange_price": (
            hit.get("exchangePriceCalculated")
            if hit.get("exchangePriceCalculated") is not None else hit.get("exchangePrice")
        ),
        "online_stock": bool(hit["inStockOnline"]) if "inStockOnline" in hit else None,
        "branch_stock": branch_stock,
        "image": images.get("large") if isinstance(images, dict) else None,
        "price_changed": hit.get("priceLastChanged"),
    }


def _query(client: httpx.Client, query: str, *, filters: str, page: int = 0,
           hits_per_page: int = 20, facets: list[str] | None = None) -> dict:
    body = {
        "query": query,
        "filters": filters,
        "page": page,
        "hitsPerPage": hits_per_page,
        "attributesToRetrieve": ATTRIBUTES,
    }
    if facets:
        body["facets"] = facets
    response = client.post(ALGOLIA_URL, json=body)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload.get("hits"), list):
        raise RuntimeError("CeX search returned an unexpected response")
    return payload


def _query_many(client: httpx.Client, queries: list[str]) -> list[dict]:
    requests = [{
        "indexName": "prod_cex_au",
        "params": urlencode({
            "query": query,
            "hitsPerPage": 10,
            "attributesToRetrieve": ",".join(ATTRIBUTES),
        }),
    } for query in queries]
    response = client.post(ALGOLIA_BATCH_URL, json={"requests": requests})
    response.raise_for_status()
    results = response.json().get("results")
    if not isinstance(results, list) or len(results) != len(queries):
        raise RuntimeError("CeX favourites search returned an unexpected response")
    if any(not isinstance(result, dict) or not isinstance(result.get("hits"), list)
           for result in results):
        raise RuntimeError("CeX favourites search returned an unexpected response")
    return results


def _catalogue(client: httpx.Client, progress=None) -> list[dict]:
    first = _query(client, "", filters=FILTER_4K, hits_per_page=1000, facets=["sellPrice"])
    total = first.get("nbHits", 0)
    hits = first["hits"]
    if total > len(hits):
        stats = first.get("facets_stats", {}).get("sellPrice", {})
        lower, upper = stats.get("min"), stats.get("max")
        if lower is None or upper is None or lower >= upper:
            raise RuntimeError("CeX catalogue exceeds its search page limit and has no usable price range")

        def price_partition(low: float, high: float, include_high: bool, depth: int = 0) -> list[dict]:
            _raise_if_cancelled()
            upper_operator = "<=" if include_high else "<"
            filters = f"{FILTER_4K} AND sellPrice >= {low} AND sellPrice {upper_operator} {high}"
            response = _query(client, "", filters=filters, hits_per_page=1000)
            if response.get("nbHits", 0) <= len(response["hits"]):
                return response["hits"]
            if depth >= 16:
                raise RuntimeError("Unable to page the complete CeX catalogue by price")
            middle = (low + high) / 2
            return (
                price_partition(low, middle, False, depth + 1)
                + price_partition(middle, high, include_high, depth + 1)
            )

        hits = price_partition(lower, upper, True)
    pages = first.get("nbPages", 0)
    if total <= len(hits):
        pages = 0
    for page in range(1, pages):
        _raise_if_cancelled()
        if progress:
            progress(f"Loading CeX 4K catalogue page {page + 1} of {pages}")
        hits.extend(_query(client, "", filters=FILTER_4K, page=page, hits_per_page=1000)["hits"])
    products = [_normalise_hit(hit) for hit in hits]
    return [product for product in products if product["box_id"] and product["name"]]


def _record_products(products: list[dict], *, baseline: bool) -> None:
    now = datetime.utcnow()
    with get_session() as session:
        existing = {product.box_id: product
                    for product in session.exec(select(CexProduct)).all()}
        for data in products:
            _raise_if_cancelled()
            product = existing.get(data["box_id"])
            created = product is None
            if created:
                product = CexProduct(
                    box_id=data["box_id"], first_seen=now,
                    name=data["name"], category=data["category"],
                )
                existing[data["box_id"]] = product
            old_price = product.sell_price
            old_cash = product.cash_buy_price
            old_exchange = product.exchange_price
            old_online = product.online_stock
            old_branches = json.loads(product.branch_stock_json or "{}")
            new_branches = data["branch_stock"]
            if created and not baseline:
                session.add(CexEvent(
                    box_id=data["box_id"], product_name=data["name"], event_type="new",
                    old_value=None, new_value=data["category"], created_at=now,
                ))
            elif not created:
                if old_price != data["sell_price"]:
                    session.add(CexEvent(
                        box_id=data["box_id"], product_name=data["name"], event_type="price",
                        old_value=str(old_price), new_value=str(data["sell_price"]), created_at=now,
                    ))
                if old_online != data["online_stock"] or old_branches != (new_branches or {}):
                    previous = {"online": old_online, **old_branches}
                    current = {"online": data["online_stock"], **(new_branches or {})}
                    session.add(CexEvent(
                        box_id=data["box_id"], product_name=data["name"], event_type="stock",
                        old_value=json.dumps(previous, sort_keys=True),
                        new_value=json.dumps(current, sort_keys=True), created_at=now,
                    ))
                    was_available = old_online or any(old_branches.values())
                    now_available = data["online_stock"] or any((new_branches or {}).values())
                    if not was_available and now_available:
                        session.add(CexEvent(
                            box_id=data["box_id"], product_name=data["name"], event_type="stock_added",
                            old_value=None, new_value=json.dumps(current, sort_keys=True), created_at=now,
                        ))
                if old_cash != data["cash_buy_price"] or old_exchange != data["exchange_price"]:
                    session.add(CexEvent(
                        box_id=data["box_id"], product_name=data["name"], event_type="buyback",
                        old_value=f"{old_cash}/{old_exchange}",
                        new_value=f"{data['cash_buy_price']}/{data['exchange_price']}", created_at=now,
                    ))
            product.name = data["name"]
            product.category = data["category"]
            product.url = _box_url(data["box_id"], data["category"])
            product.image = data["image"]
            product.sell_price = data["sell_price"]
            product.cash_buy_price = data["cash_buy_price"]
            product.exchange_price = data["exchange_price"]
            product.online_stock = data["online_stock"]
            product.branch_stock_json = json.dumps(new_branches, sort_keys=True) if new_branches is not None else None
            product.price_changed = data["price_changed"]
            product.last_seen = now
            session.add(product)
        session.commit()


def _best_match(title: Title, products: list[CexProduct], *, wanted_4k: bool) -> tuple[CexProduct, float] | None:
    candidates = []
    upcs = _barcodes(title)
    mode = getattr(title, "match_mode", "any") or "any"
    if wanted_4k and mode == "exact":
        upcs = {title.upc} if title.upc else set()
    if wanted_4k and mode in ("exact", "selected"):
        products = [product for product in products if product.box_id in upcs]
    for product in products:
        if wanted_4k and product.category != "Blu-Ray 4K":
            continue
        if not wanted_4k and (not product.category.startswith("Blu-Ray") or "4K" in product.category):
            continue
        barcode_match = product.box_id in upcs
        if wanted_4k and mode == "exact" and not barcode_match:
            continue
        candidate = _match_candidate(product, wanted_4k)
        if barcode_match:
            points = score_barcode(title.name, wanted_4k, title.edition or "", candidate,
                                   wanted_year=title.year)
        else:
            points = score(title.name, wanted_4k, title.edition or "", candidate,
                           loose=True, strict_edition=mode != "any" if wanted_4k else False,
                           wanted_year=title.year)
        if points >= REVIEW and (barcode_match or points >= 78):
            candidates.append((product, points))
    if not candidates:
        return None
    return max(candidates, key=lambda item: (
        item[1],
        item[0].online_stock or any(json.loads(item[0].branch_stock_json or "{}").values()),
        -(item[0].sell_price or float("inf")),
    ))


def _refresh_title_matches() -> None:
    with get_session() as session:
        titles = session.exec(select(Title)).all()
        products = session.exec(select(CexProduct)).all()
        fresh_after = datetime.utcnow() - timedelta(hours=48)
        uhd_products = [
            product for product in products
            if product.category == "Blu-Ray 4K" and product.last_seen >= fresh_after
        ]
        standard_title_ids = {
            title.id for title in titles if title.purchased and not title.is_4k
        }
        eligible_keys = {
            f"wishlist:{title.id}" for title in titles if title.is_4k and not title.purchased
        } | {f"collection:{title_id}" for title_id in standard_title_ids}
        for title in titles:
            if title.is_4k and not title.purchased:
                match = _best_match(title, uhd_products, wanted_4k=True)
                key, kind = f"wishlist:{title.id}", "wishlist"
            elif title.id in standard_title_ids:
                continue
            else:
                continue
            row = session.get(CexTitleMatch, key)
            if match:
                product, points = match
                if row is None:
                    row = CexTitleMatch(key=key, title_id=title.id, kind=kind)
                row.box_id = product.box_id
                row.match_score = points
                row.checked_at = datetime.utcnow()
                session.add(row)
            elif row:
                session.delete(row)
        for row in session.exec(select(CexTitleMatch)).all():
            if row.key not in eligible_keys:
                session.delete(row)
        session.commit()


def _match_collection_titles(client: httpx.Client, progress=None) -> None:
    with get_session() as session:
        titles = session.exec(select(Title).where(
            Title.purchased == True, Title.is_4k == False  # noqa: E712
        )).all()
    for index, title in enumerate(titles, 1):
        _raise_if_cancelled()
        query = title.upc or f"{title.name} {title.year or ''}".strip()
        if progress and index % 10 == 1:
            progress(f"Checking CeX buyback values {index} of {len(titles)}")
        payload = _query(
            client, query,
            filters='boxVisibilityOnWeb=1 AND boxBuyAllowed=1 AND sellPrice > 0',
        )
        products = []
        for hit in payload["hits"]:
            data = _normalise_hit(hit)
            if not data["box_id"] or not data["name"]:
                continue
            if not data["category"].startswith("Blu-Ray") or "4K" in data["category"]:
                continue
            if title.upc and data["box_id"] == title.upc:
                points = 100
            else:
                candidate = _match_candidate(data, False)
                points = score(title.name, False, title.edition or "", candidate,
                               loose=True, strict_edition=False, wanted_year=title.year)
            if points >= REVIEW:
                products.append((data, points))
        if not products:
            with get_session() as session:
                stale = session.get(CexTitleMatch, f"collection:{title.id}")
                if stale:
                    session.delete(stale)
                session.commit()
            continue
        data, points = max(products, key=lambda item: item[1])
        _record_products([data], baseline=True)
        with get_session() as session:
            row = session.get(CexTitleMatch, f"collection:{title.id}")
            if row is None:
                row = CexTitleMatch(
                    key=f"collection:{title.id}", title_id=title.id, kind="collection",
                )
            row.box_id = data["box_id"]
            row.match_score = points
            row.checked_at = datetime.utcnow()
            session.add(row)
            session.commit()


def _favourite_products(client: httpx.Client, progress=None) -> list[dict]:
    with get_session() as session:
        favourite_ids = [item.box_id for item in session.exec(
            select(CexFavourite).order_by(CexFavourite.added_at)
        ).all()]
    products = []
    checked_ids = []
    for start in range(0, len(favourite_ids), 50):
        _raise_if_cancelled()
        box_ids = favourite_ids[start:start + 50]
        if progress:
            progress(
                f"Refreshing CeX favourites {start + 1}-{start + len(box_ids)} "
                f"of {len(favourite_ids)}"
            )
        results = _query_many(client, box_ids)
        for box_id, payload in zip(box_ids, results):
            checked_ids.append(box_id)
            hit = next((item for item in payload["hits"]
                        if str(item.get("boxId") or item.get("objectID") or "") == box_id), None)
            if hit:
                product = _normalise_hit(hit)
                if product["name"]:
                    products.append(product)
    with get_session() as session:
        for box_id in checked_ids:
            favourite = session.get(CexFavourite, box_id)
            if favourite:
                favourite.checked_at = datetime.utcnow()
                session.add(favourite)
        session.commit()
    return products


def sync(progress=None) -> None:
    """Refresh CeX 4K inventory, favourites, and owned standard Blu-ray buyback values."""
    timeout = httpx.Timeout(30.0, connect=10.0)
    headers = {"user-agent": config.USER_AGENT, "accept": "application/json"}
    with httpx.Client(timeout=timeout, headers=headers) as client:
        if progress:
            progress("Loading CeX 4K catalogue")
        products = _catalogue(client, progress)
        favourite_products = _favourite_products(client, progress)
        with get_session() as session:
            baseline = session.exec(select(CexProduct.id)).first() is None
        _record_products(products + favourite_products, baseline=baseline)
        _match_collection_titles(client, progress)
    _refresh_title_matches()
    with get_session() as session:
        session.add(CexEvent(
            box_id="__tracker__", product_name="CeX catalogue", event_type="sync",
            old_value=None, new_value=str(len(products)), created_at=datetime.utcnow(),
        ))
        session.commit()


def page_data() -> dict:
    with get_session() as session:
        titles = session.exec(select(Title)).all()
        all_products = {item.box_id: item for item in session.exec(select(CexProduct)).all()}
        favourites = session.exec(
            select(CexFavourite).order_by(CexFavourite.added_at.desc())
        ).all()
        matches = {item.key: item for item in session.exec(select(CexTitleMatch)).all()}
        events = session.exec(
            select(CexEvent).where(CexEvent.event_type != "sync")
            .order_by(CexEvent.created_at.desc()).limit(100)
        ).all()
        sync_event = session.exec(
            select(CexEvent).where(CexEvent.event_type == "sync")
            .order_by(CexEvent.created_at.desc()).limit(1)
        ).first()
    fresh_after = datetime.utcnow() - timedelta(hours=48)
    products = {box_id: product for box_id, product in all_products.items()
                if product.last_seen >= fresh_after}
    favourite_rows = []
    for favourite in favourites:
        product = all_products.get(favourite.box_id)
        favourite_rows.append({
            "box_id": favourite.box_id,
            "url": product.url if product and product.url else favourite.url,
            "product": product,
            "stale": not product or product.last_seen < fresh_after,
            "not_found": bool(
                favourite.checked_at
                and (not product or product.last_seen < favourite.checked_at)
            ),
            "branches": (
                json.loads(product.branch_stock_json)
                if product and product.branch_stock_json else None
            ),
        })
    favourite_rows.sort(key=lambda row: (
        (row["product"].name if row["product"] else row["box_id"]).lower()
    ))
    wanted_by_id = {}
    owned = []
    for title in titles:
        if title.is_4k and not title.purchased:
            match = matches.get(f"wishlist:{title.id}")
            product = products.get(match.box_id) if match else None
            branches = json.loads(product.branch_stock_json or "{}") if product else {}
            online_postage = (
                0.0 if product and product.sell_price is not None
                and product.sell_price > FREE_DELIVERY_ITEM_OVER
                else DELIVERY_PER_ITEM if product and product.sell_price is not None
                else None
            )
            online_delivered = (
                product.sell_price + online_postage
                if product and product.online_stock and product.sell_price is not None
                and online_postage is not None else None
            )
            wanted_by_id[title.id] = {
                "title": title, "product": product, "branches": branches,
                "online_delivered": online_delivered, "online_postage": online_postage,
                "match_score": match.match_score if match else None,
                "competitive": bool(
                    product and match and match.match_score >= 80 and product.sell_price is not None and
                    ((any(branches.values()) and
                      (title.best_price is None or product.sell_price <= title.best_price))
                     or (product.online_stock and online_delivered is not None and
                         (title.best_price is None or online_delivered <= title.best_price)))
                ),
            }
        elif title.purchased and not title.is_4k:
            match = matches.get(f"collection:{title.id}")
            product = products.get(match.box_id) if match else None
            if product and max(product.cash_buy_price or 0, product.exchange_price or 0) >= TRADE_IN_MINIMUM:
                owned.append({
                    "title": title, "product": product,
                    "match_score": match.match_score if match else None,
                })
    wanted = sorted(wanted_by_id.values(),
                    key=lambda row: (not row["competitive"], row["title"].name.lower()))
    owned.sort(key=lambda row: -max(
        row["product"].cash_buy_price or 0, row["product"].exchange_price or 0,
    ))
    recent = [event for event in events
              if event.event_type in ("new", "stock_added", "price", "stock", "buyback")
              and event.created_at >= datetime.utcnow() - timedelta(days=30)]
    event_rows = []
    for event in recent[:50]:
        product = all_products.get(event.box_id)
        if event.event_type == "new":
            change = "First observed by this tracker"
        elif event.event_type == "stock_added":
            change = "Back in stock at CeX"
        elif event.event_type == "price":
            change = f"{_price_text(event.old_value)} to {_price_text(event.new_value)}"
        elif event.event_type == "buyback":
            old_cash, old_exchange = (event.old_value or "/").split("/", 1)
            new_cash, new_exchange = (event.new_value or "/").split("/", 1)
            change = (
                f"Cash {_price_text(old_cash)} to {_price_text(new_cash)}; "
                f"voucher {_price_text(old_exchange)} to {_price_text(new_exchange)}"
            )
        else:
            change = "Availability changed"
        event_rows.append({"event": event, "product": product, "change": change})
    return {
        "favourites": favourite_rows,
        "wanted": wanted,
        "owned": owned,
        "events": event_rows,
        "last_sync": sync_event.created_at if sync_event else None,
        "stale": not sync_event or sync_event.created_at < datetime.utcnow() - timedelta(hours=36),
        "catalogue_count": len([p for p in products.values() if p.category == "Blu-Ray 4K"]),
    }
