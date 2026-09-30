"""Import owned releases from a public blu-ray.com collection."""
import logging
from urllib.parse import parse_qs, urljoin, urlsplit

from bs4 import BeautifulSoup

from . import bluray, fetch, tracker
from .db import Title, get_session, select

log = logging.getLogger(__name__)
_COLLECTION_PATH = "/community/collection.php"
_ALLOWED_HOSTS = {"blu-ray.com", "www.blu-ray.com"}
_PAGE_PARAMS = {"page", "start", "offset", "from"}


def validate_collection_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    user_id = (params.get("u") or [""])[0]
    if parsed.scheme not in ("http", "https") or parsed.hostname not in _ALLOWED_HOSTS \
            or parsed.path.rstrip("/") != _COLLECTION_PATH or not user_id:
        raise ValueError("Use a public blu-ray.com collection link with a user ID.")
    return user_id, url


def _page_links(soup: BeautifulSoup, current_url: str, user_id: str) -> list[str]:
    current = urlsplit(current_url)
    current_params = parse_qs(current.query)
    links = []
    for anchor in soup.select("a[href]"):
        parsed = urlsplit(urljoin(bluray.BASE, anchor["href"]))
        params = parse_qs(parsed.query)
        if (parsed.hostname not in _ALLOWED_HOSTS or parsed.path != _COLLECTION_PATH
                or (params.get("u") or [""])[0] != user_id):
            continue
        if current_params.get("action") and params.get("action") != current_params["action"]:
            continue
        if any(params.get(key) != current_params.get(key) for key in _PAGE_PARAMS):
            links.append(parsed.geturl())
    return links


def fetch_collection(url: str, progress=None) -> list[bluray.SearchResult]:
    """Read every discoverable page of a public collection, deduplicating releases."""
    user_id, start_url = validate_collection_url(url)
    pending = [start_url]
    visited: set[str] = set()
    releases: dict[str, bluray.SearchResult] = {}

    while pending:
        page_url = pending.pop(0)
        if page_url in visited:
            continue
        if len(visited) >= 100:
            raise ValueError("Collection pagination exceeded 100 pages; import stopped.")
        visited.add(page_url)
        if progress:
            progress(f"Reading blu-ray.com collection page {len(visited)}")
        try:
            page_html = fetch.get(page_url)
        except fetch.Blocked:
            page_html = fetch.browser_get(page_url)
        soup = BeautifulSoup(page_html, "lxml")
        product_links = soup.select("a.hoverlink[data-productid]")
        anchors = product_links or soup.select("a[href*='/movies/']")
        for anchor in anchors:
            href = urljoin(bluray.BASE, anchor.get("href", ""))
            release_id = anchor.get("data-productid") or bluray._id_from_url(href)
            if not release_id or not href.startswith(bluray.BASE + "/movies/"):
                continue
            title = (anchor.get("title") or anchor.get_text(" ", strip=True)).strip()
            if not title:
                continue
            image = anchor.find("img")
            releases.setdefault(release_id, bluray.SearchResult(
                str(release_id), href, title, image.get("src", "") if image else "",
                bluray.is_4k(title),
            ))
        for next_url in _page_links(soup, page_url, user_id):
            if next_url not in visited and next_url not in pending:
                pending.append(next_url)

    if not releases:
        raise ValueError("No collection titles were found. Make sure the blu-ray.com collection is public.")
    return list(releases.values())


def import_collection(url: str, progress=None) -> dict:
    results = fetch_collection(url, progress)
    added = marked_owned = skipped = failed = 0
    for index, result in enumerate(results, 1):
        tracker._raise_if_cancelled()
        if progress:
            progress(f"[{index}/{len(results)}] {result.title}")
        try:
            release = bluray.details(result.url)
        except Exception:
            failed += 1
            log.exception("blu-ray.com collection release details failed for %s", result.url)
            continue
        if not release.bluray_id:
            failed += 1
            continue
        with get_session() as session:
            existing = session.exec(
                select(Title).where(Title.bluray_id == release.bluray_id)).first()
            if existing:
                if existing.purchased:
                    skipped += 1
                else:
                    existing.purchased = True
                    session.add(existing)
                    marked_owned += 1
            else:
                title = Title(source="bluray_collection", source_title=result.title, purchased=True)
                bluray.apply_release(title, release)
                session.add(title)
                added += 1
            session.commit()
    return {
        "found": len(results),
        "added": added,
        "marked_owned": marked_owned,
        "skipped": skipped,
        "failed": failed,
    }
