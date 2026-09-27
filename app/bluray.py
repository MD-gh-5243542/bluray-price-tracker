"""blu-ray.com: search and release details. Source of truth for release info."""
import html as htmllib
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from bs4 import BeautifulSoup

from . import config, fetch
from .matching import is_4k

BASE = "https://www.blu-ray.com"
COUNTRIES = {
    "AU": "Australia", "US": "United States", "UK": "United Kingdom", "CA": "Canada",
    "DE": "Germany", "FR": "France", "JP": "Japan", "NZ": "New Zealand", "all": "All countries",
}
_COUNTRY_NAMES = {
    "Australia", "United Kingdom", "Canada", "Germany", "France", "Japan", "New Zealand",
    "Italy", "Spain", "Netherlands", "Sweden", "Norway", "Denmark", "Finland", "Poland",
    "Czech Republic", "Hong Kong", "Taiwan", "South Korea", "Korea", "China", "Thailand",
    "Mexico", "Brazil", "Argentina", "India", "Russia", "Austria", "Switzerland", "Belgium",
    "Portugal", "Greece", "Turkey", "Hungary", "Ireland", "South Africa", "Singapore",
    "Malaysia", "Indonesia", "Philippines", "Israel", "Iceland", "Estonia", "Latvia",
    "Lithuania", "Slovakia", "Slovenia", "Croatia", "Serbia", "Romania", "Bulgaria", "Ukraine",
    "United States",
}


@dataclass
class SearchResult:
    bluray_id: str
    url: str
    title: str  # e.g. "Dune: Part Two 4K (2024)"
    cover: str
    is_4k: bool


@dataclass
class Release:
    bluray_id: str
    url: str
    name: str  # film name incl. 4K marker stripped, e.g. "Dune: Part Two"
    full_title: str  # page title e.g. "Dune: Part Two 4K Blu-ray (JB Hi-Fi Exclusive SteelBook) (Australia)"
    year: str | None = None
    edition: str | None = None
    country: str | None = None
    is_4k: bool = False
    upc: str | None = None
    release_date: date | None = None
    cover_url: str | None = None
    studio: str | None = None
    runtime: str | None = None
    rating: str | None = None
    region: str | None = None
    other_editions: list[SearchResult] = field(default_factory=list)


def _id_from_url(url: str) -> str | None:
    m = re.search(r"/movies/[^/]+/(\d+)/?", url)
    return m.group(1) if m else None


def normalise_url(url_or_id: str) -> str | None:
    s = url_or_id.strip()
    if s.isdigit():
        return f"{BASE}/movies/x/{s}/"
    if "blu-ray.com/movies/" in s:
        return s.split("#")[0]
    return None


def search(keyword: str, country: str | None = None) -> list[SearchResult]:
    country = country or config.BLURAY_COUNTRY
    params = {
        "quicksearch": "1",
        "quicksearch_country": "all" if country == "all" else country,
        "quicksearch_keyword": keyword,
        "section": "bluraymovies",
    }
    html = fetch.get(f"{BASE}/search/", params=params)
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.select("a.hoverlink[data-productid]"):
        href = a.get("href", "")
        bid = a.get("data-productid")
        if "/movies/" not in href or bid in seen:
            continue
        seen.add(bid)
        img = a.find("img")
        title = a.get("title", "").strip()
        out.append(SearchResult(bid, href, title, img.get("src") if img else "", is_4k(title)))
    return out


def _parse_date(s: str) -> date | None:
    for fmt in ("%B %d, %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def details(url: str) -> Release:
    html = fetch.get(url)
    soup = BeautifulSoup(html, "lxml")
    page_title = htmllib.unescape(soup.title.get_text(strip=True)) if soup.title else ""
    canonical = soup.find("link", rel="canonical")
    real_url = canonical["href"] if canonical and canonical.get("href") else url
    bid = _id_from_url(real_url) or _id_from_url(url) or ""

    info = soup.select_one("#movie_info")
    h3 = info.find("h3").get_text(strip=True) if info and info.find("h3") else page_title
    four_k = bool(re.search(r"\b4K\b", h3)) or " 4K Blu-ray" in page_title
    name = re.sub(r"\s*4K$", "", h3).strip()

    # "(JB Hi-Fi Exclusive SteelBook) (Australia)" -> edition, country.
    # US releases have no country suffix on blu-ray.com.
    parens = re.findall(r"\(([^()]*)\)", page_title.split("Blu-ray", 1)[-1])
    if parens and parens[-1] in _COUNTRY_NAMES:
        country, parens = parens[-1], parens[:-1]
    else:
        country = "United States"
    edition = " / ".join(p for p in parens if p.lower() not in ("4k ultra hd", "blu-ray")) or None

    year = None
    if info:
        m = re.search(r"\((\d{4}(?:-\d{4})?)\)", info.get_text(" ", strip=True)[:300])
        year = m.group(1) if m else None

    upc = None
    m = re.search(r"ebay\.com[^\"']*[?&]_nkw=(\d{8,14})", html)
    if not m:
        m = re.search(r"amazon\.[^\"']*[?&]k=(\d{8,14})", html)
    if not m:
        m = re.search(r"(?:UPC|EAN)[^0-9]{0,40}(\d{12,13})", html)
    if m:
        upc = m.group(1)

    rel = None
    m = re.search(r"Blu-ray Release Date ([A-Z][a-z]+ \d{1,2}, \d{4})", html)
    if m:
        rel = _parse_date(m.group(1))

    og = soup.find("meta", property="og:image")
    cover = og["content"] if og and og.get("content") else None

    runtime = None
    rt = soup.find(id="runtime")
    if rt:
        runtime = rt.get_text(strip=True)
    rating = None
    m = re.search(r"Rated\s+([^<|]{1,30}?)\s*(?:\||<)", html)
    if m:
        rating = m.group(1).strip()

    studio = None
    m = re.search(r'link\.php\?url=https://www\.blu-ray\.com/[^/"]+/\d+/"[^>]*>([^<]+)</a>', html)
    if m:
        studio = htmllib.unescape(m.group(1)).strip()

    region = None
    m = re.search(r"(Region (?:free|[ABC]\b)[^<]{0,30})", html)
    if m:
        region = htmllib.unescape(m.group(1)).strip()

    others = []
    for a in soup.select("#movie_news a.hoverlink[data-productid], a.hoverlink[data-productid]"):
        href = a.get("href", "")
        pid = a.get("data-productid")
        if pid == bid or "/movies/" not in href or any(o.bluray_id == pid for o in others):
            continue
        img = a.find("img")
        t = a.get("title", "").strip()
        others.append(SearchResult(pid, href, t, img.get("src") if img else "", is_4k(t)))

    return Release(
        bluray_id=bid, url=real_url, name=name, full_title=page_title, year=year,
        edition=edition, country=country, is_4k=four_k, upc=upc, release_date=rel,
        cover_url=cover, studio=studio, runtime=runtime, rating=rating, region=region,
        other_editions=[edition for edition in others if edition.is_4k][:40],
    )


def apply_release(title, rel: Release) -> None:
    """Copy blu-ray.com release info onto a Title (source of truth)."""
    title.bluray_id = rel.bluray_id
    title.bluray_url = rel.url
    title.name = rel.name
    title.year = rel.year
    title.edition = rel.edition
    title.country = rel.country
    title.is_4k = rel.is_4k
    title.upc = rel.upc
    title.release_date = rel.release_date
    title.cover_url = rel.cover_url
    title.studio = rel.studio
    title.runtime = rel.runtime
    title.rating = rel.rating
    title.region = rel.region
