"""Single background worker for all scraping so the headless browser is only
used from one thread, plus the periodic schedule."""
import logging
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler

from . import config, fetch, importer, tracker

log = logging.getLogger(__name__)

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scraper")
_lock = threading.Lock()
status = {"running": None, "detail": "", "queue": 0, "last": None, "last_error": None}
_scheduler = BackgroundScheduler(timezone="Australia/Sydney")


def _run(name: str, fn, *args):
    with _lock:
        status["queue"] = max(0, status["queue"] - 1)
        status["running"] = name
        status["detail"] = ""
    try:
        fn(*args, progress=lambda msg: status.__setitem__("detail", msg))
        status["last_error"] = None
    except Exception as e:
        log.error("Job %s failed: %s\n%s", name, e, traceback.format_exc())
        status["last_error"] = f"{name}: {e}"
    finally:
        status["running"] = None
        status["detail"] = ""
        status["last"] = f"{name} finished {datetime.now().strftime('%a %d %b %H:%M')}"
        if status["queue"] == 0:
            # Free the browser's memory between runs.
            fetch.shutdown_browser()


def submit(name: str, fn, *args):
    with _lock:
        status["queue"] += 1
    return _executor.submit(_run, name, fn, *args)


def queue_check_all():
    submit("Checking all prices", tracker.check_all)


def queue_check_title(title_id: int, stores: list[str] | None = None):
    submit("Checking prices", tracker.check_title, title_id, stores)


def queue_import(url: str):
    def job(url, progress):
        result = importer.import_wishlist(url, progress=progress)
        status["last_import"] = result
        tracker.check_all(progress=progress)

    submit("Importing Amazon wishlist", job, url)


def start_scheduler():
    if config.CHECK_INTERVAL_HOURS > 0:
        _scheduler.add_job(queue_check_all, "interval", hours=config.CHECK_INTERVAL_HOURS,
                           id="check_all", replace_existing=True)
        _scheduler.start()


def next_run():
    job = _scheduler.get_job("check_all") if _scheduler.running else None
    return job.next_run_time if job else None


def shutdown():
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
    _executor.shutdown(wait=False, cancel_futures=True)
