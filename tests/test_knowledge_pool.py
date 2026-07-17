import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts.knowledge_pool import KnowledgePool, MANAGED_END
from scripts.models import Item


def front(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8").split("---", 2)[1])


class KnowledgePoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.vault = Path(self.temp.name) / "vault"
        self.pool = KnowledgePool(self.vault)
        self.now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def item(self, item_id: str, title: str, score: float = 1) -> Item:
        return Item(
            item_id,
            title,
            f"https://example.test/{item_id}",
            "Example",
            "Research",
            self.now.isoformat(),
            f"Summary of {title}",
            score,
            "journal",
            authors=["A. Researcher"],
            tags=["engineering"],
        )

    def test_all_items_are_markdown_and_searchable(self):
        paths = self.pool.ingest([self.item("selected", "Selected later"), self.item("background", "Background only")], self.now)
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.pool.report()["status"], {"pooled": 2})
        self.assertEqual([row["id"] for row in self.pool.search("Background")], ["background"])
        self.assertFalse(list(self.vault.rglob("*.json")))
        self.assertFalse(list(self.vault.rglob("*.sqlite*")))

    def test_idempotent_update_preserves_manual_notes_and_path(self):
        path = self.pool.ingest([self.item("same", "Original")], self.now)[0]
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace(MANAGED_END, MANAGED_END + "\n\n## My notes\n\nDo not erase."), encoding="utf-8")
        changed = self.item("same", "Corrected", 4.5)
        again = self.pool.ingest([changed], self.now + timedelta(hours=2))[0]
        self.assertEqual(path, again)
        self.assertEqual(len(list((self.vault / "items").rglob("*.md"))), 1)
        self.assertIn("Do not erase.", again.read_text(encoding="utf-8"))
        metadata = front(again)
        self.assertEqual(metadata["title"], "Corrected")
        self.assertEqual(metadata["first_collected_at"], self.now.isoformat())
        self.assertEqual(metadata["last_collected_at"], (self.now + timedelta(hours=2)).isoformat())

    def test_daily_index_is_an_obsidian_wikilink_and_rebuildable(self):
        item_path = self.pool.ingest([self.item("wiki", "Linked item")], self.now)[0]
        index = self.vault / "daily" / "2026" / "2026-07-16.md"
        target = item_path.relative_to(self.vault).with_suffix("").as_posix()
        self.assertIn(f"[[{target}|Linked item]]", index.read_text(encoding="utf-8"))
        index.unlink()
        self.assertEqual(self.pool.rebuild()["daily_indexes"], 1)
        self.assertTrue(index.exists())

    def test_editorial_feedback_marks_selected_rejected_and_scores(self):
        bundle = {
            "generated_at": self.now.isoformat(),
            "items": [self.item("a", "Candidate A").__dict__, self.item("b", "Candidate B").__dict__],
        }
        feedback = {
            "items": [{"id": "b"}],
            "candidate_feedback": [
                {"id": "a", "relevance": 0.2, "quality": 0.8, "novelty": 0.4, "reason": "weak fit"},
                {"id": "b", "relevance": 0.9, "quality": 0.9, "novelty": 0.7, "reason": "strong fit"},
            ],
        }
        outcome = self.pool.record_editorial_outcomes(bundle, feedback, "articles/brief.md")
        self.assertEqual(len(outcome["selected"]), 1)
        self.assertEqual(len(outcome["rejected"]), 1)
        rows = {row["id"]: row for row in self.pool.search(limit=10)}
        self.assertEqual(rows["a"]["status"], "rejected")
        self.assertEqual(rows["a"]["editorial_assessment"]["reason"], "weak fit")
        self.assertEqual(rows["b"]["status"], "selected")
        self.assertEqual(rows["b"]["editorial_assessment"]["relevance"], 0.9)
        self.assertEqual(rows["b"]["article"], "articles/brief.md")

    def test_editorial_feedback_scans_vault_a_constant_number_of_times(self):
        class CountingPool(KnowledgePool):
            def __init__(self, root: Path):
                super().__init__(root)
                self.record_scans = 0

            def _records(self):
                self.record_scans += 1
                return super()._records()

        pool = CountingPool(self.vault)
        items = [self.item(f"item-{number}", f"Candidate {number}") for number in range(18)]
        bundle = {
            "generated_at": self.now.isoformat(),
            "items": [item.__dict__ for item in items],
        }
        feedback = {
            "items": [{"id": item.id} for item in items[:4]],
            "candidate_feedback": [
                {
                    "id": item.id,
                    "relevance": 0.8,
                    "quality": 0.8,
                    "novelty": 0.7,
                    "reason": "reviewed",
                }
                for item in items
            ],
        }

        outcome = pool.record_editorial_outcomes(bundle, feedback, "articles/brief.md")

        self.assertEqual(pool.record_scans, 3)
        self.assertEqual(len(outcome["selected"]), 4)
        self.assertEqual(len(outcome["rejected"]), 14)

    def test_unsafe_input_cannot_escape_vault_and_content_is_bounded(self):
        item = self.item("../../outside", "x" * 1_000)
        path = self.pool.ingest([item], self.now)[0]
        self.assertTrue(path.is_relative_to(self.vault.resolve()))
        self.assertNotIn("..", path.name)
        self.assertEqual(len(front(path)["title"]), 300)
        with self.assertRaises(ValueError):
            self.pool._inside(self.vault.parent / "outside.md")


if __name__ == "__main__":
    unittest.main()
