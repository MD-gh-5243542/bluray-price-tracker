import csv
import io
import json
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import bluray, config, jobs, notify
from .db import (STORE_NAMES, STORES, ExcludedUrl, Listing, PricePoint, Title, enabled_stores,
                 get_session, get_setting, init_db, select, set_setting)
from .tracker import shipping_for, total_for

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
HERE = Path(__file__).parent
TZ = ZoneInfo("Australia/Sydney")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    jobs.start_scheduler()
    yield
    jobs.shutdown()


app = FastAPI(title="Blu-ray Price Tracker", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def _local(dt: datetime | None) -> str:
    if not dt:
        return "never"
    return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ).strftime("%d %b %H:%M")


def _money(v) -> str:
    return "—" if v is None else f"${v:,.2f}"


templates.env.filters["local"] = _local
templates.env.filters["money"] = _money
templates.env.globals.update(STORE_NAMES=STORE_NAMES, today=lambda: date.today())


def render(request: Request, name: str, **ctx):
    ctx.setdefault("stores", enabled_stores())
    ctx["status"] = jobs.status
    return templates.TemplateResponse(request, name, ctx)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


# ------------------------------------------------------------------ list ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, sort: str = "name", show: str = "wanted", q: str = ""):
    with get_session() as s:
        stmt = select(Title).where(Title.is_4k == True)  # noqa: E712
        if show == "wanted":
            stmt = stmt.where(Title.purchased == False)  # noqa: E712
        elif show == "purchased":
            stmt = stmt.where(Title.purchased == True)  # noqa: E712
        titles = s.exec(stmt).all()
        if q:
            titles = [t for t in titles if q.lower() in f"{t.name} {t.edition or ''}".lower()]
        listings = {}
        for l in s.exec(select(Listing)).all():
            listings.setdefault(l.title_id, {})[l.store] = l
    keys = {
        "name": lambda t: t.name.lower().removeprefix("the "),
        "price": lambda t: (t.best_price is None, t.best_price or 0),
        "release": lambda t: (t.release_date is None, t.release_date or date.min),
        "added": lambda t: t.created_at,
        "saving": lambda t: -((t.target_price or 0) - (t.best_price or 1e9)),
    }
    titles.sort(key=keys.get(sort, keys["name"]), reverse=sort in ("release", "added"))
    totals = {tid: {st: total_for(l) for st, l in d.items()} for tid, d in listings.items()}
    review_count = sum(1 for t in titles if t.needs_review)
    return render(request, "index.html", titles=titles, listings=listings, totals=totals,
                  sort=sort, show=show, q=q, review_count=review_count, next_run=jobs.next_run())


@app.get("/api/status")
def api_status():
    return JSONResponse({k: v for k, v in jobs.status.items()})


@app.post("/check-all")
def check_all():
    jobs.queue_check_all()
    return back("/")


@app.post("/check-selected")
def check_selected(ids: list[int] = Form([])):
    with get_session() as s:
        valid = [t.id for t in s.exec(select(Title).where(Title.id.in_(ids), Title.is_4k == True))] if ids else []  # noqa: E712
    if valid:
        jobs.queue_check_titles(valid)
    return back("/")


@app.get("/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Title", "Edition", "Format", "Country", "UPC", "Release date", "Best price",
                "Best store", "Best URL", "Lowest ever", "Target", "blu-ray.com"]
               + [STORE_NAMES[s] for s in STORES])
    with get_session() as s:
        for t in s.exec(select(Title)).all():
            ls = {l.store: l for l in s.exec(select(Listing).where(Listing.title_id == t.id))}
            w.writerow([t.name, t.edition or "", "4K" if t.is_4k else "Blu-ray", t.country or "",
                        t.upc or "", t.release_date or "", t.best_price or "",
                        STORE_NAMES.get(t.best_store, ""), t.best_url or "", t.lowest_ever or "",
                        t.target_price or "", t.bluray_url or ""]
                       + [total_for(ls[st]) if st in ls and ls[st].status == "ok" else "" for st in STORES])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=wishlist.csv"})


# ------------------------------------------------------------------- add ---

