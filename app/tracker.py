"""Price checking: match each title at each store and record prices."""
import json
import logging
from datetime import datetime

from . import config, notify
from .db import (STORE_NAMES, ExcludedUrl, Listing, PricePoint, Title, enabled_stores,
                 get_session, select)
from .matching import AUTO_ACCEPT, REVIEW, score
from .stores import MODULES
from .stores.base import Offer, Query

log = logging.getLogger(__name__)


def shipping_for(store: str, price: float | None, listed: float | None) -> float:
    if listed is not None:
        return listed
    if price is None:
        return 0.0
    threshold = config.FREE_SHIPPING_OVER.get(store, 0)
    if threshold and price >= threshold:
        return 0.0
    return config.SHIPPING.get(store, 0.0)


def total_for(listing: Listing) -> float | None:
    if listing.price is None:
        return None
    return round(listing.price + shipping_for(listing.store, listing.price, listing.shipping), 2)


def _choose(store: str, offers: list[Offer]) -> tuple[Offer | None, list[Offer]]:
    ranked = sorted(offers, key=lambda o: -o.score)
    plausible = [o for o in ranked if o.score >= REVIEW]
    if not plausible:
        return None, ranked[:5]
    strong = [o for o in plausible if o.score >= AUTO_ACCEPT]
    pool = strong or plausible
    priced = [o for o in pool if o.price is not None and o.in_stock]
    if store == "ebay":
        # Many sellers, same product: take the cheapest delivered price.
        pick = min(priced, key=lambda o: o.price + (o.shipping or 0)) if priced else pool[0]
    else:
        top = pool[0].score
        near = [o for o in (priced or pool) if o.score >= top - 3]
        pick = min(near, key=lambda o: o.price if o.price is not None else 1e9) if near else pool[0]
    return pick, ranked[:6]


def _cands_json(cands: list[Offer]) -> str:
    return json.dumps([
        {"url": o.url, "title": o.title, "price": o.price, "shipping": o.shipping,
         "in_stock": o.in_stock, "score": round(o.score, 1), "image": o.image}
        for o in cands
    ])


def _discard_amazon_parent_prices(session, title: Title, url: str) -> None:
    stale = session.exec(select(PricePoint).where(
        PricePoint.title_id == title.id, PricePoint.store == "amazon", PricePoint.url == url
    )).all()
    if not stale:
        return
    for point in stale:
        session.delete(point)
    remaining = session.exec(select(PricePoint).where(
        PricePoint.title_id == title.id, PricePoint.in_stock == True  # noqa: E712
    )).all()
    title.lowest_ever = min((point.total for point in remaining), default=None)


def check_store(session, title: Title, store: str) -> Listing:
    mod = MODULES[store]
    listing = session.exec(
        select(Listing).where(Listing.title_id == title.id, Listing.store == store)).first()
    if listing is None:
        listing = Listing(title_id=title.id, store=store)
        session.add(listing)
    excluded = {e.url for e in session.exec(
        select(ExcludedUrl).where(ExcludedUrl.title_id == title.id, ExcludedUrl.store == store))}
    q = Query(title)
    listing.error = None
    pinned_url = listing.url if listing.pinned and store != "ebay" else None
    # Release the write lock before slow network scraping so the UI stays usable.
    session.commit()
    offer: Offer | None = None
    try:
        if pinned_url:
            offer = mod.refresh(pinned_url)
            if offer:
                if store == "amazon" and offer.url != pinned_url:
                    _discard_amazon_parent_prices(session, title, pinned_url)
                offer.score = score(title.name, True, title.edition or "", offer.title)
                if offer.score < REVIEW:
                    offer = None
            listing.status = "ok" if offer else "error"
            if not offer:
                listing.error = "Could not read the pinned product page"
        else:
            offers = [o for o in mod.search(q) if o.url not in excluded]
            if store == "amazon" and title.amazon_asin:
                wl_url = mod.product_url(title.amazon_asin)
                if wl_url not in excluded and all(o.url != wl_url for o in offers):
                    wl = mod.refresh(wl_url)
                    if wl:
                        if wl.url != wl_url:
                            _discard_amazon_parent_prices(session, title, wl_url)
                        wl.score = max(score(title.name, title.is_4k, title.edition or "", wl.title, loose=True), 0)
                        # It was on the user's own wishlist, so trust it if the format matches.
                        if wl.score >= 50:
                            wl.score = max(wl.score, AUTO_ACCEPT)
                        offers.append(wl)
            offer, cands = _choose(store, offers)
            listing.candidates_json = _cands_json(cands)
            if offer and store in ("amazon", "umbrella", "dvdhub"):
                source_url = offer.url
                refreshed = mod.refresh(source_url)
                if refreshed:
                    if store == "amazon" and refreshed.url != source_url:
                        _discard_amazon_parent_prices(session, title, source_url)
                    refreshed.score = score(title.name, title.is_4k, title.edition or "",
                                             refreshed.title, loose=offer.by_barcode)
                    offer = refreshed if refreshed.score >= REVIEW else None
                else:
                    offer = None
            elif offer and offer.price is None and store != "ebay":
                refreshed = mod.refresh(offer.url)
                if refreshed:
                    refreshed.score = offer.score
                    offer = refreshed
            if offer is None:
                listing.status = "not_found"
            else:
                listing.status = "ok" if offer.score >= AUTO_ACCEPT else "review"
    except Exception as e:  # keep going with other stores
        log.exception("%s check failed for %s", store, title.name)
        listing.status = "error"
        listing.error = f"{type(e).__name__}: {e}"[:300]

    listing.checked_at = datetime.utcnow()
    if offer:
        listing.url = offer.url
        listing.product_title = offer.title
        listing.image = offer.image
        listing.price = offer.price
        listing.shipping = offer.shipping
        listing.in_stock = offer.in_stock
        listing.condition = offer.condition
        listing.match_score = offer.score
        if offer.price is not None:
            session.add(PricePoint(
                title_id=title.id, store=store, price=offer.price,
                total=total_for(listing) or offer.price, in_stock=offer.in_stock, url=offer.url))
    elif listing.status == "not_found":
        listing.url = listing.url if listing.pinned else None
        listing.price = None
        listing.product_title = None
        listing.in_stock = None
        listing.match_score = None
    session.add(listing)
    return listing


