import unittest
from unittest.mock import patch

from app.stores import vinted
from app.stores.base import Query


class _Title:
    def __init__(self, *, name: str, is_4k: bool):
        self.name = name
        self.is_4k = is_4k
        self.edition = ""
        self.upc = None
        self.amazon_asin = None
        self.search_terms = None
        self.year = None
        self.match_mode = "any"
        self.alt_upcs = None


class VintedStoreTests(unittest.TestCase):
    @patch("app.stores.vinted.fetch.browser_get")
    def test_search_parses_catalog_cards_and_filters_to_4k(self, browser_get):
        browser_get.return_value = """
        <a href="/items/111-plain-disc" title="Movie Standard Blu-ray, Condition: Very good, 6.50 A$, 8.20 A$">
          <img src="https://img.example/plain.webp" alt="Movie Standard Blu-ray" />
        </a>
        <a href="/items/222-uhd-disc" title="Movie 4K Ultra HD Blu-ray, Condition: New, 14.90 A$, 16.00 A$">
          <img src="https://img.example/uhd.webp" alt="Movie 4K Ultra HD Blu-ray" />
        </a>
        """
        offers = vinted.search(Query(_Title(name="Movie", is_4k=True)))
        self.assertEqual(len(offers), 1)
        self.assertEqual(offers[0].url, "https://www.vinted.com.au/items/222-uhd-disc")
        self.assertEqual(offers[0].price, 14.90)
        self.assertTrue(offers[0].in_stock)

    @patch("app.stores.vinted.fetch.browser_get")
    def test_refresh_reads_title_price_and_sold_status(self, browser_get):
        browser_get.return_value = """
        <meta property="og:title" content="Movie 4K Ultra HD Blu-ray" />
        <meta property="og:image" content="https://img.example/cover.webp" />
        <div data-testid="item-price">$19.99</div>
        <div>Item has been sold</div>
        """
        offer = vinted.refresh("https://www.vinted.com.au/items/999-example")
        self.assertIsNotNone(offer)
        assert offer is not None
        self.assertEqual(offer.title, "Movie 4K Ultra HD Blu-ray")
        self.assertEqual(offer.price, 19.99)
        self.assertFalse(offer.in_stock)
        self.assertTrue(offer.delivery_unknown)


if __name__ == "__main__":
    unittest.main()
