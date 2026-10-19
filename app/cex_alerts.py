"""Opt-in wishlist restock detection with a durable WAHA outbox."""
import json
import logging
from datetime import datetime, timedelta

from sqlmodel import select

from . import config, notify
from .db import (CexAlertState, CexProduct, CexStockAlert, ExcludedUrl, Setting, Title,
                 get_session, get_setting)
from .matching import AUTO_ACCEPT

log = logging.getLogger(__name__)
LOCATIONS = {"Blacktown": "Blacktown", "Rouse Hill": "Rouse Hill", "online": "Online"}


def settings() -> dict:
    enabled = get_setting("cex_stock_alerts", "true" if config.CEX_STOCK_ALERTS else "false") == "true"
    locations = [loc.strip() for loc in
                 get_setting("cex_alert_locations", config.CEX_ALERT_LOCATIONS).split(",") if loc.strip()]
    error = notify.waha_config_error()
    if not locations or any(loc not in LOCATIONS for loc in locations):
        error = "Choose valid CeX alert locations: Blacktown, Rouse Hill and/or online."
    with get_session() as session:
        pending = session.exec(select(CexStockAlert).where(
            CexStockAlert.status == "pending",
        ).order_by(CexStockAlert.id)).all()
    return {"enabled": enabled, "locations": [loc for loc in LOCATIONS if loc in locations],
            "choices": LOCATIONS, "config_error": error,
            "pending": len(pending), "error": next((row.error for row in reversed(pending) if row.error), None)}


def _stock(product: CexProduct) -> dict:
    return {"online": product.online_stock, **json.loads(product.branch_stock_json or "{}")}


def configure(enabled: bool, locations: list[str]) -> None:
    cfg = settings()
    changed = cfg["enabled"] != enabled or set(cfg["locations"]) != set(locations)
    with get_session() as session:
        for key, value in (("cex_stock_alerts", "true" if enabled else "false"),
                           ("cex_alert_locations", ",".join(loc for loc in LOCATIONS if loc in locations))):
            session.merge(Setting(key=key, value=value))
        if changed:
            for state in session.exec(select(CexAlertState)).all():
                session.delete(state)
            for alert in session.exec(select(CexStockAlert).where(CexStockAlert.status == "pending")).all():
                alert.status = "cancelled"
                session.add(alert)
        session.commit()


