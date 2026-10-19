import unittest
from concurrent.futures import Future
from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler

from app import config, jobs


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = BackgroundScheduler(timezone="Australia/Sydney")
        self.addCleanup(self.stop_scheduler)
        self.addCleanup(patch.stopall)
        patch.object(jobs, "_scheduler", self.scheduler).start()
        patch.object(config, "SCHEDULED_CHECKS", True).start()
        start = self.scheduler.start
        patch.object(self.scheduler, "start", side_effect=lambda: start(paused=True)).start()
        patch.object(jobs, "queue_daily_refresh").start()
        patch.object(jobs, "queue_cex_refresh").start()

    def stop_scheduler(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def test_cex_runs_at_five_past_each_hour_and_prices_stay_daily(self):
        jobs.start_scheduler()
        cex = self.scheduler.get_job("cex_refresh")
        daily = self.scheduler.get_job("daily_refresh")
        tz = ZoneInfo("Australia/Sydney")
        for month in (1, 7, 10):
            now = datetime(2026, month, 19, 4, 59, tzinfo=tz)
            self.assertEqual(cex.trigger.get_next_fire_time(None, now),
                             now.replace(hour=5, minute=5))
            self.assertEqual(daily.trigger.get_next_fire_time(None, now),
                             now.replace(hour=5, minute=0))
            now = now.replace(hour=5, minute=5, second=1)
            self.assertEqual(cex.trigger.get_next_fire_time(None, now),
                             now.replace(hour=6, minute=5, second=0))
        self.assertTrue(cex.coalesce)
        self.assertEqual(cex.misfire_grace_time, 300)
        self.assertEqual(jobs.next_run(), daily.next_run_time)
        self.assertEqual(jobs.next_run("cex_refresh"), cex.next_run_time)

    def test_disabled_schedule_does_not_start_either_job(self):
        with patch.object(config, "SCHEDULED_CHECKS", False):
            jobs.start_scheduler()
        self.assertFalse(self.scheduler.running)
        self.assertEqual(self.scheduler.get_jobs(), [])
        self.assertIsNone(jobs.next_run())
        self.assertIsNone(jobs.next_run("cex_refresh"))


class QueueTests(unittest.TestCase):
    @patch("app.jobs.submit")
    def test_cex_refresh_coalesces_queued_or_running_work(self, submit):
        first, second = Future(), Future()
        submit.side_effect = [first, second]
        with patch.object(jobs, "_cex_future", None):
            self.assertIs(jobs.queue_cex_refresh(), first)
            self.assertIs(jobs.queue_cex_refresh(), first)
            submit.assert_called_once_with("Refreshing CeX stock", jobs.cex.sync)
            first.set_result(None)
            self.assertIs(jobs.queue_cex_refresh(), second)
        self.assertEqual(submit.call_count, 2)

    @patch("app.jobs.submit")
    def test_cancelled_cex_refresh_can_be_queued_again(self, submit):
        cancelled = Future()
        cancelled.cancel()
        with patch.object(jobs, "_cex_future", cancelled):
            jobs.queue_cex_refresh()
        submit.assert_called_once_with("Refreshing CeX stock", jobs.cex.sync)

    @patch("app.jobs.submit")
    def test_daily_job_only_queues_price_checks(self, submit):
        jobs.queue_daily_refresh()
        submit.assert_called_once_with("Daily price refresh", jobs.tracker.check_all)


if __name__ == "__main__":
    unittest.main()
