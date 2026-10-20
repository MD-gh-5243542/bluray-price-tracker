import unittest
from unittest.mock import Mock, patch

import httpx

from app import cex


class CexRateLimitTests(unittest.TestCase):
    def response(self, code: int, body: dict | None = None, headers: dict | None = None):
        request = httpx.Request("POST", cex.ALGOLIA_URL)
        return httpx.Response(code, request=request, json=body or {"hits": []},
                              headers=headers or {})

    @patch("app.cex.time.sleep")
    def test_query_retries_429_and_then_succeeds(self, sleep):
        client = Mock()
        client.post.side_effect = [
            self.response(429, headers={"Retry-After": "1"}),
            self.response(429),
            self.response(200, body={"hits": [{"boxId": "x"}]}),
        ]
        payload = cex._query(client, "", filters=cex.FILTER_4K)
        self.assertEqual(payload["hits"][0]["boxId"], "x")
        self.assertEqual(client.post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @patch("app.cex.time.sleep")
    def test_query_fails_after_repeated_429(self, sleep):
        client = Mock()
        client.post.side_effect = [self.response(429) for _ in range(6)]
        with self.assertRaisesRegex(RuntimeError, "CeX rate limited while querying CeX page 1"):
            cex._query(client, "", filters=cex.FILTER_4K)
        self.assertEqual(client.post.call_count, 6)
        self.assertEqual(sleep.call_count, 5)


if __name__ == "__main__":
    unittest.main()
