import logging

from . import config
from .db import get_setting

log = logging.getLogger(__name__)


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
