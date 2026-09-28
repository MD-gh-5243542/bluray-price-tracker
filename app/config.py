import os
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "tracker.db"

# How often to re-check every title (hours).
CHECK_INTERVAL_HOURS = float(os.getenv("CHECK_INTERVAL_HOURS", "12"))
# Seconds to wait between requests to the same site (be polite, avoid bans).
REQUEST_DELAY = float(os.getenv("REQUEST_DELAY", "2.5"))
# Set false to disable the headless browser (eBay + Amazon fallback need it).
USE_BROWSER = _bool("USE_BROWSER", True)

# Apprise notification URLs, comma separated, e.g.
# "ntfy://ntfy.sh/my-topic,discord://id/token,mailto://user:pass@gmail.com"
APPRISE_URLS = [u.strip() for u in os.getenv("APPRISE_URLS", "").split(",") if u.strip()]
# Public base URL of this app, used for links in notifications.
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")

# Optional eBay Browse API credentials (https://developer.ebay.com). If not set,
# eBay is scraped with the headless browser.
EBAY_CLIENT_ID = os.getenv("EBAY_CLIENT_ID", "")
EBAY_CLIENT_SECRET = os.getenv("EBAY_CLIENT_SECRET", "")
# Include used eBay listings?
EBAY_INCLUDE_USED = _bool("EBAY_INCLUDE_USED", False)

# Flat shipping estimates (AUD) added to retailer prices when comparing.
# Free-shipping thresholds (0 = none) are applied per item, e.g. EzyDVD is
# free over $80.
SHIPPING = {
    "amazon": float(os.getenv("SHIPPING_AMAZON", "0")),
    "ezydvd": float(os.getenv("SHIPPING_EZYDVD", "6.95")),
    "jbhifi": float(os.getenv("SHIPPING_JBHIFI", "6.95")),
    "umbrella": float(os.getenv("SHIPPING_UMBRELLA", "9.95")),
    "dvdhub": float(os.getenv("SHIPPING_DVDHUB", "7.95")),
    "sanity": float(os.getenv("SHIPPING_SANITY", "6.95")),
    "rarewaves": float(os.getenv("SHIPPING_RAREWAVES", "9.95")),
}
FREE_SHIPPING_OVER = {
    "amazon": float(os.getenv("FREE_SHIPPING_OVER_AMAZON", "0")),
    "ezydvd": float(os.getenv("FREE_SHIPPING_OVER_EZYDVD", "80")),
    "jbhifi": float(os.getenv("FREE_SHIPPING_OVER_JBHIFI", "99")),
    "umbrella": float(os.getenv("FREE_SHIPPING_OVER_UMBRELLA", "120")),
    "dvdhub": float(os.getenv("FREE_SHIPPING_OVER_DVDHUB", "0")),
    "sanity": float(os.getenv("FREE_SHIPPING_OVER_SANITY", "120")),
    "rarewaves": float(os.getenv("FREE_SHIPPING_OVER_RAREWAVES", "0")),
}

# Default blu-ray.com country used when searching for new titles.
BLURAY_COUNTRY = os.getenv("BLURAY_COUNTRY", "AU")

USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36",
)
