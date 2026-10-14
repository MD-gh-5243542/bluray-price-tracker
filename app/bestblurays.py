"""On-demand UHD tier lookup; edition selection is always left to the user."""
from __future__ import annotations

import hashlib
import re
from typing import TYPE_CHECKING

from bs4 import BeautifulSoup

from . import fetch
from .matching import core_title, year_conflict

if TYPE_CHECKING:
    from .bluray import Release
    from .db import Title

BASE = "https://www.bestblurays.com"
_FILM_PATH = re.compile(r"^/film/\d+-[^?#/]+$")


class LookupError(ValueError):
    """The response could not be safely interpreted as UHD search results."""


def release_key(title: Title | Release) -> tuple:
    return (title.bluray_id, title.name, title.year, title.edition, title.country,
            title.upc, title.studio, title.is_4k)


def clear(title: Title) -> None:
    title.uhd_tier = None
    title.uhd_release = None
    title.uhd_source_url = None
    title.uhd_checked_at = None
    title.uhd_candidates_json = None
    title.uhd_error = None


def parse_page(html: str, name: str, year: str | None) -> tuple[list[dict[str, str]], int, int]:
    soup = BeautifulSoup(html, "lxml")
    main = soup.select_one("main")
    heading = main.select_one("h1") if main else None
    total = re.search(r"\bTotal:\s*([\d,]+)\s+films?\b",
                      main.get_text(" ", strip=True) if main else "")
    if not heading or heading.get_text(strip=True) != "UHD Tier Lists" or not total:
        raise LookupError("BestBluRays returned an unexpected page; no saved tier was changed.")
    cards = main.select(".card-hover")
    count = int(total.group(1).replace(",", ""))
    if count and not cards:
        raise LookupError("BestBluRays results could not be read; no saved tier was changed.")
    candidates = {}
    for card in cards:
        link = next((a for a in card.select('a[href^="/film/"]')
                     if a.get_text(strip=True)), None)
        if not link or not _FILM_PATH.fullmatch(link["href"]):
            raise LookupError("BestBluRays film links have changed; please try again later.")
        film = link.get_text(" ", strip=True)
        identity = re.fullmatch(r"(.+)\s+\((\d{4})\)", film)
        if (not identity or core_title(identity.group(1)) != core_title(name)
                or year_conflict(year, film)):
            continue
        lines = card.select(".uhd-tier-line")
        if not lines:
            raise LookupError("The matching film has no readable UHD tier entries.")
        for line in lines:
            badge = line.select_one(".mantine-Badge-label")
            if not badge:
                raise LookupError("BestBluRays tier labels have changed; please try again later.")
            tier = badge.get_text(" ", strip=True)
            text = line.get_text(" ", strip=True)
            description = text.removeprefix(tier).strip()
            if not tier or not description:
                raise LookupError("BestBluRays returned an incomplete UHD tier entry.")
            url = BASE + link["href"]
            key = hashlib.sha256(f"{url}\n{tier}\n{description}".encode()).hexdigest()
            candidates[key] = dict(key=key, film=film, tier=tier, release=description, url=url)
    return list(candidates.values()), count, len(cards)


def lookup(name: str, year: str | None) -> list[dict[str, str]]:
    seen = 0
    matches = {}
    for page in range(1, 51):
        html = fetch.get(BASE + "/uhd", params={"title": core_title(name) or name, "page": page})
        candidates, total, count = parse_page(html, name, year)
        matches.update((c["key"], c) for c in candidates)
        seen += count
        if seen >= total:
            return list(matches.values())
        if not count:
            raise LookupError("BestBluRays pagination stopped before all results could be read.")
    raise LookupError("Too many BestBluRays results; no saved tier was changed.")
