"""Rarewaves international Shopify store."""
from ..matching import is_4k, score, search_query
from . import shopify
from .base import Offer, Query

BASE = "https://www.rarewaves.com"
STORE = "rarewaves"


def search(q: Query) -> list[Offer]:
    text = q.search_terms or search_query(q.name)
    if q.is_4k and not is_4k(text):
        text += " 4K"
    offers = shopify.search(BASE, STORE, text)
    for offer in offers:
        offer.score = score(q.name, q.is_4k, q.edition, offer.title)
    return offers


def refresh(url: str) -> Offer | None:
    return shopify.refresh(BASE, STORE, url)


def bargains() -> list[Offer]:
    return shopify.bargains(BASE, STORE)