@app.get("/add", response_class=HTMLResponse)
def add_page(request: Request, q: str = "", country: str = config.BLURAY_COUNTRY, fmt: str = "4k"):
    results, error = [], None
    if q:
        url = bluray.normalise_url(q)
        if url:
            return back(f"/add/preview?url={url}")
        try:
            results = bluray.search(q, country)
            results = [r for r in results if r.is_4k]
        except Exception as e:
            error = f"blu-ray.com search failed: {e}"
    with get_session() as s:
        have = {t.bluray_id for t in s.exec(select(Title)).all() if t.bluray_id}
    return render(request, "add.html", q=q, country=country, fmt=fmt, results=results,
                  error=error, countries=bluray.COUNTRIES, have=have)


@app.get("/add/preview", response_class=HTMLResponse)
def add_preview(request: Request, url: str, title_id: int | None = None):
    try:
        rel = bluray.details(url)
    except Exception as e:
        raise HTTPException(502, f"Couldn't read blu-ray.com: {e}")
    return render(request, "preview.html", rel=rel, title_id=title_id)


@app.post("/add")
def add_title(url: str = Form(...), target_price: str = Form(""), check_now: str = Form("")):
    rel = bluray.details(url)
    if not rel.is_4k:
        raise HTTPException(400, "Only 4K releases can be added to this wishlist")
    with get_session() as s:
        existing = s.exec(select(Title).where(Title.bluray_id == rel.bluray_id)).first()
        if existing:
            return back(f"/title/{existing.id}")
        t = Title(name=rel.name, source="manual")
        bluray.apply_release(t, rel)
        t.target_price = float(target_price) if target_price.strip() else None
        s.add(t)
        s.commit()
        s.refresh(t)
        tid = t.id
    if check_now:
        jobs.queue_check_title(tid)
    return back(f"/title/{tid}")


# ----------------------------------------------------------------- title ---

def _history_svg(points: list[PricePoint], width=720, height=220) -> str:
    if not points:
        return ""
    colours = {"amazon": "#ff9900", "ezydvd": "#e4002b", "zavvi": "#8a5cf6", "ebay": "#0064d2", "jbhifi": "#e31837"}
    xs = [p.checked_at.timestamp() for p in points]
    ys = [p.total for p in points]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    pad = max((y1 - y0) * 0.15, 2)
    y0, y1 = max(0, y0 - pad), y1 + pad
    L, R, T, B = 48, 12, 12, 28

    def px(x):
        return L + (x - x0) / (x1 - x0) * (width - L - R)

    def py(y):
        return T + (1 - (y - y0) / (y1 - y0)) * (height - T - B)

    parts = [f'<svg viewBox="0 0 {width} {height}" class="chart" role="img" aria-label="Price history">']
    for i in range(5):
        v = y0 + (y1 - y0) * i / 4
        yy = py(v)
        parts.append(f'<line x1="{L}" x2="{width - R}" y1="{yy:.1f}" y2="{yy:.1f}" class="grid"/>')
        parts.append(f'<text x="{L - 6}" y="{yy + 4:.1f}" text-anchor="end">${v:.0f}</text>')
    for label_x in (x0, x1):
        d = datetime.utcfromtimestamp(label_x).replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        parts.append(f'<text x="{px(label_x):.1f}" y="{height - 8}" text-anchor="middle">{d:%d %b}</text>')
    by_store: dict[str, list[PricePoint]] = {}
    for p in points:
        by_store.setdefault(p.store, []).append(p)
    for store, pts in by_store.items():
        c = colours.get(store, "#888")
        path = " ".join(f"{px(p.checked_at.timestamp()):.1f},{py(p.total):.1f}" for p in pts)
        if len(pts) > 1:
            parts.append(f'<polyline points="{path}" fill="none" stroke="{c}" stroke-width="2"/>')
        for p in pts:
            parts.append(f'<circle cx="{px(p.checked_at.timestamp()):.1f}" cy="{py(p.total):.1f}" r="3" fill="{c}">'
                         f'<title>{STORE_NAMES[store]} ${p.total:.2f} — {_local(p.checked_at)}</title></circle>')
    parts.append("</svg>")
    return "".join(parts)


