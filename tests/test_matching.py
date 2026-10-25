import unittest

from app import matching


class MatchingFormatGuardTests(unittest.TestCase):
    def test_bluray_title_rejects_dvd_listing(self):
        score = matching.score(
            wanted_name="Heat",
            wanted_4k=False,
            wanted_edition="",
            candidate="Heat DVD",
            loose=True,
            strict_edition=False,
        )
        self.assertEqual(score, 0.0)

    def test_bluray_title_rejects_bluray_plus_dvd_combo(self):
        score = matching.score(
            wanted_name="Heat",
            wanted_4k=False,
            wanted_edition="",
            candidate="Heat Blu-ray + DVD",
            loose=True,
            strict_edition=False,
        )
        self.assertEqual(score, 0.0)

    def test_bluray_title_still_accepts_bluray_listing(self):
        score = matching.score(
            wanted_name="Heat",
            wanted_4k=False,
            wanted_edition="",
            candidate="Heat Blu-ray",
            loose=True,
            strict_edition=False,
        )
        self.assertGreater(score, matching.REVIEW)

    def test_barcode_scoring_rejects_dvd_listing(self):
        score = matching.score_barcode(
            wanted_name="Heat",
            wanted_4k=False,
            wanted_edition="",
            candidate="Heat DVD",
            loose=True,
        )
        self.assertEqual(score, 0.0)


if __name__ == "__main__":
    unittest.main()
