import unittest

from retry_proxy.api import _sse_has_token


class TtftSseTests(unittest.TestCase):
    def test_openai_metadata_does_not_count_as_token(self):
        found, remaining = _sse_has_token(
            b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
        )

        self.assertFalse(found)
        self.assertEqual(remaining, b"")

    def test_openai_content_counts_as_token_across_chunks(self):
        found, remaining = _sse_has_token(
            b'data: {"choices":[{"delta":{"content":"hello"}}]}\n\n'
        )

        self.assertTrue(found)
        self.assertEqual(remaining, b"")

    def test_unknown_non_json_data_counts_as_token(self):
        found, _ = _sse_has_token(b"data: token\n\n")

        self.assertTrue(found)

    def test_unknown_json_data_counts_as_token(self):
        found, _ = _sse_has_token(b'data: {"token":"hello"}\n\n')

        self.assertTrue(found)


if __name__ == "__main__":
    unittest.main()
