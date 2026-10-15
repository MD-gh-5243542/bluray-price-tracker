import unittest
from unittest.mock import patch

from app.stores import amazon
from app.stores.base import Offer


class AmazonBargainsTests(unittest.TestCase):
    @patch("app.stores.amazon.fetch.smart_get")
    @patch("app.stores.amazon._search")
    def test_bargains_use_4k_variant_price_not_parent_listing_price(
        self, search, smart_get,
    ):
        parent_asin = "B0PARENT01"
        four_k_asin = "B0FOURK4K1"
        search.return_value = [
            Offer(
                "amazon", amazon.product_url(parent_asin),
                "Fight Club 4K Ultra HD/Blu-ray", 24.57,
            )
        ]
        smart_get.return_value = f"""
            <h1 id="productTitle">Fight Club</h1>
            <div id="corePrice_feature_div"><span class="a-offscreen">$24.57</span></div>
            <div id="formats"><ul id="tmmSwatches">
              <li class="swatchElement selected">
                <a href="/dp/{four_k_asin}">4K Ultra HD</a>
                <span class="a-color-secondary">$44.67</span>
              </li>
              <li class="swatchElement">
                <a href="/dp/B0BLURAY01">Blu-ray</a>
                <span class="a-color-secondary">$24.57</span>
              </li>
            </ul></div>
        """

        bargains = amazon.bargains()

        self.assertEqual(len(bargains), 1)
        self.assertEqual(bargains[0].price, 44.67)
        self.assertEqual(bargains[0].url, amazon.product_url(four_k_asin))
        smart_get.assert_called_once_with(
            amazon.product_url(parent_asin), wait_selector="#productTitle",
        )


if __name__ == "__main__":
    unittest.main()
