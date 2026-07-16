import json
import shutil
import unittest
import uuid
from datetime import date, timedelta
from pathlib import Path

from scripts.adaptive_learning import (
    build_research_plan,
    ingest_exchange_inbox,
    learning_report,
    open_database,
    rank_candidates,
    record_explicit_feedback,
    record_publisher_feedback,
    register_candidates,
)


class AdaptiveLearningTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).parent / f".tmp-learning-{uuid.uuid4().hex}"
        self.root.mkdir()
        self.db = open_database(self.root / "learning.sqlite3")
        self.config = {
            "learning": {
                "source_weight": 0.55,
                "keyword_weight": 0.30,
                "priority_area_weight": 0.15,
                "exploration_weight": 10,
                "max_learning_bonus": 0.08,
                "editorial_reward_weight": 0.75,
                "explicit_feedback_weight": 2.0,
            },
            "research": {
                "max_queries": 4,
                "exploitation_ratio": 0.5,
                "priority_area_boost": 0.1,
                "priority_areas": [
                    {
                        "name": "aerospace engineering",
                        "keywords": ["aircraft", "CAE"],
                        "queries": ["aircraft CAE", "aerospace simulation"],
                        "weight": 1.2,
                    },
                    {"name": "materials", "keywords": ["alloy"], "queries": ["advanced alloy"]},
                ],
                "exclude_terms": ["consumer drone", "celebrity"],
                "seed_queries": ["digital twin engineering", "consumer drone news"],
                "domain_groups": {"journals": ["example.org"]},
            },
        }

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.root)

    def candidate(self, item_id="one", source="Engineering Journal", **overrides):
        value = {
            "id": item_id,
            "source": source,
            "source_kind": "journal",
            "matched_keywords": ["CAE"],
            "query": "aircraft CAE",
            "tags": ["engineering"],
            "url": "https://example.org/item",
        }
        value.update(overrides)
        return value

    def test_beta_ranking_is_deterministic_capped_and_filters_exclusions(self):
        good = self.candidate()
        register_candidates(self.db, [good], self.config)
        record_publisher_feedback(
            self.db,
            {
                "feedback_id": "publisher-1",
                "article": "published/one.json",
                "items": [{"id": "one"}],
                "candidate_feedback": [
                    {"id": "one", "relevance": 1, "quality": 0.9, "novelty": 0.8, "reason": "useful"}
                ],
            },
            self.config,
        )
        unknown = self.candidate("unknown", "Unknown", matched_keywords=["other"], query="other")
        excluded = self.candidate("excluded", title="Consumer drone launch")
        first = rank_candidates(self.db, [unknown, good, excluded], self.config)
        second = rank_candidates(self.db, [unknown, good, excluded], self.config)
        self.assertEqual(first, second)
        self.assertEqual([row["id"] for row in first], ["one", "unknown"])
        self.assertLessEqual(first[0]["exploration_bonus"], 0.08)
        self.assertIn("aerospace engineering", first[0]["matched_priority_areas"])

    def test_max_per_source_and_id_tie_break_are_stable(self):
        config = {**self.config, "learning": {**self.config["learning"], "max_per_source": 1}}
        candidates = [self.candidate("z", "Same"), self.candidate("a", "Same")]
        self.assertEqual([row["id"] for row in rank_candidates(self.db, candidates, config)], ["a"])

    def test_publisher_exchange_is_idempotent_and_never_stores_bodies(self):
        envelope = {
            "feedback_id": "exchange-1",
            "article": "publisher/article-1.json",
            "body": "THIS ARTICLE BODY MUST NOT ENTER SQLITE",
            "bundle": {
                "items": [
                    {
                        **self.candidate(),
                        "title": "Secret full title",
                        "excerpt": "Secret full abstract",
                        "body": "Secret candidate body",
                    }
                ]
            },
            "feedback": {
                "items": [{"id": "one"}],
                "candidate_feedback": [
                    {"id": "one", "relevance": 0.9, "quality": 0.8, "novelty": 0.7, "reason": "strong fit"}
                ],
            },
        }
        inbox = self.root / "exchange"
        inbox.mkdir()
        (inbox / "001.json").write_text(json.dumps(envelope), encoding="utf-8")
        first = ingest_exchange_inbox(self.db, inbox, self.config)
        second = ingest_exchange_inbox(self.db, inbox, self.config)
        self.assertEqual((first["processed"], first["errors"]), (1, []))
        self.assertEqual((second["skipped"], second["errors"]), (1, []))
        report = learning_report(self.db)
        self.assertEqual(report["publisher_feedback"][0]["selected_ids"], ["one"])
        self.assertEqual(len(report["publisher_feedback"][0]["candidate_feedback"]), 1)
        dump = " ".join(
            str(value)
            for table in ("processed_events", "editorial_runs", "explicit_feedback", "item_signal_bindings")
            for row in self.db.execute(f"SELECT * FROM {table}")
            for value in row
        )
        self.assertNotIn("THIS ARTICLE BODY", dump)
        self.assertNotIn("Secret full", dump)
        columns = " ".join(
            row[1]
            for table in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            for row in self.db.execute(f"PRAGMA table_info({table[0]})")
        )
        for forbidden in ("body", "title", "url", "excerpt", "abstract"):
            self.assertNotIn(forbidden, columns)

    def test_explicit_feedback_uses_registered_item_signals(self):
        register_candidates(self.db, [self.candidate()], self.config)
        self.assertTrue(record_explicit_feedback(self.db, "reader-1", None, "one", 1, "more like this", self.config))
        self.assertFalse(record_explicit_feedback(self.db, "reader-1", None, "one", 1, "more like this", self.config))
        report = learning_report(self.db)
        self.assertEqual(report["explicit_feedback"][0]["item_id"], "one")
        self.assertEqual(report["signals"]["source"][0]["observations"], 1)

    def test_openclaw_explicit_feedback_can_supply_safe_signals(self):
        inbox = self.root / "explicit.json"
        inbox.write_text(
            json.dumps(
                {
                    "type": "feedback",
                    "event_id": "openclaw-1",
                    "item_id": "new-item",
                    "rating": "up",
                    "note": "find more",
                    "signals": {"source": "Technical Publisher", "keywords": ["CAE"]},
                }
            ),
            encoding="utf-8",
        )
        result = ingest_exchange_inbox(self.db, inbox, self.config)
        self.assertEqual((result["processed"], result["errors"]), (1, []))
        self.assertEqual(learning_report(self.db)["signals"]["source"][0]["name"], "technical publisher")

    def test_versioned_publisher_feedback_contract_uses_ranked_bindings(self):
        rank_candidates(self.db, [self.candidate()], self.config)
        event = {
            "schema": "daily-signal-feedback/v1",
            "type": "editorial",
            "event_id": "editorial:versioned",
            "batch_id": "batch-1",
            "article_id": "content/daily/article.md",
            "selected_ids": ["one"],
            "candidate_feedback": [
                {"id": "one", "relevance": 0.9, "quality": 0.8, "novelty": 0.7, "reason": "useful"}
            ],
        }
        self.assertTrue(record_publisher_feedback(self.db, event, self.config))
        report = learning_report(self.db)
        self.assertEqual(report["publisher_feedback"][0]["article_id"], "content/daily/article.md")
        self.assertEqual(report["signals"]["source"][0]["observations"], 1)

    def test_publisher_signals_are_merged_with_registered_provenance(self):
        rank_candidates(self.db, [self.candidate()], self.config)
        event = {
            "event_id": "editorial:merged-signals",
            "article_id": "content/daily/article.md",
            "selected_ids": ["one"],
            "candidate_feedback": [
                {"id": "one", "relevance": 1, "quality": 1, "novelty": 1, "reason": "useful"}
            ],
            "candidate_signals": {
                "one": {"source": "Technical Publisher", "matched_keywords": ["CAE"]}
            },
        }
        self.assertTrue(record_publisher_feedback(self.db, event, self.config))
        report = learning_report(self.db)
        self.assertEqual(report["signals"]["source_kind"][0]["name"], "journal")
        self.assertEqual(report["signals"]["source_kind"][0]["observations"], 1)

    def test_plan_includes_priority_exclusions_and_exploit_explore_queries(self):
        register_candidates(self.db, [self.candidate()], self.config)
        record_explicit_feedback(self.db, "liked", None, "one", 1, config=self.config)
        plan = build_research_plan(self.db, self.config, date(2026, 7, 16))
        self.assertEqual(plan["exclude_terms"], ["consumer drone", "celebrity"])
        self.assertNotIn("consumer drone news", plan["queries"])
        self.assertEqual({row["strategy"] for row in plan["schedule"]}, {"high_reward", "underexplored"})
        self.assertEqual(plan["priority_areas"][0]["name"], "aerospace engineering")

    def test_watchlist_rotates_all_49_sources_deterministically(self):
        config = {
            **self.config,
            "research": {
                **self.config["research"],
                "watchlist_coverage_days": 7,
                "watchlist_rotation_days": 1,
                "must_check_sources": [
                    {
                        "name": f"Publisher {index:02d}",
                        "category": "technical",
                        "domains": [f"p{index}.test"],
                    }
                    for index in range(49)
                ],
            },
        }
        start = date(2026, 7, 1)
        first_plan = build_research_plan(self.db, config, start)
        first = first_plan["watchlist"]["active_sources"]
        repeated = build_research_plan(self.db, config, start)["watchlist"]["active_sources"]
        self.assertEqual(first, repeated)
        observed = {
            row["watchlist_id"]
            for offset in range(7)
            for row in build_research_plan(self.db, config, start + timedelta(days=offset))["watchlist"]["active_sources"]
        }
        self.assertEqual(len(first), 7)
        self.assertEqual(len(observed), 49)
        self.assertEqual(first_plan["watchlist"]["total_sources"], 49)
        self.assertIn("domains", first[0])
        self.assertNotEqual(
            first,
            build_research_plan(self.db, config, start + timedelta(days=1))["watchlist"]["active_sources"],
        )

    def test_rebuild_from_exchange_events_produces_same_learning_report(self):
        event = {
            "feedback_id": "rebuild-1",
            "article": "publisher/a.json",
            "bundle": {"items": [self.candidate()]},
            "items": [{"id": "one"}],
            "candidate_feedback": [
                {"id": "one", "relevance": 0.8, "quality": 0.7, "novelty": 0.6, "reason": "good"}
            ],
        }
        inbox = self.root / "feedback.json"
        inbox.write_text(json.dumps(event), encoding="utf-8")
        ingest_exchange_inbox(self.db, inbox, self.config)
        expected = learning_report(self.db)
        rebuilt = open_database(self.root / "rebuilt.sqlite3")
        try:
            ingest_exchange_inbox(rebuilt, inbox, self.config)
            self.assertEqual(learning_report(rebuilt), expected)
        finally:
            rebuilt.close()


if __name__ == "__main__":
    unittest.main()
