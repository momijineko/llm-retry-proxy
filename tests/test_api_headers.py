import unittest
from types import SimpleNamespace

from retry_proxy.api import outbound_request_headers


class OutboundRequestHeadersTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            image_upstream_user_agent="codex_cli_rs/0.114.0",
            image_upstream_originator="codex_cli_rs",
        )

    def test_image_request_overrides_client_identity(self):
        headers = outbound_request_headers(
            {"user-agent": "Python-urllib/3.12", "content-type": "application/json"},
            "images/generations", "gpt-image-2", self.config,
        )

        self.assertEqual(headers["user-agent"], "codex_cli_rs/0.114.0")
        self.assertEqual(headers["originator"], "codex_cli_rs")

    def test_text_request_preserves_client_identity(self):
        headers = outbound_request_headers(
            {"user-agent": "client/1.0", "accept-encoding": "gzip, br, zstd"},
            "responses", "gpt-5.6", self.config,
        )

        self.assertEqual(headers["user-agent"], "client/1.0")
        self.assertEqual(headers["accept-encoding"], "gzip, deflate")
        self.assertNotIn("originator", headers)


if __name__ == "__main__":
    unittest.main()
