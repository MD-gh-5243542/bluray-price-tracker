import unittest
from unittest.mock import patch

from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel, Session, create_engine, select

from app import bluray, main, tracker
from app.db import Title


def _release(*, bluray_id: str, is_4k: bool) -> bluray.Release:
    fmt = "4K Blu-ray" if is_4k else "Blu-ray"
    name = "Example 4K" if is_4k else "Example Blu-ray"
    return bluray.Release(
        bluray_id=bluray_id,
        url=f"https://www.blu-ray.com/movies/x/{bluray_id}/",
        name=name,
        full_title=f"{name} {fmt}",
        is_4k=is_4k,
        edition=None,
        country="Australia",
    )


class WishlistFormatsTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool,
        )
        SQLModel.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.addCleanup(patch.stopall)
        patch("app.db.engine", self.engine).start()

    def test_add_title_accepts_standard_bluray(self):
        with patch("app.main.bluray.details", return_value=_release(bluray_id="1001", is_4k=False)), \
                patch("app.main.jobs.queue_check_title") as queue_check:
            response = main.add_title(
                url="https://www.blu-ray.com/movies/x/1001/", target_price="", check_now="1",
            )

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            titles = session.exec(select(Title)).all()
        self.assertEqual(len(titles), 1)
        self.assertFalse(titles[0].is_4k)
        queue_check.assert_called_once_with(titles[0].id)

    def test_relink_release_allows_switching_to_standard_bluray(self):
        with Session(self.engine) as session:
            title = Title(name="Old", bluray_id="999", bluray_url="https://www.blu-ray.com/movies/x/999/", is_4k=True)
            session.add(title)
            session.commit()
            session.refresh(title)
            tid = title.id

        with patch("app.main.bluray.details", return_value=_release(bluray_id="1002", is_4k=False)), \
                patch("app.main.jobs.queue_check_title") as queue_check:
            response = main.title_release(tid=tid, url="https://www.blu-ray.com/movies/x/1002/")

        self.assertEqual(response.status_code, 303)
        with Session(self.engine) as session:
            title = session.get(Title, tid)
            self.assertIsNotNone(title)
            self.assertFalse(title.is_4k)
            self.assertEqual(title.bluray_id, "1002")
        queue_check.assert_called_once_with(tid)

    def test_check_all_queues_standard_and_4k_wishlist_titles(self):
        with Session(self.engine) as session:
            keep_4k = Title(name="Wanted 4K", is_4k=True, purchased=False)
            keep_bd = Title(name="Wanted Blu-ray", is_4k=False, purchased=False)
            skip_owned = Title(name="Owned Blu-ray", is_4k=False, purchased=True)
            session.add(keep_4k)
            session.add(keep_bd)
            session.add(skip_owned)
            session.commit()
            session.refresh(keep_4k)
            session.refresh(keep_bd)

        with patch("app.tracker.check_titles") as check_titles:
            tracker.check_all()

        check_titles.assert_called_once()
        ids = check_titles.call_args.args[0]
        self.assertEqual(set(ids), {keep_4k.id, keep_bd.id})
        self.assertEqual(check_titles.call_args.kwargs, {"progress": None})


if __name__ == "__main__":
    unittest.main()
