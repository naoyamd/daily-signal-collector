import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from scripts.models import Item, canonical_url, collect, item_id, normalize_doi, rank


class FakeResponse:
    content = b"feed"

    def raise_for_status(self):
        return None


class FakeClient:
    def get(self, _url):
        return FakeResponse()


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 16, 9, tzinfo=timezone.utc)

    def test_canonical_url_and_doi_are_stable(self):
        self.assertEqual(
            canonical_url("HTTPS://Example.com/a/?utm_source=x&keep=1#fragment"),
            "https://example.com/a?keep=1",
        )
        self.assertEqual(normalize_doi("https://doi.org/10.1234/ABC.9)."), "10.1234/abc.9")
        self.assertEqual(item_id("https://one.test", "One", "10.1234/X"), item_id("https://two.test", "Two", "doi:10.1234/x"))

    def test_collect_accepts_rss_and_atom_shaped_entries(self):
        rss = {
            "title": "RSS paper",
            "link": "https://journal.test/paper?utm_medium=rss",
            "summary": "<p>Peer reviewed result</p>",
            "published_parsed": (2026, 7, 15, 1, 2, 3, 0, 0, 0),
            "prism_doi": "10.1000/RSS",
            "authors": [{"name": "A. Researcher"}],
        }
        atom = {
            "title": "Atom release",
            "links": [{"rel": "alternate", "href": "https://company.test/release"}],
            "updated": "2026-07-16T02:00:00Z",
            "description": "Technical announcement",
            "author": "Engineering Team",
        }
        config = {
            "sources": [{
                "name": "Mixed feed",
                "url": "https://feeds.test/mixed.xml",
                "source_kind": "journal",
                "category": "Engineering",
                "weight": 1.5,
            }]
        }
        items = collect(
            config,
            self.now,
            client=FakeClient(),
            feed_parser=lambda _content: SimpleNamespace(entries=[rss, atom], bozo=False),
        )
        self.assertEqual([item.title for item in items], ["RSS paper", "Atom release"])
        self.assertEqual(items[0].doi, "10.1000/rss")
        self.assertEqual(items[0].authors, ["A. Researcher"])
        self.assertEqual(items[0].source_kind, "journal")
        self.assertNotIn("utm_", items[0].url)

    def test_rank_prioritizes_research_and_excludes_out_of_scope(self):
        config = {
            "site": {"lookback_hours": 72},
            "research": {
                "priority_keywords": ["aircraft engine", "digital twin"],
                "priority_boost": 0.6,
                "exclude_terms": ["drone"],
                "source_kind_boosts": {"journal": 0.2},
            },
        }
        engine = Item("engine", "Aircraft engine digital twin", "https://x.test/engine", "A", "R", self.now.isoformat(), "", 1, "journal")
        generic = Item("generic", "General engineering", "https://x.test/general", "A", "R", self.now.isoformat(), "", 1.8)
        drone = Item("drone", "Drone aircraft engine", "https://x.test/drone", "A", "R", self.now.isoformat(), "", 99)
        old = Item("old", "Aircraft engine", "https://x.test/old", "A", "R", (self.now - timedelta(days=5)).isoformat(), "", 99)
        ranked = rank([generic, drone, old, engine], config, set(), self.now)
        self.assertEqual([item.id for item in ranked], ["engine", "generic"])
        self.assertEqual(engine.score, 1)  # Ranking does not compound by mutating input.

    def test_rank_filters_seen_and_deduplicates_url(self):
        first = Item("first", "First", "https://x.test/same?utm_source=a", "A", "R", self.now.isoformat(), "", 1)
        better = Item("better", "Better", "https://x.test/same", "B", "R", self.now.isoformat(), "", 2)
        seen = Item("seen", "Seen", "https://x.test/seen", "A", "R", self.now.isoformat(), "", 100)
        result = rank([first, better, seen], {"site": {"lookback_hours": 24}}, {"seen"}, self.now)
        self.assertEqual([item.id for item in result], ["better"])


if __name__ == "__main__":
    unittest.main()