@app.get("/title/{tid}", response_class=HTMLResponse)
def title_page(request: Request, tid: int, q: str = ""):
    with get_session() as s:
        t = s.get(Title, tid)
        if not t:
            raise HTTPException(404)
        ls = {l.store: l for l in s.exec(select(Listing).where(Listing.title_id == tid))}
        pts = s.exec(select(PricePoint).where(PricePoint.title_id == tid)
                     .order_by(PricePoint.checked_at)).all()
        excluded = s.exec(select(ExcludedUrl).where(ExcludedUrl.title_id == tid)).all()
    cands = {st: json.loads(l.candidates_json) if l.candidates_json else [] for st, l in ls.items()}
    ship = {st: shipping_for(st, l.price, l.shipping) for st, l in ls.items()}
    totals = {st: total_for(l) for st, l in ls.items()}
    search_results, editions = [], []
    if q:
        try:
            search_results = bluray.search(q, "all")
            search_results = [r for r in search_results if r.is_4k]
        except Exception:
            pass
    return render(request, "title.html", t=t, listings=ls, cands=cands, ship=ship, totals=totals,
                  chart=_history_svg(pts), excluded=excluded, q=q, search_results=search_results,
                  editions=editions)


@app.get("/title/{tid}/editions", response_class=HTMLResponse)
def title_editions(request: Request, tid: int):
    with get_session() as s:
        t = s.get(Title, tid)
    if not t or not t.bluray_url:
        return HTMLResponse("<p class='muted'>No blu-ray.com link yet — search below.</p>")
    try:
        rel = bluray.details(t.bluray_url)
    except Exception as e:
        return HTMLResponse(f"<p class='error'>blu-ray.com lookup failed: {e}</p>")
    return templates.TemplateResponse(request, "_editions.html", {"t": t, "editions": rel.other_editions})


@app.post("/title/{tid}/update")
def title_update(tid: int, target_price: str = Form(""), notes: str = Form(""),
                 search_terms: str = Form(""), upc: str = Form("")):
    upc = upc.strip()
    if upc and (not upc.isdigit() or len(upc) not in (8, 12, 13, 14)):
        raise HTTPException(400, "UPC/EAN must contain 8, 12, 13 or 14 digits")
    with get_session() as s:
        t = s.get(Title, tid)
        if not t:
            raise HTTPException(404)
        t.target_price = float(target_price) if target_price.strip() else None
        t.notes = notes.strip() or None
        t.search_terms = search_terms.strip() or None
        t.upc = upc or None
        t.last_notified_price = None
        s.add(t)
        s.commit()
    return back(f"/title/{tid}")


@app.post("/title/{tid}/purchased")
def title_purchased(tid: int, value: str = Form("1")):
    with get_session() as s:
        t = s.get(Title, tid)
        t.purchased = value == "1"
        s.add(t)
        s.commit()
    return back(f"/title/{tid}")


@app.post("/title/{tid}/confirm")
def title_confirm(tid: int, next: str = Form("")):
    with get_session() as s:
        t = s.get(Title, tid)
        t.needs_review = False
        s.add(t)
        s.commit()
    return back(next or f"/title/{tid}")


@app.post("/title/{tid}/release")
def title_release(tid: int, url: str = Form(...)):
    """Re-link a title to a different blu-ray.com release (the source of truth)."""
    rel = bluray.details(url)
    if not rel.is_4k:
        raise HTTPException(400, "Only 4K releases can be tracked")
    with get_session() as s:
        t = s.get(Title, tid)
        bluray.apply_release(t, rel)
        t.needs_review = False
        t.best_price = t.best_store = t.best_url = None
        t.lowest_ever = None
        # Store matches were for the old release; re-match everything that isn't pinned.
        for l in s.exec(select(Listing).where(Listing.title_id == tid)).all():
            if not l.pinned:
                s.delete(l)
        s.add(t)
        s.commit()
    jobs.queue_check_title(tid)
    return back(f"/title/{tid}")


@app.post("/title/{tid}/check")
def title_check(tid: int):
    jobs.queue_check_title(tid)
    return back(f"/title/{tid}")


