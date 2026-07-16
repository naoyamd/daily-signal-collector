import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from scripts.web_scout import (
    SCOUT_SCHEMA,
    is_public_https_url,
    load_scout_handoff,
    validate_scout_handoff,
)


NOW = datetime(2026, 7, 16, 3, tzinfo=timezone.utc)


class WebScoutTests(unittest.TestCase):
    def test_loads_local_metadata_and_rejects_unsafe_urls_without_fetching(self):
        warnings = []
        payload = {
            "schema": SCOUT_SCHEMA,
            "searched_queries": ["aircraft engine certification"],
            "items": [
                {
                    "title": "Authority updates engine guidance",
                    "url": "https://authority.example/guidance/42?utm_source=scout",
                    "source": "Aviation authority",
                    "source_kind": "official",
                    "category": "Certification",
                    "published_at": "2026-07-15T01:00:00Z",
                    "excerpt": "Operationally useful guidance.",
                    "query": "aircraft engine certification",
                    "tags": ["aerospace", "safety"],
                    "organization": "Authority",
                    "full_text": "must not be retained",
                },
                {"title": "Local", "url": "https://127.0.0.1/admin"},
                {"title": "Private", "url": "https://10.0.0.5/report"},
                {"title": "Insecure", "url": "http://news.example.com/report"},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            handoff = Path(directory) / "scout.json"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            items = load_scout_handoff(handoff, NOW, warn=warnings.append)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item.url, "https://authority.example/guidance/42")
        self.assertEqual(item.source_kind, "official")
        self.assertEqual(item.query, "aircraft engine certification")
        self.assertEqual(item.metadata["searched_queries"], ["aircraft engine certification"])
        self.assertNotIn("full_text", item.metadata)
        self.assertEqual(len(warnings), 3)

    def test_validation_is_bounded_and_deduplicates(self):
        warnings = []
        payload = {
            "items": [
                {"title": "First", "url": "https://one.example/item"},
                {"title": "First duplicate", "url": "https://one.example/item#copy"},
                {"title": "Never inspected", "url": "https://two.example/item"},
            ]
        }
        items = validate_scout_handoff(payload, NOW, max_items=2, warn=warnings.append)
        self.assertEqual([item.url for item in items], ["https://one.example/item"])
        self.assertTrue(any("truncated" in message for message in warnings))

    def test_rejects_oversized_invalid_and_wrong_schema_handoffs(self):
        warnings = []
        with tempfile.TemporaryDirectory() as directory:
            handoff = Path(directory) / "scout.json"
            handoff.write_text("x" * 2_000, encoding="utf-8")
            self.assertEqual(
                load_scout_handoff({"path": handoff, "max_bytes": 1_024}, NOW, warn=warnings.append),
                [],
            )
        self.assertTrue(any("exceeds" in message for message in warnings))

        warnings.clear()
        self.assertEqual(
            validate_scout_handoff({"schema": "unknown/v9", "items": []}, NOW, warn=warnings.append),
            [],
        )
        self.assertIn("unsupported schema", warnings[0])

    def test_public_https_guard_is_dns_free_and_strict(self):
        self.assertTrue(is_public_https_url("https://press.vendor.example/releases/1"))
        self.assertFalse(is_public_https_url("https://localhost:8443/item"))
        self.assertFalse(is_public_https_url("https://192.168.1.1/item"))
        self.assertFalse(is_public_https_url("https://203.0.113.8/item"))
        self.assertFalse(is_public_https_url("https://intranet/item"))
        self.assertFalse(is_public_https_url("https://user:secret@example.com/item"))
        self.assertFalse(is_public_https_url("https://example.com:not-a-port/item"))


if __name__ == "__main__":
    unittest.main()
