import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.web_scout import (
    SCOUT_SCHEMA,
    SCOUT_SCHEMA_V1,
    SCOUT_SCHEMA_V2,
    diagnose_scout_handoff,
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

    def test_v2_strict_validates_freshness_coverage_and_preserves_warnings(self):
        payload = {
            "schema": SCOUT_SCHEMA_V2,
            "generated_at": NOW.isoformat(),
            "searched_queries": ["aircraft engine certification"],
            "warnings": ["Beta: no new public finding"],
            "checked_sources": [
                {"name": "Alpha", "status": "found", "query": "aircraft engine certification"},
                {"name": "Beta", "status": "no_new_finding", "warning": "no new public finding"},
            ],
            "items": [
                {
                    "title": "Authority updates engine guidance",
                    "url": "https://authority.example/guidance/42",
                    "source": "Authority",
                    "source_kind": "official",
                    "category": "Certification",
                    "published_at": "2026-07-15",
                    "excerpt": "x" * 400,
                }
            ],
        }
        plan = {"watchlist": {"active_sources": [{"name": "Alpha"}, {"name": "Beta"}]}}
        diagnostics = {}
        items = validate_scout_handoff(
            payload,
            NOW,
            strict=True,
            research_plan=plan,
            diagnostics=diagnostics,
            warn=lambda message: None,
        )
        self.assertEqual(len(items), 1)
        self.assertEqual(len(items[0].excerpt), 400)
        self.assertEqual(items[0].published_at, "2026-07-15T00:00:00+00:00")
        self.assertEqual(items[0].metadata["warnings"], ["Beta: no new public finding"])
        self.assertEqual(items[0].metadata["published_at_quality"], "reported")
        self.assertTrue(diagnostics["valid"] if "valid" in diagnostics else not diagnostics["errors"])

    def test_v2_strict_rejects_stale_future_and_bad_coverage(self):
        base = {
            "schema": SCOUT_SCHEMA_V2,
            "generated_at": NOW.isoformat(),
            "searched_queries": ["q"],
            "checked_sources": [{"name": "Alpha", "status": "found", "query": "q"}],
            "items": [
                {
                    "title": "Title",
                    "url": "https://authority.example/item",
                    "source": "Authority",
                    "source_kind": "official",
                    "category": "Certification",
                }
            ],
        }
        plan = {"watchlist": {"active_sources": [{"name": "Alpha"}]}}
        stale = dict(base, generated_at=(NOW - timedelta(hours=37)).isoformat())
        future = dict(base, generated_at=(NOW + timedelta(seconds=1)).isoformat())
        duplicate = dict(
            base,
            checked_sources=[
                {"name": "Alpha", "status": "found", "query": "q"},
                {"name": "alpha", "status": "unreachable", "warning": "down"},
            ],
        )
        for payload, expected in ((stale, "stale"), (future, "future"), (duplicate, "duplicate")):
            diagnostics = diagnose_scout_handoff(payload, NOW, research_plan=plan)
            self.assertFalse(diagnostics["valid"])
            self.assertTrue(any(expected in message for message in diagnostics["errors"]))

    def test_v1_remains_readable_and_cli_emits_json_exit_code(self):
        payload = {"schema": SCOUT_SCHEMA_V1, "items": []}
        with tempfile.TemporaryDirectory() as directory:
            handoff = Path(directory) / "scout.json"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            self.assertEqual(load_scout_handoff(handoff, NOW, warn=lambda message: None), [])
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.web_scout",
                    "validate",
                    str(handoff),
                    "--now",
                    NOW.isoformat(),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        diagnostic = json.loads(result.stdout)
        self.assertFalse(diagnostic["valid"])
        self.assertTrue(diagnostic["errors"])

    def test_cli_accepts_v2_with_research_plan_and_runner_limits(self):
        payload = {
            "schema": SCOUT_SCHEMA_V2,
            "generated_at": NOW.isoformat(),
            "searched_queries": ["official Alpha aerospace update"],
            "checked_sources": [{
                "name": "Alpha",
                "status": "no_new_finding",
                "warning": "no new public finding",
            }],
            "items": [],
        }
        plan = {"watchlist": {"active_sources": [{"name": "Alpha"}]}}
        with tempfile.TemporaryDirectory() as directory:
            handoff = Path(directory) / "scout.json"
            research_plan = Path(directory) / "research-plan.json"
            handoff.write_text(json.dumps(payload), encoding="utf-8")
            research_plan.write_text(json.dumps(plan), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.web_scout",
                    "validate",
                    str(handoff),
                    "--research-plan",
                    str(research_plan),
                    "--max-age-hours",
                    "6",
                    "--max-items",
                    "80",
                    "--now",
                    NOW.isoformat(),
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        diagnostic = json.loads(result.stdout)
        self.assertTrue(diagnostic["valid"])
        self.assertTrue(diagnostic["coverage_valid"])


if __name__ == "__main__":
    unittest.main()