@app.post("/title/{tid}/delete")
def title_delete(tid: int):
    with get_session() as s:
        for model in (Listing, PricePoint, ExcludedUrl):
            for row in s.exec(select(model).where(model.title_id == tid)).all():
                s.delete(row)
        t = s.get(Title, tid)
        if t:
            s.delete(t)
        s.commit()
    return back("/")


def _listing(s, tid: int, store: str) -> Listing:
    if store not in STORES:
        raise HTTPException(404)
    l = s.exec(select(Listing).where(Listing.title_id == tid, Listing.store == store)).first()
    if not l:
        l = Listing(title_id=tid, store=store)
    return l


@app.post("/title/{tid}/listing/{store}/reject")
def listing_reject(tid: int, store: str):
    with get_session() as s:
        l = _listing(s, tid, store)
        if l.url:
            s.add(ExcludedUrl(title_id=tid, store=store, url=l.url))
        l.pinned = False
        l.url = None
        l.status = "pending"
        s.add(l)
        s.commit()
    jobs.queue_check_title(tid, [store])
    return back(f"/title/{tid}")


@app.post("/title/{tid}/listing/{store}/accept")
def listing_accept(tid: int, store: str):
    with get_session() as s:
        l = _listing(s, tid, store)
        l.status = "ok"
        l.pinned = store != "ebay"
        s.add(l)
        s.commit()
        from .tracker import update_best
        t = s.get(Title, tid)
        update_best(s, t)
        s.add(t)
        s.commit()
    return back(f"/title/{tid}")


@app.post("/title/{tid}/listing/{store}/pin")
def listing_pin(tid: int, store: str, url: str = Form(...)):
    url = url.strip()
    with get_session() as s:
        l = _listing(s, tid, store)
        l.url = url
        l.pinned = store != "ebay"
        l.status = "pending"
        s.add(l)
        # Un-exclude it if the user explicitly picked it.
        for e in s.exec(select(ExcludedUrl).where(ExcludedUrl.title_id == tid,
                                                  ExcludedUrl.url == url)).all():
            s.delete(e)
        s.commit()
    jobs.queue_check_title(tid, [store])
    return back(f"/title/{tid}")


@app.post("/title/{tid}/listing/{store}/unpin")
def listing_unpin(tid: int, store: str):
    with get_session() as s:
        l = _listing(s, tid, store)
        l.pinned = False
        s.add(l)
        s.commit()
    jobs.queue_check_title(tid, [store])
    return back(f"/title/{tid}")


@app.post("/title/{tid}/excluded/{eid}/delete")
def excluded_delete(tid: int, eid: int):
    with get_session() as s:
        e = s.get(ExcludedUrl, eid)
        if e:
            s.delete(e)
            s.commit()
    return back(f"/title/{tid}")


# ---------------------------------------------------------------- import ---

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return render(request, "import.html", url=get_setting("amazon_wishlist_url", ""),
                  last=jobs.status.get("last_import"))


@app.post("/import")
def import_start(url: str = Form(...)):
    set_setting("amazon_wishlist_url", url.strip())
    jobs.queue_import(url.strip())
    return back("/import")


# -------------------------------------------------------------- settings ---

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, msg: str = ""):
    return render(request, "settings.html", all_stores=STORES, enabled=enabled_stores(),
                  apprise=get_setting("apprise_urls", ""), env_apprise=len(config.APPRISE_URLS),
                  cfg=config, msg=msg, ebay_api=bool(config.EBAY_CLIENT_ID), next_run=jobs.next_run())


@app.post("/settings")
async def settings_save(request: Request):
    form = await request.form()
    chosen = [s for s in STORES if form.get(f"store_{s}")]
    set_setting("enabled_stores", ",".join(chosen))
    set_setting("apprise_urls", str(form.get("apprise_urls", "")).strip())
    return back("/settings?msg=Saved")


@app.post("/settings/test-notify")
def settings_test():
    if not notify.enabled():
        return back("/settings?msg=No notification targets configured")
    ok = notify.send("💿 Blu-ray Price Tracker", "Test notification — alerts are working.")
    return back("/settings?msg=" + ("Test notification sent" if ok else "Notification failed — check the URL(s)"))


@app.get("/healthz")
def healthz():
    return {"ok": True}
