import base64
import json
import os
import tempfile
import unittest

import yaml

from retry_proxy.dlp import inspect_json_body, load_policy, validate_policy


RULE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "retry_proxy", "dlp_rules.yaml")
MARKER_START = "[[ALLOW_SENSITIVE]]"
MARKER_END = "[[/ALLOW_SENSITIVE]]"


class DlpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        load_policy.cache_clear()
        cls.policy = load_policy(RULE_FILE)
        cls.enabled = frozenset(name for name, rule in cls.policy.rules.items() if rule.enabled)

    def inspect(self, payload, mode="redact", enabled=None, rule_file=RULE_FILE, **kwargs):
        return inspect_json_body(
            json.dumps(payload).encode(), enabled or self.enabled, MARKER_START, MARKER_END,
            mode=mode, rule_file=rule_file, **kwargs,
        )

    def test_policy_validates(self):
        result = validate_policy(RULE_FILE)
        self.assertEqual(result["version"], 2)
        self.assertGreaterEqual(result["enabled"], 10)

    def test_all_user_history_and_tool_outputs_are_scanned(self):
        field = "api" + "Key"
        value = "value" + "1234567890"
        tool_output = json.dumps({field: value})
        payload = {"messages": [
            {"role": "system", "content": tool_output},
            {"role": "user", "content": {field: value}},
            {"role": "assistant", "content": tool_output},
            {"role": "tool", "content": tool_output},
            {"role": "user", "content": {field: value}},
        ]}
        result = self.inspect(payload)
        cleaned = json.loads(result.body)
        self.assertIn("structured_secret", result.matched_rules)
        self.assertNotIn(value, json.dumps(cleaned["messages"][1]))
        self.assertNotIn(value, cleaned["messages"][3]["content"])
        self.assertNotIn(value, json.dumps(cleaned["messages"][4]))
        self.assertIn(value, cleaned["messages"][0]["content"])
        self.assertIn(value, cleaned["messages"][2]["content"])

    def test_explicit_exemption_wins(self):
        text = MARKER_START + "skipped value" + MARKER_END
        result = self.inspect({"input": text}, allow_exemptions=True)
        self.assertEqual(result.exemptions, 1)
        self.assertEqual(json.loads(result.body)["input"], "skipped value")

    def test_exemption_markers_are_scanned_when_disabled(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        text = MARKER_START + token + MARKER_END
        result = self.inspect({"input": text}, allow_exemptions=False)
        self.assertEqual(result.exemptions, 0)
        self.assertIn("ai_tokens", result.matched_rules)
        self.assertNotIn(token, result.body.decode())

    def test_encoded_secrets_are_scanned_recursively(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        encoded = {
            "base64": base64.b64encode(token.encode()).decode(),
            "base64url": base64.urlsafe_b64encode(token.encode()).decode().rstrip("="),
            "base64_twice": base64.b64encode(base64.b64encode(token.encode())).decode(),
            "hex": token.encode().hex(),
            "percent": "".join(f"%{byte:02X}" for byte in token.encode()),
        }
        for name, value in encoded.items():
            with self.subTest(name=name):
                result = self.inspect({"messages": [{"role": "tool", "content": value}]},
                                      mode="block", decode_depth=2)
                self.assertIn("encoded_secret", result.matched_rules)
                self.assertIn("ai_tokens", result.matched_rules)
                self.assertIn("encoded_secret", result.blocked_rules)

    def test_encoded_redaction_replaces_the_original_fragment(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        encoded = base64.b64encode(token.encode()).decode()
        result = self.inspect({"input": f"prefix:{encoded}:suffix"}, decode_depth=1)
        cleaned = json.loads(result.body)["input"]
        self.assertEqual(cleaned, "prefix:[REDACTED:encoded_secret]:suffix")
        self.assertNotIn(encoded, cleaned)

    def test_known_key_pool_secret_matches_unknown_format_and_encoding(self):
        secret = "vendor-private-value-987654321"
        encoded = base64.b64encode(secret.encode()).decode()
        result = self.inspect({"input": encoded}, mode="block", enabled=frozenset({"ai_tokens"}),
                              known_secrets=(secret,), decode_depth=1)
        self.assertIn("known_secret", result.matched_rules)
        self.assertIn("encoded_secret", result.blocked_rules)

    def test_decode_budget_stops_additional_candidates(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        encoded = base64.b64encode(token.encode()).decode()
        result = self.inspect({"input": f"{encoded} {encoded}"}, mode="block", decode_depth=1,
                              decode_max_candidates=1)
        self.assertIn("encoded_secret", result.blocked_rules)
        self.assertTrue(result.limit_exceeded)

    def test_disabled_exemptions_do_not_require_markers(self):
        token = "sk-A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        result = inspect_json_body(
            json.dumps({"input": token}).encode(), self.enabled, "", "",
            mode="block", rule_file=RULE_FILE, allow_exemptions=False,
        )
        self.assertFalse(result.malformed_exemption)
        self.assertIn("ai_tokens", result.blocked_rules)

    def test_non_json_body_is_marked_uninspectable(self):
        result = inspect_json_body(
            b"plain text", self.enabled, MARKER_START, MARKER_END,
            mode="block", rule_file=RULE_FILE,
        )
        self.assertTrue(result.uninspectable)
        self.assertEqual(result.body, b"plain text")

    def test_entropy_and_allowlist_reduce_false_positives(self):
        prefix = "".join(chr(code) for code in (115, 107, 45))
        low_entropy = prefix + "a" * 32
        varied = prefix + "A1b2C3d4E5f6G7h8J9k0LmNoPqRsTuVx"
        self.assertNotIn("ai_tokens", self.inspect({"input": low_entropy}).matched_rules)
        self.assertIn("ai_tokens", self.inspect({"input": varied}).matched_rules)
        allowed = "api_key=YOUR_KEY_123456789"
        self.assertNotIn("credentials", self.inspect({"input": allowed}).matched_rules)
        structured = self.inspect({"input": {"api_key": "YOUR_KEY"}})
        self.assertNotIn("structured_secret", structured.matched_rules)

    def test_binary_payloads_are_skipped(self):
        payload = "data:image/png;base64," + "A" * 5000
        result = self.inspect({"input": {"image_url": payload}})
        self.assertEqual(json.loads(result.body)["input"]["image_url"], payload)
        self.assertFalse(result.matched_rules)

    def test_csv_key_column_is_redacted_without_removing_metadata(self):
        key = "random" + "KeyValue123456789"
        csv_text = "key,url,provider,label,models,paths\n" + key + ",https://example.test,example,image,gpt-image-*,images/*"
        result = self.inspect({"messages": [{"role": "tool", "content": csv_text}]})
        cleaned = json.loads(result.body)["messages"][0]["content"]
        self.assertIn("csv_credentials", result.matched_rules)
        self.assertNotIn(key, cleaned)
        self.assertIn("https://example.test,example,image,gpt-image-*,images/*", cleaned)

    def test_rule_actions_and_longest_overlap(self):
        policy = {
            "version": 2,
            "defaults": {"action": "redact", "placeholder": "[REDACTED:{rule}]"},
            "rules": {
                "short": {"pattern": "secret", "action": "audit"},
                "long": {"pattern": "secret-value", "action": "block"},
                "mask": {"pattern": "token-[0-9]+", "placeholder": "<hidden:{rule}>"},
            },
        }
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", encoding="utf-8", delete=False) as handle:
            yaml.safe_dump(policy, handle)
            path = handle.name
        try:
            load_policy.cache_clear()
            result = self.inspect({"input": "secret-value token-12345"}, enabled=frozenset(policy["rules"]), rule_file=path)
            self.assertEqual(result.blocked_rules, ("long",))
            self.assertIn("<hidden:mask>", result.body.decode())
            self.assertNotIn("token-12345", result.body.decode())
        finally:
            os.unlink(path)
            load_policy.cache_clear()


if __name__ == "__main__":
    unittest.main()
