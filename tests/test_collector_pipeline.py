import json
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from scripts.collector_pipeline import CANDIDATES_SCHEMA, _adaptive_rank, build_handoff, run_pipeline
from scripts.models import Item


NOW = datetime(2026, 7, 16, 3, 0, tzinfo=timezone.utc)


def make_item(identifier, title, url, *, source="Source", source_kind="feed", score=1.0, query="", tags=None):
    return Item(
        id=identifier,
        title=title,
        url=url,
        source=source,
        category="Engineering",
        published_at=NOW.isoformat(),
        excerpt=f"Summary for {title}",
        score=score,
        source_kind=source_kind,
        query=query,
        tags=list(tags or []),
        metadata={"feed_entry_id": identifier},
    )


class FakePool:
    def __init__(self, vault_path, captured, marked):
        self.vault_path = Path(vault_path)
        self.captured = captured
        self.marked = marked

    def ingest(self, items, collected_at=None):
        values = list(items)
        self.captured.extend(values)
        return [self.vault_path / f"{item.id}.md" for item in values]

    def mark_candidates(self, payload):
        self.marked.append(payload)


class CollectorPipelineTests(unittest.TestCase):
    def test_pipeline_pools_every_in_scope_discovery_and_writes_exact_atomic_handoff(self):
        rss_kept = make_item(
            "rss-kept",
            "Aircraft engine digital twin",
            "https://updates.vendor.example/engineering/twin",
            source="Vendor engineering",
            source_kind="corporate_tech",
            query="engine digital twin",
            tags=["aerospace"],
        )
        rss_excluded = make_item(
            "rss-drone",
            "Consumer drone promotion",
            "https://updates.vendor.example/drone",
            score=99,
        )
        rss_quasi = make_item(
            "rss-quasi",
            "Quasi-steady aircraft simulation",
            "https://research.example/quasi-steady",
        )
        scout = make_item(
            "scout",
            "New certification standard",
            "https://standards.example/standard/1",
            source="Standards body",
            source_kind="standard",
            score=3,
        )
        config = {
            "edition": "research",
            "site": {
                "lookback_hours": 72,
                "timezone": "Asia/Tokyo",
                "max_items": 8,
                "candidate_pool_size": 1,
            },
            "research": {
                "exclude_terms": ["consumer drone", "UAS"],
                "domain_groups": {
                    "vendor_technical": ["vendor.example"],
                    "standards": ["standards.example"],
                },
            },
            "topics": [{"keywords": ["digital twin", "aircraft engine"]}],
            "openclaw_scout": {"path": "/local/scout.json"},
            "candidates_handoff": {"ttl_hours": 6, "timezone": "Asia/Tokyo"},
        }
        pooled = []
        marked = []
        adaptive_calls = []

        def rss_collector(_config, _now):
            return [rss_kept, rss_excluded, rss_quasi]

        def scout_loader(scout_config, _now, warn=None):
            self.assertEqual(scout_config["path"], "/local/scout.json")
            return [scout]

        def adaptive_ranker(items, _config, learning_db):
            adaptive_calls.append((list(items), learning_db))
            # Demonstrate that learned ranking, rather than the initial score,
            # controls the one-item handoff.
            by_id = {item.id: item for item in items}
            return [replace(by_id["rss-kept"], score=12), by_id["scout"], by_id["rss-quasi"]]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "handoff" / "candidates.json"
            output.parent.mkdir(parents=True)
            output.write_text("old incomplete content", encoding="utf-8")
            payload = run_pipeline(
                config,
                vault_path=root / "vault",
                output_path=output,
                learning_db=root / "learning.sqlite3",
                now=NOW,
                rss_collector=rss_collector,
                scout_loader=scout_loader,
                pool_factory=lambda path: FakePool(path, pooled, marked),
                adaptive_ranker=adaptive_ranker,
            )
            on_disk = json.loads(output.read_text(encoding="utf-8"))
            leftovers = list(output.parent.glob(".candidates.json.*.tmp"))

        self.assertEqual(on_disk, payload)
        self.assertEqual(marked, [payload])
        self.assertEqual(leftovers, [])
        self.assertEqual(
            set(payload),
            {
                "schema", "batch_id", "edition", "generated_at", "timezone", "expires_at",
                "max_items", "collection_counts", "items",
            },
        )
        self.assertEqual(payload["schema"], CANDIDATES_SCHEMA)
        self.assertEqual(payload["edition"], "research")
        self.assertEqual(payload["timezone"], "Asia/Tokyo")
        self.assertTrue(payload["generated_at"].endswith("+09:00"))
        self.assertTrue(payload["expires_at"].endswith("+09:00"))
        self.assertEqual(payload["max_items"], 8)
        self.assertEqual([item.id for item in pooled], ["rss-kept", "rss-quasi", "scout"])
        self.assertEqual([item.id for item in adaptive_calls[0][0]], ["scout", "rss-kept", "rss-quasi"])
        self.assertEqual(payload["collection_counts"], {
            "rss_items": 3,
            "scout_items": 1,
            "collected_items": 4,
            "pooled_items": 3,
            "excluded_items": 1,
            "ranked_items": 3,
            "candidate_items": 1,
        })
        exported = payload["items"][0]
        self.assertEqual(exported["id"], "rss-kept")
        self.assertEqual(exported["source"], "Vendor engineering")
        self.assertEqual(exported["query"], "engine digital twin")
        self.assertEqual(exported["matched_keywords"], ["digital twin", "aircraft engine"])
        self.assertEqual(exported["tags"], ["aerospace"])
        self.assertEqual(exported["metadata"]["domain_groups"], ["vendor_technical"])
        self.assertEqual(exported["candidate_signals"], {
            "source": "Vendor engineering",
            "keywords": ["digital twin", "aircraft engine"],
            "queries": ["engine digital twin"],
            "domain_groups": ["vendor_technical"],
            "tags": ["aerospace"],
        })

    def test_handoff_is_deterministic_bounded_and_uses_utc_for_bad_timezone(self):
        items = [
            make_item(str(index), f"Item {index}", f"https://source.example/{index}", score=10 - index)
            for index in range(4)
        ]
        config = {
            "site": {"max_items": 1},
            "candidates_handoff": {"candidate_limit": 2, "ttl_hours": 2, "timezone": "Not/AZone"},
        }
        first = build_handoff(items, config, NOW, edition="daily", collection_counts={"ranked_items": 4})
        second = build_handoff(items, config, NOW, edition="daily", collection_counts={"ranked_items": 4})
        self.assertEqual(first["batch_id"], second["batch_id"])
        self.assertEqual(first["timezone"], "UTC")
        self.assertEqual(first["max_items"], 1)
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first["collection_counts"]["candidate_items"], 2)
        self.assertEqual(first["expires_at"], "2026-07-16T05:00:00+00:00")

    def test_pipeline_explicitly_disables_configured_scout_without_warning(self):
        item = make_item(
            "rss-only",
            "Aircraft engine update",
            "https://engine.example/update",
        )
        config = {
            "site": {"max_items": 1},
            "openclaw_scout": {"enabled": True, "path": "/missing/scout.json"},
        }
        pooled = []
        marked = []
        warnings = []

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = run_pipeline(
                config,
                vault_path=root / "vault",
                output_path=root / "candidates.json",
                scout_enabled=False,
                now=NOW,
                rss_collector=lambda _config, _now: [item],
                scout_loader=lambda *_args, **_kwargs: self.fail("scout loader called"),
                pool_factory=lambda path: FakePool(path, pooled, marked),
                adaptive_ranker=lambda items, _config, _learning_db: list(items),
                warn=warnings.append,
            )

        self.assertEqual(payload["collection_counts"]["scout_items"], 0)
        self.assertEqual(payload["collection_counts"]["collected_items"], 1)
        self.assertEqual(warnings, [])

    def test_pipeline_pools_but_does_not_reoffer_reviewed_items(self):
        reviewed = make_item(
            "reviewed",
            "Previously reviewed CAE item",
            "https://engineering.example/reviewed",
        )
        fresh = make_item(
            "fresh",
            "Fresh CAE item",
            "https://engineering.example/fresh",
        )
        pooled = []
        marked = []

        with tempfile.TemporaryDirectory() as directory:
            from scripts.adaptive_learning import open_database, record_editorial_feedback

            root = Path(directory)
            database = root / "learning.sqlite3"
            connection = open_database(database)
            record_editorial_feedback(
                connection,
                "editorial:reviewed",
                "content/daily/reviewed.md",
                [{
                    "id": "reviewed",
                    "relevance": 0.2,
                    "quality": 0.3,
                    "novelty": 0.1,
                    "reason": "Already assessed and not suitable.",
                }],
                [],
            )
            connection.close()
            payload = run_pipeline(
                {"site": {"max_items": 8}},
                vault_path=root / "vault",
                output_path=root / "candidates.json",
                learning_db=database,
                now=NOW,
                rss_collector=lambda _config, _now: [reviewed, fresh],
                pool_factory=lambda path: FakePool(path, pooled, marked),
                adaptive_ranker=lambda items, _config, _learning_db: list(items),
            )

        self.assertEqual({item.id for item in pooled}, {"reviewed", "fresh"})
        self.assertEqual([item["id"] for item in payload["items"]], ["fresh"])

    def test_actual_adaptive_ranking_registers_learning_provenance(self):
        item = make_item(
            "learn-me",
            "Aircraft engine digital twin",
            "https://research.engine.example/paper/1",
            source="Engine Research",
            source_kind="paper",
            query="engine digital twin",
            tags=["propulsion"],
        )
        config = {
            "learning": {"max_per_source": 5},
            "research": {
                "domain_groups": {"aircraft_engines": ["engine.example"]},
                "priority_areas": [{
                    "name": "engine_design",
                    "keywords": ["aircraft engine", "digital twin"],
                    "weight": 1,
                }],
            },
            "topics": [{"keywords": ["digital twin"]}],
        }
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "learning.sqlite3"
            ranked = _adaptive_rank([item], config, database, warn=self.fail)
            import sqlite3

            with closing(sqlite3.connect(database)) as connection:
                bindings = set(connection.execute(
                    "SELECT kind,name FROM item_signal_bindings WHERE item_id='learn-me'"
                ).fetchall())

        self.assertEqual(len(ranked), 1)
        self.assertIn(("source", "engine research"), bindings)
        self.assertIn(("keyword", "digital twin"), bindings)
        self.assertIn(("query", "engine digital twin"), bindings)
        self.assertIn(("domain_group", "aircraft_engines"), bindings)
        self.assertIn(("priority_area", "engine_design"), bindings)
        self.assertEqual(ranked[0].metadata["candidate_signals"]["domain_groups"], ["aircraft_engines"])
        self.assertIn("learning", ranked[0].metadata)


if __name__ == "__main__":
    unittest.main()
