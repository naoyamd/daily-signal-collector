"""Collection models, RSS/Atom ingestion, URL normalization, and ranking."""

from __future__ import annotations

import hashlib
import html
import re
import sys
from contextlib import nullcontext
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PREFIXES = ("utm_", "ref_", "mc_")
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "cmpid", "campaign_id"}
DEFAULT_USER_AGENT = "daily-signal-collector/1.0 (+https://github.com/naoyamd/daily-signal)"


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    category: str
    published_at: str
    excerpt: str
    score: float = 0.0
    source_kind: str = "feed"
    doi: str = ""
    authors: list[str] = field(default_factory=list)
    query: str = ""
    tags: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


def clean_text(value: Any, limit: int = 700) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, limit)]


def canonical_url(url: str) -> str:
    """Return a stable HTTP(S) URL without fragments or tracking parameters."""

    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        return ""
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(TRACKING_PREFIXES) and key.lower() not in TRACKING_KEYS
    ]
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, urlencode(query), ""))


def normalize_doi(value: Any) -> str:
    text = clean_text(value, 300).lower()
    text = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", text, flags=re.I)
    text = text.rstrip(".,;:)]}")
    return text if re.match(r"^10\.\d{4,9}/\S+$", text) else ""


def item_id(url: str, title: str, doi: str = "") -> str:
    normalized_doi = normalize_doi(doi)
    key = f"doi:{normalized_doi}" if normalized_doi else canonical_url(url)
    key = key or clean_text(title, 500).casefold()
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


def entry_datetime(entry: Mapping[str, Any], now: datetime) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
    for name in ("published", "updated", "created"):
        raw = entry.get(name)
        if not raw:
            continue
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            return value.replace(tzinfo=value.tzinfo or timezone.utc).astimezone(timezone.utc)
        except ValueError:
            continue
    return now.astimezone(timezone.utc)


def _entry_url(entry: Mapping[str, Any]) -> str:
    direct = canonical_url(str(entry.get("link") or ""))
    if direct:
        return direct
    links = entry.get("links") or []
    if isinstance(links, list):
        for link in links:
            if not isinstance(link, Mapping) or link.get("rel", "alternate") != "alternate":
                continue
            candidate = canonical_url(str(link.get("href") or ""))
            if candidate:
                return candidate
    return ""


def _entry_doi(entry: Mapping[str, Any]) -> str:
    for value in (
        entry.get("prism_doi"),
        entry.get("doi"),
        entry.get("dc_identifier"),
        entry.get("id"),
    ):
        doi = normalize_doi(value)
        if doi:
            return doi
    return ""


def _authors(entry: Mapping[str, Any]) -> list[str]:
    result: list[str] = []
    raw_authors = entry.get("authors")
    if isinstance(raw_authors, list):
        for author in raw_authors:
            name = author.get("name") if isinstance(author, Mapping) else author
            clean = clean_text(name, 160)
            if clean and clean not in result:
                result.append(clean)
    elif entry.get("author"):
        result.extend(name.strip() for name in clean_text(entry["author"], 1_000).split(",") if name.strip())
    return result[:30]


def _feed_items(source: Mapping[str, Any], feed: Any, now: datetime) -> list[Item]:
    result: list[Item] = []
    entries = getattr(feed, "entries", None)
    if entries is None and isinstance(feed, Mapping):
        entries = feed.get("entries", [])
    for raw in entries or []:
        if not isinstance(raw, Mapping):
            continue
        title = clean_text(raw.get("title") or "Untitled", 300)
        url = _entry_url(raw)
        doi = _entry_doi(raw)
        if not url and doi:
            url = f"https://doi.org/{doi}"
        if not url:
            continue
        published = entry_datetime(raw, now)
        source_kind = clean_text(source.get("source_kind") or "feed", 100)
        category = clean_text(source.get("category") or "Uncategorized", 200)
        configured_tags = source.get("tags") if isinstance(source.get("tags"), list) else []
        tags = list(dict.fromkeys(filter(None, [source_kind, category, *map(str, configured_tags)])))[:50]
        result.append(
            Item(
                id=item_id(url, title, doi),
                title=title,
                url=url,
                source=clean_text(source.get("name") or source.get("url") or "Unknown source", 300),
                category=category,
                published_at=published.isoformat(),
                excerpt=clean_text(raw.get("summary") or raw.get("description") or "", 20_000),
                score=float(source.get("weight", 1.0) or 1.0),
                source_kind=source_kind,
                doi=doi,
                authors=_authors(raw),
                tags=tags,
                metadata={
                    "feed_url": canonical_url(str(source.get("url") or "")),
                    "feed_entry_id": clean_text(raw.get("id") or raw.get("guid") or "", 1_000),
                },
            )
        )
    return result


