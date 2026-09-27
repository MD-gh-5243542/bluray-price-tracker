"""HTTP + headless browser fetching with per-host politeness delays.

All scraping jobs run on a single worker thread (see jobs.py) so the
Playwright sync API is only ever touched from one thread.
"""
import logging
import re
import threading
import time
from urllib.parse import urlparse

import httpx
from playwright.sync_api import Error as PlaywrightError

from . import config

log = logging.getLogger(__name__)


class Blocked(Exception):
    """The site refused the request (bot protection, captcha, 403/503)."""


_last_hit: dict[str, float] = {}
_lock = threading.Lock()

_client = httpx.Client(
    headers={
        "User-Agent": config.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    },
    follow_redirects=True,
    timeout=30,
)


def _throttle(url: str) -> None:
    host = urlparse(url).netloc
    with _lock:
        now = time.time()
        slot = max(now, _last_hit.get(host, 0) + config.REQUEST_DELAY)
        _last_hit[host] = slot
    if slot > now:
        time.sleep(slot - now)


def _looks_blocked(url: str, text: str) -> bool:
    host = urlparse(url).netloc
    if "amazon." in host:
        return "captcha" in text.lower() and "productTitle" not in text and "wishlist" not in text.lower()
    return False


def get(url: str, params: dict | None = None) -> str:
    _throttle(url)
    r = _client.get(url, params=params)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"{r.status_code} from {urlparse(url).netloc}")
    r.raise_for_status()
    _fix_encoding(r)
    if _looks_blocked(str(r.url), r.text):
        raise Blocked(f"captcha from {urlparse(url).netloc}")
    return r.text


_META_CHARSET = re.compile(rb"<meta[^>]+charset=[\"']?([\w-]+)", re.I)


def _fix_encoding(r: httpx.Response) -> None:
    """Honour <meta charset> when the HTTP header has none (httpx would assume UTF-8)."""
    if "charset" in r.headers.get("content-type", "").lower():
        return
    m = _META_CHARSET.search(r.content[:4096])
    if m:
        enc = m.group(1).decode().lower()
        r.encoding = "cp1252" if enc in ("iso-8859-1", "latin-1", "latin1") else enc


def get_json(url: str, **kw) -> dict:
    _throttle(url)
    r = _client.get(url, **kw)
    r.raise_for_status()
    return r.json()


def post(url: str, **kw) -> httpx.Response:
    return _client.post(url, **kw)


# ---------------------------------------------------------------- browser ---

_tl = threading.local()


def _context():
    if not config.USE_BROWSER:
        raise Blocked("headless browser disabled (USE_BROWSER=false)")
    b = getattr(_tl, "browser", None)
    if b is None or not b.is_connected():
        from playwright.sync_api import sync_playwright

        _tl.pw = sync_playwright().start()
        _tl.browser = _tl.pw.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
        _tl.ctx = None
        _tl.warmed = set()
    if getattr(_tl, "ctx", None) is None:
        _tl.ctx = _tl.browser.new_context(
            user_agent=config.USER_AGENT,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            viewport={"width": 1366, "height": 900},
        )
        _tl.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        _tl.warmed = set()
    return _tl.ctx


class _PageHandle:
    """Context-manager-ish wrapper so callers can .close() like a context."""

    def __init__(self, page):
        self.page = page

    def close(self):
        try:
            self.page.close()
        except Exception:
            pass


def browser_page(url: str, wait_selector: str | None = None, scroll: bool = False,
                 timeout_ms: int = 30000, block_resources: set[str] | None = None):
    """Open url in the shared browser context and return (handle, page).

    The first visit to each site loads its home page to pick up cookies, which
    avoids bot-protection error pages (notably on eBay)."""
    ctx = _context()
    page = ctx.new_page()
    if block_resources:
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in block_resources else route.continue_())
    parsed = urlparse(url)
    host = parsed.netloc
    if host not in _tl.warmed:
        _throttle(url)
        try:
            page.goto(f"{parsed.scheme}://{host}/", wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(1500)
        except Exception:
            pass
        _tl.warmed.add(host)
    _throttle(url)
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    if wait_selector:
        try:
            page.wait_for_selector(wait_selector, timeout=15000)
        except Exception:
            pass
    else:
        page.wait_for_timeout(2500)
    if scroll:
        for _ in range(40):
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(700)
            if page.query_selector("#endOfListMarker"):
                break
    return _PageHandle(page), page


def browser_get(url: str, wait_selector: str | None = None, scroll: bool = False,
                block_resources: set[str] | None = None) -> str:
    for attempt in range(2):
        handle, page = browser_page(url, wait_selector, scroll, block_resources=block_resources)
        try:
            return page.content()
        except PlaywrightError as e:
            if attempt or "Target crashed" not in str(e):
                raise
            log.warning("Browser page crashed while reading %s; restarting browser and retrying", urlparse(url).netloc)
            shutdown_browser()
        finally:
            handle.close()
    raise RuntimeError("Browser page crashed twice while reading content")


def smart_get(url: str, params: dict | None = None, wait_selector: str | None = None) -> str:
    """Plain HTTP first, falling back to the headless browser if blocked."""
    try:
        return get(url, params)
    except Blocked as e:
        log.info("Falling back to browser for %s (%s)", url, e)
        if params:
            url = str(httpx.URL(url, params=params))
        return browser_get(url, wait_selector)


def shutdown_browser() -> None:
    b = getattr(_tl, "browser", None)
    if b is not None:
        try:
            if getattr(_tl, "ctx", None) is not None:
                _tl.ctx.close()
            b.close()
            _tl.pw.stop()
        except Exception:
            pass
        _tl.browser = None
        _tl.ctx = None