def update_best(session, title: Title) -> None:
    listings = session.exec(select(Listing).where(Listing.title_id == title.id)).all()
    best, best_total = None, None
    for l in listings:
        if l.status != "ok" or not l.in_stock:
            continue
        tot = total_for(l)
        if tot is not None and (best_total is None or tot < best_total):
            best, best_total = l, tot
    prev_low = title.lowest_ever
    title.best_price = best_total
    title.best_store = best.store if best else None
    title.best_url = best.url if best else None
    if best_total is not None and (title.lowest_ever is None or best_total < title.lowest_ever):
        title.lowest_ever = best_total
    _maybe_notify(title, best, best_total, prev_low)


def _maybe_notify(title: Title, best: Listing | None, total: float | None, prev_low: float | None):
    if best is None or total is None:
        return
    if title.target_price and total > title.target_price and prev_low is not None and total >= prev_low:
        # Price went back up: re-arm alerts for the next drop.
        title.last_notified_price = None
    if not notify.enabled():
        return
    already = title.last_notified_price is not None and total >= title.last_notified_price
    reason = None
    if title.target_price and total <= title.target_price and not already:
        reason = f"at or below your target of ${title.target_price:.2f}"
    elif prev_low is not None and total < prev_low - 0.01 and not already:
        reason = f"new lowest price (was ${prev_low:.2f})"
    if not reason:
        return
    fmt = "4K" if title.is_4k else "Blu-ray"
    link = f"\n{config.BASE_URL}/title/{title.id}" if config.BASE_URL else ""
    ok = notify.send(
        f"💿 {title.name} ({fmt}) ${total:.2f} at {STORE_NAMES[best.store]}",
        f"{title.name} {('- ' + title.edition) if title.edition else ''}\n"
        f"${total:.2f} delivered at {STORE_NAMES[best.store]} — {reason}.\n{best.url}{link}",
    )
    if ok:
        title.last_notified_price = total


def check_title(title_id: int, stores: list[str] | None = None, progress=None) -> None:
    with get_session() as s:
        title = s.get(Title, title_id)
        if not title or not title.is_4k:
            return
        for store in stores or enabled_stores():
            if progress:
                progress(f"{title.name} — {STORE_NAMES[store]}")
            check_store(s, title, store)
            s.commit()
        title.last_checked = datetime.utcnow()
        update_best(s, title)
        s.add(title)
        s.commit()


def check_all(progress=None) -> None:
    with get_session() as s:
        ids = [t.id for t in s.exec(select(Title).where(
            Title.purchased == False, Title.is_4k == True))]  # noqa: E712
    for i, tid in enumerate(ids, 1):
        check_title(tid, progress=(lambda msg, i=i: progress(f"[{i}/{len(ids)}] {msg}")) if progress else None)


def check_titles(ids: list[int], progress=None) -> None:
    for i, tid in enumerate(ids, 1):
        check_title(tid, progress=(lambda msg, i=i: progress(f"[{i}/{len(ids)}] {msg}")) if progress else None)