def collect(
    config: Mapping[str, Any],
    now: datetime,
    *,
    client: Any | None = None,
    feed_parser: Callable[[bytes], Any] | None = None,
    warn: Callable[[str], None] | None = None,
) -> list[Item]:
    """Collect all configured RSS/Atom feeds.

    ``client`` and ``feed_parser`` are injectable for offline tests.  In normal
    operation the function lazily uses HTTPX and feedparser.
    """

    if feed_parser is None:
        import feedparser  # type: ignore[import-not-found]

        feed_parser = feedparser.parse
    owned_client = client is None
    if owned_client:
        import httpx  # type: ignore[import-not-found]

        client = httpx.Client(
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=float(config.get("http_timeout", 20)),
            follow_redirects=True,
        )
    warning = warn or (lambda message: print(f"warning: {message}", file=sys.stderr))
    items: list[Item] = []
    with client if owned_client else nullcontext(client) as active_client:
        for source in config.get("sources", []):
            if not isinstance(source, Mapping) or not source.get("url"):
                continue
            name = clean_text(source.get("name") or source.get("url"), 300)
            try:
                response = active_client.get(str(source["url"]))
                response.raise_for_status()
                feed = feed_parser(response.content)
                entries = getattr(feed, "entries", feed.get("entries", []) if isinstance(feed, Mapping) else [])
                if getattr(feed, "bozo", False) and not entries:
                    raise ValueError(str(getattr(feed, "bozo_exception", "invalid feed")))
                items.extend(_feed_items(source, feed, now))
            except Exception as exc:  # One broken publisher must not stop the collection run.
                warning(f"could not read {name}: {exc}")
    return items


def _published(value: str, fallback: datetime) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return fallback
    return result.replace(tzinfo=result.tzinfo or timezone.utc).astimezone(timezone.utc)


def rank(items: Iterable[Item], config: Mapping[str, Any], seen: set[str], now: datetime) -> list[Item]:
    """Filter and rank signals, including explicit research-priority terms."""

    site = config.get("site", {}) if isinstance(config.get("site"), Mapping) else {}
    research = config.get("research", {}) if isinstance(config.get("research"), Mapping) else {}
    lookback = timedelta(hours=max(1, int(site.get("lookback_hours", 168))))
    topic_groups = config.get("topics", []) if isinstance(config.get("topics"), list) else []
    priority_terms = [str(term).casefold() for term in research.get("priority_keywords", [])]
    exclude_terms = [str(term).casefold() for term in research.get("exclude_terms", [])]
    priority_boost = float(research.get("priority_boost", 0.3))
    kind_boosts = research.get("source_kind_boosts", {})
    if not isinstance(kind_boosts, Mapping):
        kind_boosts = {}
    now_utc = now.replace(tzinfo=now.tzinfo or timezone.utc).astimezone(timezone.utc)
    unique: dict[str, Item] = {}
    for original in items:
        if original.id in seen:
            continue
        published = _published(original.published_at, now_utc)
        age = now_utc - published
        if age > lookback:
            continue
        haystack = f"{original.title} {original.excerpt} {' '.join(original.tags)}".casefold()
        if any(term and term in haystack for term in exclude_terms):
            continue
        topic_hits = 0
        for topic in topic_groups:
            if isinstance(topic, Mapping):
                topic_hits += sum(1 for term in topic.get("keywords", []) if str(term).casefold() in haystack)
        priority_hits = sum(1 for term in priority_terms if term and term in haystack)
        age_hours = max(0.0, age.total_seconds() / 3_600)
        recency = max(0.0, 1.0 - age_hours / max(lookback.total_seconds() / 3_600, 1.0))
        score = (
            float(original.score)
            + min(topic_hits, 5) * float(research.get("topic_boost", 0.35))
            + min(priority_hits, 8) * priority_boost
            + float(kind_boosts.get(original.source_kind, 0.0) or 0.0)
            + recency
        )
        candidate = replace(original, score=round(score, 6))
        key = canonical_url(candidate.url) or candidate.id
        current = unique.get(key)
        if current is None or (candidate.score, candidate.published_at) > (current.score, current.published_at):
            unique[key] = candidate
    return sorted(unique.values(), key=lambda item: (item.score, item.published_at, item.id), reverse=True)
