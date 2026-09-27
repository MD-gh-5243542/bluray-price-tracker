from dataclasses import dataclass


@dataclass
class Offer:
    store: str
    url: str
    title: str
    price: float | None
    shipping: float | None = None  # None = use store default
    in_stock: bool = True
    image: str | None = None
    condition: str | None = None
    score: float = 0.0
    by_barcode: bool = False


class Query:
    """What we're looking for at a store (derived from a Title)."""

    def __init__(self, title):
        self.name: str = title.name
        self.is_4k: bool = title.is_4k
        self.edition: str = title.edition or ""
        self.upc: str | None = title.upc
        self.asin: str | None = title.amazon_asin
        self.search_terms: str | None = title.search_terms
        self.year: str | None = title.year
