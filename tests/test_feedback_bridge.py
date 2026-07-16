import json
import tempfile
import unittest
from pathlib import Path

from scripts.feedback_bridge import apply_feedback
from scripts.knowledge_pool import KnowledgePool
from scripts.models import Item


class FeedbackBridgeTests(unittest.TestCase):
    def test_applies_versioned_publisher_event_to_vault(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            vault = root / "vault"
            candidates = root / "candidates"
            feedback = root / "feedback"
            candidates.mkdir()
            feedback.mkdir()
            item = Item(
                "item-1", "Title", "https://example.com/one", "Source", "Tech",
                "2026-07-16T00:00:00+00:00", "Summary",
            )
            KnowledgePool(vault).ingest([item])
            (candidates / "batch.json").write_text(json.dumps({
                "schema": "daily-signal-candidates/v1",
                "batch_id": "batch-1",
                "generated_at": "2026-07-16T00:00:00+00:00",
                "items": [item.__dict__],
            }), encoding="utf-8")
            (feedback / "event.json").write_text(json.dumps({
                "schema": "daily-signal-feedback/v1",
                "type": "editorial",
                "event_id": "editorial:one",
                "batch_id": "batch-1",
                "article_id": "content/daily/article.md",
                "selected_ids": ["item-1"],
                "candidate_feedback": [{
                    "id": "item-1", "relevance": 0.9, "quality": 0.8,
                    "novelty": 0.7, "reason": "useful",
                }],
            }), encoding="utf-8")
            report = apply_feedback(vault, feedback, candidates)
            pool_report = KnowledgePool(vault).report()
        self.assertEqual(report["applied"], 1)
        self.assertEqual(report["errors"], [])
        self.assertEqual(pool_report["status"], {"selected": 1})


if __name__ == "__main__":
    unittest.main()
