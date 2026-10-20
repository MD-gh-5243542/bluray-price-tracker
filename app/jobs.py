"""Single background worker for all scraping so the headless browser is only
used from one thread, plus the periodic schedule."""
import logging
import threading
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import cex, collection_importer, config, fetch, importer, tracker

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scraper")
_lock = threading.Lock()
_pending: set[Future] = set()
_cex_lock = threading.Lock()
_cex_future: Future | None = None
status = {"running": None, "detail": "", "queue": 0, "last": None, "last_error": None,
          "cancelling": False}
_scheduler = BackgroundScheduler(timezone="Australia/Sydney")


def _run(name: str, fn, *args, **kwargs):
    with _lock:
        status["queue"] = max(0, status["queue"] - 1)
        status["running"] = name
        status["detail"] = ""
        status["cancelling"] = False
        tracker.cancel_event.clear()
    outcome = "finished"
    try:
        fn(*args, progress=lambda msg: status.__setitem__("detail", msg), **kwargs)
        status["last_error"] = None
    except tracker.Cancelled:
        outcome = "cancelled"
        log.info("Job %s cancelled", name)
    except Exception as e:
        log.error("Job %s failed: %s\n%s", name, e, traceback.format_exc())
        status["last_error"] = f"{name}: {e}"
    finally:
        status["running"] = None
        status["detail"] = ""
        status["cancelling"] = False
        tracker.cancel_event.clear()
        status["last"] = f"{name} {outcome} {datetime.now().strftime('%a %d %b %H:%M')}"
        if status["queue"] == 0:
            # Free the browser's memory between runs.
            fetch.shutdown_browser()


def submit(name: str, fn, *args, **kwargs):
    with _lock:
        status["queue"] += 1
        future = _executor.submit(_run, name, fn, *args, **kwargs)
        _pending.add(future)
    future.add_done_callback(_pending.discard)
    return future


def cancel() -> bool:
    """Drop queued jobs and stop the running one before its next store/request."""
    with _lock:
        dropped = sum(1 for future in list(_pending) if future.cancel())
        status["queue"] = max(0, status["queue"] - dropped)
        running = status["running"] is not None
        if running:
            status["cancelling"] = True
            tracker.cancel_event.set()
    return running or dropped > 0


def queue_check_all():
    submit("Checking all prices", tracker.check_all)


def queue_daily_refresh():
    submit("Daily price refresh", tracker.check_all)


def queue_cex_refresh(*, full: bool = True):
    label = "Refreshing CeX stock" if full else "Refreshing CeX stock (hourly)"
    return submit(label, cex.sync, include_favourites=full, include_buyback=full)


def queue_cex_refresh_scheduled():
    global _cex_future
    with _cex_lock:
        if _cex_future is not None and not _cex_future.done():
            log.info("CeX refresh already running or queued; skipping duplicate")
            return _cex_future
        _cex_future = queue_cex_refresh(full=False)
        return _cex_future


def queue_cex_refresh_full_daily():
    queue_cex_refresh(full=True)


def queue_check_title(title_id: int, stores: list[str] | None = None):
    submit("Checking prices", tracker.check_title, title_id, stores)


def queue_check_titles(ids: list[int]):
    submit(f"Checking {len(ids)} selected title{'s' if len(ids) != 1 else ''}", tracker.check_titles, ids)


def queue_import(url: str):
    def job(url, progress):
        result = importer.import_wishlist(url, progress=progress)
        status["last_import"] = result
        tracker.check_all(progress=progress)

    submit("Importing Amazon wishlist", job, url)


def queue_collection_import(url: str):
    def job(url, progress):
        result = collection_importer.import_collection(url, progress=progress)
        status["last_collection_import"] = result

    submit("Importing blu-ray.com collection", job, url)


def start_scheduler():
    if config.SCHEDULED_CHECKS:
        _scheduler.add_job(queue_daily_refresh, "cron", hour=5, minute=0,
                           id="daily_refresh", replace_existing=True)
        _scheduler.add_job(queue_cex_refresh_scheduled, "cron", minute=5, second=0,
                           id="cex_refresh", replace_existing=True,
                           coalesce=True, misfire_grace_time=300)
        _scheduler.add_job(queue_cex_refresh_full_daily, "cron", hour=5, minute=35,
                           id="cex_refresh_full_daily", replace_existing=True,
                           coalesce=True, misfire_grace_time=900)
        _scheduler.start()


def next_run(job_id: str = "daily_refresh"):
    job = _scheduler.get_job(job_id) if _scheduler.running else None
    return job.next_run_time if job else None


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
    _executor.shutdown(wait=False, cancel_futures=True)
