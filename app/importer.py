"""Import an Amazon AU wishlist and match each item to a blu-ray.com release."""
import logging
import re
from dataclasses import dataclass

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from . import bluray, fetch
from .db import Title, get_session, select
from .matching import core_title, is_4k, is_boxset, is_steelbook, parse_price, search_query

log = logging.getLogger(__name__)
AMAZON = "https://www.amazon.com.au"


@dataclass
class WishlistItem:
    asin: str
    title: str
    price: float | None
    byline: str


def _parse_items(html: str) -> list[WishlistItem]:
    s = BeautifulSoup(html, "lxml")
    out = []
    for li in s.select("li[data-itemid]"):
        a = li.select_one("a[id^=itemName_]")
        if not a:
            continue
        m = re.search(r"/dp/([A-Z0-9]{10})", a.get("href", ""))
        if not m:
            continue
        by = li.select_one("[id^=item-byline]")
        out.append(WishlistItem(m.group(1), (a.get("title") or a.get_text()).strip(),
                                parse_price(li.get("data-price") or ""),
                                by.get_text(" ", strip=True) if by else ""))
    return out


def _list_id(url: str) -> str:
    m = re.search(r"/wishlist/(?:ls/)?([A-Z0-9]{10,16})", url)
    if not m:
        raise ValueError("That doesn't look like an Amazon wishlist link")
    return m.group(1)


def fetch_wishlist(url: str) -> list[WishlistItem]:
    list_id = _list_id(url)
    page_url = f"{AMAZON}/hz/wishlist/ls/{list_id}"
    try:
        html = fetch.get(page_url)
    except fetch.Blocked:
        html = fetch.browser_get(page_url, scroll=True)
        return _parse_items(html)
    items = _parse_items(html)
    seen = {i.asin for i in items}
    for _ in range(40):
        m = re.search(r'"showMoreUrl":"([^"]+)"', html)
        if not m:
            break
        more = m.group(1).encode().decode("unicode_escape")
        html = fetch.get(AMAZON + more)
        new = [i for i in _parse_items(html) if i.asin not in seen]
        if not new:
            break
        items += new
        seen.update(i.asin for i in new)
    return items


_EDITION_WORDS = ["steelbook", "limited", "collector", "special", "anniversary", "criterion",
                  "arrow", "deluxe", "ultimate", "mediabook", "exclusive", "slipcover"]


def _edition_affinity(a: str, b: str) -> int:
    a, b = a.lower(), b.lower()
    return sum(1 for w in _EDITION_WORDS if (w in a) == (w in b) and w in a) - sum(
        1 for w in _EDITION_WORDS if (w in a) != (w in b))


_PREFIX_NOISE = re.compile(r"^(?:the walt disney company|walt disney|disney pixar|disney|pixar|"
                           r"marvel studios|marvels?|dc comics|dc|dreamworks)\s+")


def _score_result(product_title: str, want_core: str, r: bluray.SearchResult) -> float:
    cand = re.sub(r"\s*\(\d{4}(-\d{4})?\)\s*$", "", r.title)
    c = core_title(cand)
    s = 0.5 * fuzz.token_set_ratio(want_core, c) + 0.5 * fuzz.token_sort_ratio(want_core, c)
    if is_boxset(product_title) != is_boxset(cand):
        s -= 25
    return s


def _search_candidates(product_title: str, want_4k: bool, country: str) -> list[tuple[float, bluray.SearchResult]]:
    """blu-ray.com quick search is literal and mixes formats; try a few query shapes
    until one gives a strong match."""
    fmt = " 4k" if want_4k else ""
    q = search_query(product_title)
    want_core = core_title(product_title)
    queries = [q]
    stripped = _PREFIX_NOISE.sub("", q)
    if stripped != q:
        queries.append(stripped)
        want_core = _PREFIX_NOISE.sub("", want_core)
    for noise in (" triple pack", " double pack", " 3 film", " 2 film"):
        if noise in stripped:
            queries.append(stripped.split(noise)[0])
    words = q.split()
    if len(words) > 3:
        queries.append(" ".join(words[:3]))
    attempts = []
    for query in dict.fromkeys(queries):
        attempts += [(query + fmt, country), (query + fmt, "all")]
    seen: dict[str, tuple[float, bluray.SearchResult]] = {}
    for query, c in attempts:
        for r in bluray.search(query, c):
            if r.is_4k == want_4k and r.bluray_id not in seen:
                seen[r.bluray_id] = (_score_result(product_title, want_core, r), r)
        if seen and max(s for s, _ in seen.values()) >= 85:
            break
    return sorted(seen.values(), key=lambda x: -x[0])


MIN_SCORE = 70


def find_release(product_title: str, country: str = "AU") -> tuple[bluray.Release | None, bool]:
    """Best blu-ray.com release for a store product title. Returns (release, needs_review)."""
    want_4k = is_4k(product_title)
    scored = _search_candidates(product_title, want_4k, country)
    if not scored:
        return None, True
    top = scored[0][0]
    if top < MIN_SCORE:
        return None, True
    close = [r for s, r in scored if s >= top - 2][:6]
    releases = []
    for r in close:
        try:
            releases.append(bluray.details(r.url))
        except Exception:
            log.exception("blu-ray.com details failed for %s", r.url)
    if not releases:
        return None, True
    releases.sort(key=lambda rel: (
        -_edition_affinity(product_title, rel.full_title),
        # Steelbook mismatch is a strong signal of a different product
        is_steelbook(product_title) != is_steelbook(rel.full_title),
        rel.country != "Australia",
    ))
    best = releases[0]
    ambiguous = len(releases) > 1 and _edition_affinity(product_title, releases[1].full_title) == \
        _edition_affinity(product_title, best.full_title)
    return best, top < 85 or ambiguous


def _display_name(title: str) -> str:
    s = re.sub(r"\[[^\]]*\]", " ", title)
    s = re.sub(r"\((?:[^)]*\b(?:4k|uhd|blu|region|dvd)\b[^)]*)\)", " ", s, flags=re.I)
    s = re.sub(r"\b(?:4k|ultra\s*hd|uhd|blu[\s-]?ray|region\s*free)\b", " ", s, flags=re.I)
    s = re.sub(r"\s*[/+]\s*(?=\s|$)", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -/+:,") or title


def import_wishlist(url: str, progress=None) -> dict:
    items = fetch_wishlist(url)
    added = skipped = skipped_non_4k = 0
    for i, it in enumerate(items, 1):
        if progress:
            progress(f"[{i}/{len(items)}] {it.title}")
        if not is_4k(it.title):
            skipped_non_4k += 1
            continue
        with get_session() as s:
            if s.exec(select(Title).where(Title.amazon_asin == it.asin)).first():
                skipped += 1
                continue
        title = Title(name=_display_name(it.title), is_4k=is_4k(it.title),
                      source="amazon", source_title=it.title, amazon_asin=it.asin, needs_review=True)
        try:
            rel, review = find_release(it.title)
            if rel:
                bluray.apply_release(title, rel)
                title.needs_review = review
        except Exception:
            log.exception("blu-ray.com lookup failed for %s", it.title)
        with get_session() as s:
            s.add(title)
            s.commit()
        added += 1
    return {"found": len(items), "added": added, "skipped": skipped,
            "skipped_non_4k": skipped_non_4k}
