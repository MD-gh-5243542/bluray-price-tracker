"""Fuzzy matching between a wishlist title and store product titles."""
import re
import unicodedata

from rapidfuzz import fuzz

AUTO_ACCEPT = 80  # score at/above which a store match is accepted automatically
REVIEW = 62  # score at/above which a match is kept but flagged for review

_4K = re.compile(r"(\b4k|\buhd\b|ultra[\s-]?hd|2160p)", re.I)
_STEEL = re.compile(r"steel\s?book", re.I)
_BOX = re.compile(r"\b(collection|box\s?set|trilogy|quadrilogy|anthology|complete series|seasons?\s*\d|double pack|triple pack|\d-film|film collection)\b", re.I)
_DVD_ONLY = re.compile(r"\bdvd\b", re.I)
_BLURAY = re.compile(r"blu[\s-]?ray|\bbd\b|4k|uhd", re.I)

_NOISE = [
    r"4k\s*ultra\s*hd", r"4k\s*uhd", r"\b\d\s*bd\b", r"ultra\s*hd", r"\buhd\b", r"\b4k\b", r"\b2k\b", r"2160p",
    r"blu[\s-]?ray", r"\bbd\b", r"\bdvd\b", r"digital(\s*(copy|code|hd))?",
    r"region\s*(free|[abc](\s*(&|and|,)?\s*[abc])*)\b", r"\bregion\b",
    r"steel\s?book", r"limited(\s*edition)?", r"collector'?s?(\s*edition)?",
    r"special(\s*edition)?", r"standard(\s*edition)?", r"deluxe(\s*edition)?",
    r"ultimate(\s*edition)?", r"anniversary(\s*edition)?", r"\bedition\b",
    r"combo(\s*pack)?", r"\bpack\b", r"\bset\b", r"\bexclusive\b", r"jb\s*hi[\s-]?fi",
    r"\bnew\b", r"\bsealed\b", r"brand new", r"free (postage|shipping)", r"\bpostage\b",
    r"\baus(tralian?)?\b", r"\bau\b", r"\buk\b", r"\bus\b", r"\bimport\b", r"\bmovie\b",
    r"\bfilm\b", r"\bdisc\b", r"\bdiscs\b", r"\bslipcover\b", r"\bslip\s?cover\b",
    r"\bwith\b", r"\bplus\b", r"\bincl\w*\b", r"\bversion\b", r"\brestored\b",
    r"\bremastered\b", r"\bcut\b(?!\w)", r"\b(19|20)\d\d\b", r"\bvideo\b", r"\barrow\b",
    r"\bumbrella\b", r"\bimprint\b", r"\bvia vision\b",
]
_NOISE_RE = re.compile("|".join(_NOISE), re.I)
_NUM_WORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6",
    "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7", "viii": "8", "ix": "9",
}


def _ascii(s: str) -> str:
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()


def is_4k(text: str) -> bool:
    return bool(_4K.search(text or ""))


def is_steelbook(text: str) -> bool:
    return bool(_STEEL.search(text or ""))


def is_boxset(text: str) -> bool:
    return bool(_BOX.search(text or ""))


def looks_like_dvd_only(text: str) -> bool:
    return bool(_DVD_ONLY.search(text or "")) and not _BLURAY.search(text or "")


def core_title(text: str) -> str:
    """Reduce a product title to the bare film name for comparison."""
    s = _ascii(re.sub(r"[\u2022\u00b7\u2027\u2219]", "-", text or "")).lower()
    s = s.replace("&", " and ")
    # "Batman, The" -> "The Batman"
    s = re.sub(r"^([^,\[\(]+),\s*the\b", r"the \1", s)
    s = _NOISE_RE.sub(" ", s)
    s = re.sub(r"[\[\]\(\)\{\}]", " ", s)
    s = re.sub(r"(?<=\w)['`](?=\w)", "", s)  # carlito's -> carlitos
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    words = [w for w in s.split() if w]
    return " ".join(words)


def search_query(text: str) -> str:
    """A short query suitable for store search boxes."""
    s = core_title(text)
    s = re.sub(r"^the ", "", s)
    return s


def _numbers(core: str) -> set[str]:
    out = set()
    for w in core.split():
        if w.isdigit():
            out.add(w)
        elif w in _NUM_WORDS:
            out.add(_NUM_WORDS[w])
    return out


def score(wanted_name: str, wanted_4k: bool, wanted_edition: str, candidate: str,
          loose: bool = False) -> float:
    """0-100 likelihood that `candidate` is the wanted release."""
    if not candidate:
        return 0.0
    if wanted_4k != is_4k(candidate):
        return 0.0
    if looks_like_dvd_only(candidate):
        return 0.0
    a, b = core_title(wanted_name), core_title(candidate)
    if not a or not b:
        return 0.0
    ts = fuzz.token_set_ratio(a, b)
    so = fuzz.token_sort_ratio(a, b)
    s = (0.8 * ts + 0.2 * so) if loose else (0.55 * ts + 0.45 * so)
    if _numbers(a) != _numbers(b):
        s -= 25
    wanted_full = f"{wanted_name} {wanted_edition or ''}"
    if is_steelbook(wanted_full) != is_steelbook(candidate):
        s -= 15
    if is_boxset(wanted_full) != is_boxset(candidate):
        s -= 25
    return max(0.0, min(100.0, s))


def score_barcode(wanted_name: str, wanted_4k: bool, wanted_edition: str, candidate: str,
                  loose: bool = False) -> float:
    """Score title identity for a barcode hit; the barcode establishes the format."""
    title = re.sub(
        r"\b(?:4k|uhd|2160p|dvd|blu[\s-]?ray|ultra[\s-]?hd)\b", " ", candidate, flags=re.I)
    title += " 4K Ultra HD" if wanted_4k else " Blu-ray"
    return score(wanted_name, wanted_4k, wanted_edition, title, loose=loose)


def parse_price(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)", text.replace("\xa0", " "))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None
