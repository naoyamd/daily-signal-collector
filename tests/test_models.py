import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from scripts.models import Item, canonical_url, collect, item_id, normalize_doi, rank


class FakeResponse:
    def __init__(self, content=b"feed", *, status_code=200, headers=None):
        self.content = content
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def get(self, _url):
        return FakeResponse()


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, headers=None):
        self.calls.append((url, headers or {}))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


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
        self.assertEqual(items[0].metadata["published_at_quality"], "reported")

    def test_collect_retries_bounds_responses_and_uses_conditional_get(self):
        feed = SimpleNamespace(entries=[{
            "title": "Retried feed item",
            "link": "https://journal.test/retried",
            "published": "2026-07-16T08:00:00Z",
        }], bozo=False)
        config = {
            "feed_http": {"max_attempts": 2, "backoff_seconds": 0, "max_bytes": 1_024},
            "sources": [{"name": "Resilient feed", "url": "https://feeds.test/rss"}],
        }
        client = SequenceClient([
            FakeResponse(status_code=503),
            FakeResponse(headers={"content-type": "application/rss+xml", "etag": '"v1"'}),
        ])
        with TemporaryDirectory() as directory:
            cache_path = Path(directory) / "feed-cache.json"
            config["feed_http"]["cache_path"] = str(cache_path)
            items = collect(config, self.now, client=client, feed_parser=lambda _content: feed)
            conditional = SequenceClient([FakeResponse(status_code=304)])
            unchanged = collect(config, self.now, client=conditional, feed_parser=lambda _content: feed)

        self.assertEqual([item.title for item in items], ["Retried feed item"])
        self.assertEqual(len(client.calls), 2)
        self.assertEqual([item.title for item in unchanged], ["Retried feed item"])
        self.assertEqual(conditional.calls[0][1], {"If-None-Match": '"v1"'})

        warnings = []
        oversized = SequenceClient([FakeResponse(content=b"x" * 1_025)])
        result = collect(
            config | {"feed_http": {"max_attempts": 1, "max_bytes": 1_024}},
            self.now,
            client=oversized,
            feed_parser=lambda _content: feed,
            warn=warnings.append,
        )
        self.assertEqual(result, [])
        self.assertIn("response exceeds 1024 byte limit", warnings[0])

        missing = SequenceClient([FakeResponse(status_code=404), FakeResponse()])
        collect(config, self.now, client=missing, feed_parser=lambda _content: feed, warn=lambda _message: None)
        self.assertEqual(len(missing.calls), 1)

    def test_unknown_and_future_feed_dates_are_not_treated_as_current(self):
        feed = SimpleNamespace(entries=[
            {"title": "Undated", "link": "https://journal.test/undated"},
            {
                "title": "Future",
                "link": "https://journal.test/future",
                "published": "2026-07-18T09:00:00Z",
            },
        ], bozo=False)
        items = collect(
            {"sources": [{"name": "Dates", "url": "https://feeds.test/dates"}]},
            self.now,
            client=FakeClient(),
            feed_parser=lambda _content: feed,
        )

        self.assertEqual([item.published_at for item in items], ["", ""])
        self.assertEqual(
            [item.metadata["published_at_quality"] for item in items],
            ["unknown", "future_rejected"],
        )

        dated = Item(
            "dated", "Dated", "https://journal.test/dated", "A", "R",
            self.now.isoformat(), "", 1.0,
        )
        ranked = rank([items[0], dated], {"site": {"lookback_hours": 24}}, set(), self.now)
        self.assertEqual([item.id for item in ranked], ["dated", items[0].id])

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
