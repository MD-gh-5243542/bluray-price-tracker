import re
from dataclasses import dataclass

from ..matching import score as _score
from ..matching import score_barcode as _score_barcode


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


MATCH_MODES = {
    "any": "Any 4K release",
    "exact": "Exact edition",
    "selected": "Selected editions",
}


def parse_upcs(text: str | None) -> list[str]:
    return [u for u in re.split(r"[\s,;]+", text or "") if u]


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
        mode = getattr(title, "match_mode", None) or "any"
        self.mode: str = mode if mode in MATCH_MODES else "any"
        upcs = [title.upc] if title.upc else []
        if self.mode != "exact":
            upcs += parse_upcs(getattr(title, "alt_upcs", None))
        self.upcs: list[str] = list(dict.fromkeys(upcs))[:4]

    @property
    def any_edition(self) -> bool:
        return self.mode == "any"

    @property
    def title_search_too(self) -> bool:
        """Barcode stores also search by title when any edition is acceptable."""
        return self.any_edition or not self.upcs

    def score(self, candidate: str, loose: bool = False) -> float:
        return _score(self.name, self.is_4k, self.edition, candidate, loose=loose,
                      strict_edition=not self.any_edition, wanted_year=self.year)

    def score_barcode(self, candidate: str, loose: bool = True) -> float:
        # The barcode proves identity; listings may mention a reissue year, so skip the year check.
        return _score_barcode(self.name, self.is_4k, self.edition, candidate, loose=loose,
                              strict_edition=False)
