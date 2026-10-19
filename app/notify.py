import logging
from urllib.parse import urlsplit

import httpx

from . import config
from .db import get_setting

log = logging.getLogger(__name__)
WAHA_TEST_MESSAGE = "Blu-ray Price Tracker: WAHA test notification."


class WahaDeliveryError(Exception):
    """Safe-to-display WAHA failure without response bodies or credentials."""


def waha_config_error() -> str | None:
    try:
        url = urlsplit(config.WAHA_URL)
        port = url.port
    except ValueError:
        return "WAHA_URL is not a valid HTTP(S) base URL."
    if (url.scheme not in ("http", "https") or not url.hostname
            or url.username or url.password or url.query or url.fragment or port == 0):
        return "Set WAHA_URL to an HTTP(S) base URL without credentials or query parameters."
    if not config.WAHA_SESSION or not config.WAHA_CHAT_ID:
        return "Set WAHA_SESSION and WAHA_CHAT_ID in the container environment."
    return None


def send_waha(text: str) -> None:
    if error := waha_config_error():
        raise WahaDeliveryError(error)
    headers = {"X-Api-Key": config.WAHA_API_KEY} if config.WAHA_API_KEY else {}
    try:
        response = httpx.post(
            f"{config.WAHA_URL}/api/sendText",
            headers=headers,
            json={"session": config.WAHA_SESSION, "chatId": config.WAHA_CHAT_ID,
                  "text": text, "linkPreview": False},
            timeout=httpx.Timeout(20.0, connect=5.0),
            follow_redirects=False,
        )
    except httpx.RequestError as exc:
        raise WahaDeliveryError(f"WAHA connection failed ({type(exc).__name__}).") from exc
    if not 200 <= response.status_code < 300:
        raise WahaDeliveryError(f"WAHA returned HTTP {response.status_code}.")


def _urls() -> list[str]:
    urls = list(config.APPRISE_URLS)
    extra = get_setting("apprise_urls", "")
    urls += [u.strip() for u in extra.replace("\n", ",").split(",") if u.strip()]
    return urls


def enabled() -> bool:
    return bool(_urls())


def send(title: str, body: str) -> bool:
    urls = _urls()
    if not urls:
        return False
    import apprise

    ap = apprise.Apprise()
    for u in urls:
        ap.add(u)
    ok = ap.notify(title=title, body=body)
    if not ok:
        log.warning("Notification failed for %d target(s)", len(urls))
    return bool(ok)
