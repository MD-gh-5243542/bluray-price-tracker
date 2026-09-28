"""Shared Shopify product search and variant price parsing."""
from urllib.parse import urljoin, urlsplit

from .. import fetch
from ..matching import is_4k, parse_price
from .base import Offer


def search(base: str, store: str, query: str, limit: int = 10) -> list[Offer]:
    data = fetch.get_json(
        urljoin(base, "/search/suggest.json"),
        params={
            "q": query,
            "resources[type]": "product",
            "resources[limit]": str(limit),
            "resources[options][unavailable_products]": "hide",
        },
    )
    products = (data.get("resources", {}).get("results", {}).get("products", []))
    out = []
    for product in products:
        title = str(product.get("title") or "")
        metadata = " ".join([str(product.get("type") or ""), *map(str, product.get("tags") or [])])
        if is_4k(metadata) and not is_4k(title):
            title += " [4K UHD]"
        path = str(product.get("url") or "")
        if not path:
            continue
        image = product.get("image") or product.get("featured_image")
        if isinstance(image, dict):
            image = image.get("url")
        out.append(Offer(
            store, urljoin(base, path.split("?", 1)[0]), title,
            parse_price(str(product.get("price") or "")),
            in_stock=bool(product.get("available")),
            image=urljoin(base, image) if image else None,
        ))
    return out


def refresh(base: str, store: str, url: str) -> Offer | None:
    parsed = urlsplit(url)
    path = parsed.path.rstrip("/")
    if not path.startswith("/products/"):
        return None
    product_url = urljoin(base, f"{path}.js")
    product = fetch.get_json(product_url)
    variants = product.get("variants") or []
    if not variants:
        return None
    available = [variant for variant in variants if variant.get("available")]
    pool = available or variants
    variant = min(pool, key=lambda v: int(v.get("price") or 0))
    title = str(product.get("title") or "")
    metadata = " ".join([str(product.get("type") or ""), *map(str, product.get("tags") or [])])
    if is_4k(metadata) and not is_4k(title):
        title += " [4K UHD]"
    variant_title = str(variant.get("title") or "")
    if variant_title and variant_title.lower() != "default title" and variant_title.lower() not in title.lower():
        title += f" [{variant_title}]"
    cents = int(variant.get("price") or 0)
    image = product.get("featured_image")
    if not image:
        images = product.get("images") or []
        image = images[0] if images else None
    variant_id = variant.get("id")
    offer_url = f"{urljoin(base, path)}?variant={variant_id}" if variant_id else urljoin(base, path)
    return Offer(
        store, offer_url, title, round(cents / 100, 2),
        in_stock=bool(available),
        image=urljoin(base, image) if image else None,
        by_barcode=bool(variant.get("barcode")),
    )
