import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import db
from app.db import Setting, Title


class StoreMigrationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(patch.stopall)
        patch("app.db.engine", self.engine).start()

    def test_init_db_replaces_dvdhub_with_vinted_for_existing_settings(self):
        with Session(self.engine) as session:
            session.add(Setting(key="enabled_stores", value="amazon,dvdhub,sanity"))
            session.add(Title(name="Example", best_store="dvdhub", best_price=10.0, best_url="https://example.test"))
            session.commit()

        db.init_db()

        with Session(self.engine) as session:
            enabled = session.get(Setting, "enabled_stores")
            self.assertIsNotNone(enabled)
            stores = enabled.value.split(",") if enabled else []
            self.assertNotIn("dvdhub", stores)
            self.assertIn("vinted", stores)
            self.assertIn("amazon", stores)
            title = session.exec(db.select(Title)).first()
            self.assertIsNotNone(title)
            self.assertIsNone(title.best_store)
            self.assertIsNone(title.best_price)
            self.assertIsNone(title.best_url)


if __name__ == "__main__":
    unittest.main()
