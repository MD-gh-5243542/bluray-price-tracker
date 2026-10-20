import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, create_engine, select

from app import cex, cex_alerts, config, db, notify
from app.db import CexAlertState, CexProduct, CexStockAlert, ExcludedUrl, Title, get_session


class CexAlertsTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                               poolclass=StaticPool)
        self.addCleanup(engine.dispose)
        SQLModel.metadata.create_all(engine)
        self.addCleanup(patch.stopall)
        patch("app.db.engine", engine).start()
        patch.multiple(config, WAHA_URL="http://waha:3000", WAHA_SESSION="default",
                       WAHA_CHAT_ID="61400111222@c.us", WAHA_API_KEY="secret",
                       CEX_STOCK_ALERTS=True,
                       CEX_ALERT_LOCATIONS="Blacktown,Rouse Hill,online").start()
        self.send = patch("app.notify.send_waha").start()
        with get_session() as session:
            title = Title(name="Arrival", is_4k=True, upc="1234567890123")
            session.add(title)
            session.commit()
            session.refresh(title)
            self.title_id = title.id
            session.add(CexProduct(box_id=title.upc, name="Arrival", category="Blu-Ray 4K",
                                   sell_price=15, online_stock=False,
                                   branch_stock_json='{"Blacktown": false, "Rouse Hill": false}'))
            session.commit()

    def stock(self, online=False, blacktown=False, rouse=False):
        with get_session() as session:
            product = session.exec(select(CexProduct)).first()
            product.online_stock = online
            product.branch_stock_json = json.dumps({"Blacktown": blacktown, "Rouse Hill": rouse})
            product.last_seen = datetime.utcnow()
            session.add(product)
            session.commit()

    def alerts(self):
        with get_session() as session:
            return session.exec(select(CexStockAlert).order_by(CexStockAlert.id)).all()

    def test_baseline_silent_then_independent_locations_and_no_repeats(self):
        self.stock(online=True)
        cex_alerts.refresh()
        self.send.assert_not_called()
        self.stock(online=True, blacktown=True)
        cex_alerts.refresh()
        self.assertEqual(self.send.call_count, 1)
        self.assertIn("$15.00 | Blacktown", self.send.call_args.args[0])
        self.assertIn("Arrival", self.send.call_args.args[0])
        self.assertIn("https://au.webuy.com/product-detail?", self.send.call_args.args[0])
        cex_alerts.refresh()
        self.assertEqual(self.send.call_count, 1)
        self.stock(online=True, blacktown=True, rouse=True)
        cex_alerts.refresh()
        self.assertEqual(self.send.call_count, 2)
        self.assertIn("$15.00 | Rouse Hill", self.send.call_args.args[0])
        self.stock(online=True, blacktown=False, rouse=True)
        cex_alerts.refresh()
        self.stock(online=True, blacktown=True, rouse=True)
        cex_alerts.refresh()
        self.assertEqual(self.send.call_count, 3)

    def test_retry_survives_new_refresh_and_cancels_when_purchased(self):
        cex_alerts.refresh()
        self.stock(blacktown=True)
        self.send.side_effect = notify.WahaDeliveryError("WAHA returned HTTP 503.")
        cex_alerts.refresh()
        self.assertEqual(self.alerts()[0].status, "pending")
        self.assertEqual(cex_alerts.settings()["pending"], 1)
        self.send.side_effect = None
        cex_alerts.refresh()
        self.assertEqual(self.alerts()[0].status, "sent")
        self.assertEqual(self.alerts()[0].attempts, 2)
        self.stock()
        cex_alerts.refresh()
        self.stock(blacktown=True)
        self.send.side_effect = notify.WahaDeliveryError("offline")
        cex_alerts.refresh()
        with get_session() as session:
            title = session.get(Title, self.title_id)
            title.purchased = True
            session.add(title)
            session.commit()
        self.send.reset_mock()
        cex_alerts.refresh()
        self.send.assert_not_called()
        self.assertEqual(self.alerts()[-1].status, "cancelled")

    def test_sold_out_pending_is_not_sent(self):
        cex_alerts.refresh()
        self.stock(blacktown=True)
        self.send.side_effect = notify.WahaDeliveryError("offline")
        cex_alerts.refresh()
        self.stock()
        self.send.reset_mock()
        cex_alerts.refresh()
        self.send.assert_not_called()
        self.assertEqual(self.alerts()[0].status, "cancelled")

    def test_disable_and_reenable_and_location_changes_reset_baseline(self):
        cex_alerts.refresh()
        cex_alerts.configure(False, ["Blacktown"])
        self.stock(blacktown=True)
        cex_alerts.refresh()
        cex_alerts.configure(True, ["Blacktown"])
        cex_alerts.refresh()
        self.send.assert_not_called()
        cex_alerts.configure(True, ["Rouse Hill"])
        self.stock(blacktown=True, rouse=True)
        cex_alerts.refresh()
        self.send.assert_not_called()

    def test_unknown_stock_does_not_generate_restock(self):
        self.stock(blacktown=None)
        cex_alerts.refresh()
        self.stock(blacktown=True)
        cex_alerts.refresh()
        self.send.assert_not_called()

    def test_new_matching_release_after_baseline_alerts(self):
        cex_alerts.refresh()
        with get_session() as session:
            session.add(CexProduct(box_id="other", name="Arrival", category="Blu-Ray 4K",
                                   sell_price=10, online_stock=True, branch_stock_json="{}"))
            session.commit()
        cex_alerts.refresh()
        self.send.assert_called_once()
        self.assertIn("$10.00 | Online", self.send.call_args.args[0])

    def test_ineligible_titles_categories_stale_and_low_confidence_excluded(self):
        with get_session() as session:
            title = session.get(Title, self.title_id)
            title.needs_review = True
            session.add(title)
            session.commit()
        cex_alerts.refresh()
        with get_session() as session:
            self.assertEqual(session.exec(select(CexAlertState)).all(), [])
            title = session.get(Title, self.title_id)
            title.needs_review = False
            session.add(title)
            session.commit()
        cex_alerts.refresh()
        with get_session() as session:
            for box, category, seen, name in (
                ("anime", "Blu-Ray Anime", datetime.utcnow(), "Arrival"),
                ("stale", "Blu-Ray 4K", datetime.utcnow() - timedelta(days=3), "Arrival"),
                ("wrong", "Blu-Ray 4K", datetime.utcnow(), "Completely Unrelated"),
            ):
                session.add(CexProduct(box_id=box, name=name, category=category,
                                       online_stock=True, last_seen=seen))
            session.commit()
        cex_alerts.refresh()
        self.send.assert_not_called()

    def test_target_change_is_silent_new_baseline(self):
        cex_alerts.refresh()
        self.stock(online=True)
        with patch.object(config, "WAHA_CHAT_ID", "other@g.us"):
            cex_alerts.refresh()
        self.send.assert_not_called()

    def test_configuration_missing_never_sends(self):
        with patch.object(config, "WAHA_URL", ""):
            cex_alerts.refresh()
            self.send.assert_not_called()
            self.assertIn("WAHA_URL", cex_alerts.settings()["config_error"])

    def test_exact_edition_and_excluded_products_are_not_alerted(self):
        with get_session() as session:
            title = session.get(Title, self.title_id)
            title.match_mode = "exact"
            session.add(title)
            session.add(ExcludedUrl(title_id=self.title_id, store="cex",
                                    url=cex._box_url("1234567890123", "Blu-Ray 4K")))
            session.commit()
        cex_alerts.refresh()
        self.stock(online=True)
        with get_session() as session:
            session.add(CexProduct(box_id="other", name="Arrival", category="Blu-Ray 4K",
                                   sell_price=10, online_stock=True))
            session.commit()
        cex_alerts.refresh()
        self.send.assert_not_called()

    def test_price_and_buyback_changes_do_not_alert(self):
        cex_alerts.refresh()
        with get_session() as session:
            product = session.exec(select(CexProduct)).first()
            product.sell_price = 9
            product.cash_buy_price = 4
            product.exchange_price = 7
            session.add(product)
            session.commit()
        cex_alerts.refresh()
        self.send.assert_not_called()

    def test_repeated_restock_while_waha_offline_coalesces_pending(self):
        cex_alerts.refresh()
        self.stock(blacktown=True)
        self.send.side_effect = notify.WahaDeliveryError("offline")
        cex_alerts.refresh()
        self.stock(blacktown=True, rouse=True)
        self.send.side_effect = None
        self.send.reset_mock()
        cex_alerts.refresh()
        self.send.assert_called_once()
        self.assertIn("Blacktown, Rouse Hill", self.send.call_args.args[0])
        self.assertEqual([row.status for row in self.alerts()], ["cancelled", "sent"])

    def test_routes_save_validate_and_do_not_expose_api_key(self):
        from app.main import app
        client = TestClient(app)
        response = client.get("/settings")
        self.assertEqual(response.status_code, 200)
        self.assertIn("WAHA", response.text)
        self.assertNotIn("secret", response.text)
        response = client.post("/settings/cex-alerts",
                               data={"enabled": "on", "locations": ["Blacktown", "online"]},
                               follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(cex_alerts.settings()["locations"], ["Blacktown", "online"])
        self.assertEqual(client.post("/settings/cex-alerts", data={"enabled": "on"}).status_code, 400)
        self.assertEqual(client.post("/settings/cex-alerts",
                                    data={"locations": "Invalid"}).status_code, 400)
        self.send.assert_not_called()

    def test_deleting_title_removes_alert_state_and_queue(self):
        from app.main import app
        cex_alerts.refresh()
        self.stock(blacktown=True)
        self.send.side_effect = notify.WahaDeliveryError("offline")
        cex_alerts.refresh()
        response = TestClient(app).post(f"/title/{self.title_id}/delete", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        with get_session() as session:
            self.assertIsNone(session.get(CexAlertState, self.title_id))
            self.assertEqual(session.exec(select(CexStockAlert)).all(), [])

    def test_explicit_waha_test_uses_preview_text_and_surfaces_failure(self):
        from app.main import app
        client = TestClient(app)
        response = client.post("/settings/test-waha", follow_redirects=False)
        self.assertEqual(response.status_code, 303)
        self.send.assert_called_once_with(notify.WAHA_TEST_MESSAGE)
        self.send.side_effect = notify.WahaDeliveryError("WAHA returned HTTP 401.")
        response = client.post("/settings/test-waha", follow_redirects=False)
        self.assertIn("WAHA+test+failed", response.headers["location"])

    def test_cex_and_settings_show_hourly_next_refresh(self):
        from zoneinfo import ZoneInfo
        from app.main import app
        next_cex = datetime(2026, 10, 19, 16, 5, tzinfo=ZoneInfo("Australia/Sydney"))
        with patch("app.jobs.next_run", return_value=next_cex):
            client = TestClient(app)
            cex_page = client.get("/cex")
            settings_page = client.get("/settings")
        self.assertEqual(cex_page.status_code, 200)
        self.assertIn("next automatic refresh Mon 19 Oct 16:05 Sydney time", cex_page.text)
        self.assertIn("hourly at :05 (catalogue and wishlist stock)", settings_page.text)

    def test_defaults_are_disabled_and_schema_initialization_is_repeatable(self):
        with patch.object(config, "CEX_STOCK_ALERTS", False):
            self.assertFalse(cex_alerts.settings()["enabled"])
            db.init_db()
            db.init_db()
            cex_alerts.refresh()
        self.send.assert_not_called()

    def test_successful_sync_runs_alert_detection(self):
        with patch.object(cex, "_catalogue", return_value=[]), \
                patch.object(cex, "_favourite_products", return_value=[]), \
                patch.object(cex, "_record_products"), \
                patch.object(cex, "_sync_favourites_to_wishlist"), \
                patch.object(cex, "_match_collection_titles"), \
                patch.object(cex, "_refresh_title_matches"), \
                patch.object(cex_alerts, "refresh") as refresh:
            cex.sync()
        refresh.assert_called_once()


class WahaTransportTests(unittest.TestCase):
    def test_invalid_urls_are_rejected_without_network(self):
        for url in ("", "file:///tmp", "http://waha:bad", "http://[", "http://key@host",
                    "http://host?api-key=secret"):
            with self.subTest(url=url), patch.object(config, "WAHA_URL", url):
                self.assertIsNotNone(notify.waha_config_error())

    @patch.multiple(config, WAHA_URL="http://waha:3000", WAHA_SESSION="default",
                    WAHA_CHAT_ID="destination@g.us", WAHA_API_KEY="secret")
    @patch("app.notify.httpx.post")
    def test_payload_auth_and_no_redirects(self, post):
        post.return_value = httpx.Response(201)
        notify.send_waha("CeX wishlist: Back in stock")
        args, kwargs = post.call_args
        self.assertEqual(args[0], "http://waha:3000/api/sendText")
        self.assertEqual(kwargs["headers"], {"X-Api-Key": "secret"})
        self.assertEqual(kwargs["json"], {
            "session": "default", "chatId": "destination@g.us",
            "text": "CeX wishlist: Back in stock", "linkPreview": False,
        })
        self.assertFalse(kwargs["follow_redirects"])

    @patch.multiple(config, WAHA_URL="http://waha:3000", WAHA_SESSION="default",
                    WAHA_CHAT_ID="destination@g.us", WAHA_API_KEY="secret")
    @patch("app.notify.httpx.post")
    def test_failure_messages_do_not_expose_keys_or_server_body(self, post):
        post.return_value = httpx.Response(401, text="secret private response")
        with self.assertRaisesRegex(notify.WahaDeliveryError, "^WAHA returned HTTP 401.$"):
            notify.send_waha("test")
        post.side_effect = httpx.ConnectError("secret private URL")
        with self.assertRaisesRegex(notify.WahaDeliveryError, r"^WAHA connection failed \(ConnectError\).$"):
            notify.send_waha("test")


if __name__ == "__main__":
    unittest.main()