def refresh() -> None:
    from .cex import _best_match, _box_url, _raise_if_cancelled
    from .tracker import match_key

    cfg = settings()
    with get_session() as session:
        if not cfg["enabled"]:
            for state in session.exec(select(CexAlertState)).all():
                session.delete(state)
            for alert in session.exec(select(CexStockAlert).where(CexStockAlert.status == "pending")).all():
                alert.status = "cancelled"
                session.add(alert)
            session.commit()
            return
        if cfg["config_error"]:
            log.warning("CeX WAHA alerts not configured: %s", cfg["config_error"])
            return
        locations = cfg["locations"]
        titles = session.exec(select(Title).where(
            Title.is_4k == True, Title.purchased == False,  # noqa: E712
            Title.needs_review == False,  # noqa: E712
        )).all()
        products = session.exec(select(CexProduct).where(
            CexProduct.category == "Blu-Ray 4K",
            CexProduct.last_seen >= datetime.utcnow() - timedelta(hours=48),
        )).all()
        excluded = {}
        for row in session.exec(select(ExcludedUrl).where(ExcludedUrl.store == "cex")).all():
            excluded.setdefault(row.title_id, set()).add(match_key("cex", row.url))
        for title in titles:
            _raise_if_cancelled()
            fingerprint = json.dumps([
                locations, config.WAHA_URL, config.WAHA_SESSION, config.WAHA_CHAT_ID,
                title.name, title.year, title.match_mode, title.upc, title.alt_upcs,
            ])
            state = session.get(CexAlertState, title.id)
            baseline = state is None or state.fingerprint != fingerprint
            if baseline:
                for alert in session.exec(select(CexStockAlert).where(
                    CexStockAlert.title_id == title.id, CexStockAlert.status == "pending",
                )).all():
                    alert.status = "cancelled"
                    session.add(alert)
            previous = {} if baseline else json.loads(state.snapshot_json)
            current = dict(previous)
            for product in products:
                if f"cex:{product.box_id}" in excluded.get(title.id, set()):
                    continue
                match = _best_match(title, [product], wanted_4k=True)
                if not match or match[1] < AUTO_ACCEPT:
                    continue
                stock = _stock(product)
                before = previous.get(product.box_id, {})
                observed = {loc: value for loc in locations if (value := stock.get(loc)) is not None}
                current[product.box_id] = {**before, **observed}
                restocked = [loc for loc in locations
                             if observed.get(loc) is True
                             and (before.get(loc) is False or product.box_id not in previous)]
                if restocked and not baseline:
                    for alert in session.exec(select(CexStockAlert).where(
                        CexStockAlert.title_id == title.id, CexStockAlert.box_id == product.box_id,
                        CexStockAlert.status == "pending",
                    )).all():
                        restocked = list(dict.fromkeys(json.loads(alert.locations_json) + restocked))
                        alert.status = "cancelled"
                        session.add(alert)
                    session.add(CexStockAlert(
                        title_id=title.id, box_id=product.box_id,
                        locations_json=json.dumps(restocked),
                    ))
            if state is None:
                state = CexAlertState(title_id=title.id, fingerprint=fingerprint, snapshot_json="{}")
            state.fingerprint = fingerprint
            state.snapshot_json = json.dumps(current)
            session.add(state)
        # Removing/re-adding a wishlist title should start a fresh baseline.
        eligible_ids = {title.id for title in titles}
        for state in session.exec(select(CexAlertState)).all():
            if state.title_id not in eligible_ids:
                session.delete(state)
        session.commit()

    with get_session() as session:
        pending = session.exec(select(CexStockAlert).where(
            CexStockAlert.status == "pending",
        ).order_by(CexStockAlert.id)).all()
        for alert in pending:
            _raise_if_cancelled()
            session.refresh(alert)
            if alert.status != "pending":
                continue
            if get_setting("cex_stock_alerts", "true" if config.CEX_STOCK_ALERTS else "false") != "true":
                return
            title = session.get(Title, alert.title_id)
            product = session.exec(select(CexProduct).where(CexProduct.box_id == alert.box_id)).first()
            valid = title and title.is_4k and not title.purchased and not title.needs_review and product
            match = _best_match(title, [product], wanted_4k=True) if valid else None
            stock = _stock(product) if product else {}
            available = [loc for loc in json.loads(alert.locations_json)
                         if loc in locations and stock.get(loc) is True]
            if (not match or match[1] < AUTO_ACCEPT or not available
                    or product.last_seen < datetime.utcnow() - timedelta(hours=48)
                    or f"cex:{alert.box_id}" in excluded.get(alert.title_id, set())):
                alert.status = "cancelled"
                session.add(alert)
                session.commit()
                continue
            price = f"${product.sell_price:.2f}" if product.sell_price is not None else "Price unavailable"
            text = (f"CeX wishlist: Back in stock\n{title.name}\n{product.name}\n"
                    f"{price} | {', '.join(LOCATIONS[loc] for loc in available)}\n"
                    f"{_box_url(product.box_id, product.category)}")
            # Release SQLite's write lock before contacting WAHA.
            alert.attempts += 1
            session.add(alert)
            session.commit()
            try:
                notify.send_waha(text)
            except notify.WahaDeliveryError as exc:
                alert.error = str(exc)
                session.add(alert)
                session.commit()
                log.warning("CeX WAHA delivery failed: %s", exc)
                break  # Do not hammer a disconnected session; retry on the next refresh.
            alert.status = "sent"
            alert.sent_at = datetime.utcnow()
            alert.error = None
            session.add(alert)
            session.commit()
