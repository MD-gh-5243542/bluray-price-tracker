# 💿 Blu-ray Price Tracker

A self-hosted wishlist for 4K Ultra HD Blu-rays. It tracks the cheapest **delivered** price across:

| Store | How |
|---|---|
| Amazon AU | product page + barcode search |
| EzyDVD | site search |
| eBay AU | Buy It Now, new, AU-located (headless browser, or the eBay Browse API if keys are set) |
| JB Hi-Fi AU | site search + product JSON-LD |
| Umbrella Entertainment | Shopify product search + variant data |
| DVD Hub | Shopify product search + variant data |
| Sanity | Unbxd product search + JSON-LD product data |
| Rarewaves | Shopify product search + variant data |

**[blu-ray.com](https://www.blu-ray.com) is the source of truth** for each title: edition, country, barcode, release date and cover art. Only 4K releases are imported, added, and tracked. Store listings are matched against that release using the barcode where possible, then by fuzzy title matching with 4K, SteelBook and box-set guards.

## Features
- **Import** your Amazon AU wishlist (public/shared link). Non-4K items are skipped; 4K items are matched to a blu-ray.com release, and uncertain matches are flagged for you to confirm.
- **Add titles** by searching blu-ray.com or pasting a 4K blu-ray.com URL. Pick the exact edition you want (e.g. the JB Hi-Fi SteelBook vs the standard release).
- Prices are checked automatically every 12 h (configurable), and you can check manually: every title, only the titles you tick on the wishlist (“Check selected”), or one title (“Check now”).
- Per-store controls: “Wrong match” (exclude and re-search), pick another result, or pin an exact product URL.
- Each title has an edition match mode (title page → Tracking options):
  - **Any 4K release** (default): the cheapest 4K disc of the film from any edition. Stores are searched by title, and by UPC where supported.
  - **Exact edition**: only the title's main UPC/EAN counts. Amazon and eBay search by barcode only, and title-matched steelbooks and collector's editions are penalised.
  - **Selected editions**: the main UPC/EAN plus any additional UPC/EANs you list.
  - Changing the mode or the UPCs re-matches the stores; pinned products are kept.
  - Listings found by barcode show a "UPC match" badge.
  - Store titles that name a different year (e.g. *Point Break (2015)* when tracking the 1991 film) are rejected.
- Amazon verifies the 4K variant on the product page before saving its price.
- A running price check can be cancelled from the header. Queued checks are dropped immediately; the running check stops after the store it is currently checking, and prices already fetched are kept.
- Store prices show item price plus nonzero postage in parentheses; “Best” remains the delivered total. Price-history chart, lowest-ever price and target price.
- **Alerts** via [Apprise](https://github.com/caronc/apprise/wiki) (ntfy, Discord, Telegram, email, Teams, Pushover…) when a title hits your target or a new low.
- CSV export.
- **Bargains** page showing the ten cheapest in-stock 4K titles discovered in each enabled retailer catalogue (not limited to the wishlist).

## Run with Docker
```bash
git clone <this repo> bluray-price-tracker && cd bluray-price-tracker
docker compose up -d --build
```
Open `http://<docker-host>:8000`, go to **Import**, and paste your wishlist link.

Data (SQLite) lives in `./data`, so back that folder up.

> There's no login. Keep it on your LAN, or put it behind your reverse proxy with authentication.

### Configuration (environment variables)
| Variable | Default | |
|---|---|---|
| `CHECK_INTERVAL_HOURS` | `12` | `0` disables scheduled checks |
| `REQUEST_DELAY` | `2.5` | seconds between requests to the same site |
| `APPRISE_URLS` | – | comma-separated; can also be set in Settings |
| `BASE_URL` | – | e.g. `http://nas:8000`, used for links in alerts |
| `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` | – | optional [eBay Browse API](https://developer.ebay.com) keys |
| `EBAY_INCLUDE_USED` | `false` | include used listings |
| `SHIPPING_EZYDVD` / `FREE_SHIPPING_OVER_EZYDVD` | `6.95` / `80` | delivery estimate |
| `SHIPPING_AMAZON` | `0` | Prime / free over $59 |
| `SHIPPING_JBHIFI` / `FREE_SHIPPING_OVER_JBHIFI` | `6.95` / `99` | configurable delivery estimate |
| `SHIPPING_UMBRELLA` / `FREE_SHIPPING_OVER_UMBRELLA` | `9.95` / `120` | `$9.95` is an estimate; postage varies by weight/location; free AU shipping over $120 |
| `SHIPPING_DVDHUB` / `FREE_SHIPPING_OVER_DVDHUB` | `7.95` / `0` | standard tracked postage |
| `SHIPPING_SANITY` / `SHIPPING_SANITY_MULTI` / `FREE_SHIPPING_OVER_SANITY` | `3.50` / `9.95` / `120` | $3.50 single-item, $9.95 multiple-item postage; free over $120. Per-title estimates use the single-item rate. |
| `SHIPPING_RAREWAVES` / `FREE_SHIPPING_OVER_RAREWAVES` | `9.95` / `0` | estimate; actual international postage varies |
| `BLURAY_COUNTRY` | `AU` | default country when searching blu-ray.com |
| `USE_BROWSER` | `true` | headless Chromium (needed for eBay scraping) |

## Local development
```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium
DATA_DIR=./data .venv/bin/uvicorn app.main:app --reload
```

## Notes
- This app scrapes public pages at a gentle rate for personal use. Sites change their HTML, so if a store starts showing errors, its parser in `app/stores/` probably needs a tweak.
- Only in-stock, confirmed matches count toward the “cheapest” price. Uncertain matches are shown with a dotted underline until you accept them.
- Legacy non-4K records are preserved in the database but are hidden from the wishlist and excluded from price checks.
