"""Collection models, RSS/Atom ingestion, URL normalization, and ranking."""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


TRACKING_PREFIXES = ("utm_", "ref_", "mc_")
TRACKING_KEYS = {"fbclid", "gclid", "igshid", "cmpid", "campaign_id"}
DEFAULT_USER_AGENT = "daily-signal-collector/1.0 (+https://github.com/naoyamd/daily-signal)"
DEFAULT_FEED_MAX_BYTES = 2_000_000
HARD_FEED_MAX_BYTES = 8_000_000
DEFAULT_FEED_ATTEMPTS = 3
HARD_FEED_ATTEMPTS = 5
MAX_FUTURE_SKEW = timedelta(hours=6)
ALLOWED_FEED_CONTENT_TYPES = (
    "application/atom+xml",
    "application/feed+json",
    "application/json",
    "application/rss+xml",
    "application/xml",
    "text/xml",
)


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


def entry_datetime(entry: Mapping[str, Any], now: datetime) -> datetime | None:
    """Return a trustworthy entry timestamp, or ``None`` when it is unknown.

    Missing dates used to be replaced by the collection time, which made old
    undated pages look maximally fresh.  Keeping the uncertainty explicit lets
    ranking penalize the entry instead.
    """

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
    return None


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
        now_utc = now.replace(tzinfo=now.tzinfo or timezone.utc).astimezone(timezone.utc)
        if published is None:
            published_at = ""
            published_quality = "unknown"
        elif published > now_utc + MAX_FUTURE_SKEW:
            published_at = ""
            published_quality = "future_rejected"
        else:
            published_at = published.isoformat()
            published_quality = "reported"
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
                published_at=published_at,
                excerpt=clean_text(raw.get("summary") or raw.get("description") or "", 4_000),
                score=float(source.get("weight", 1.0) or 1.0),
                source_kind=source_kind,
                doi=doi,
                authors=_authors(raw),
                tags=tags,
                metadata={
                    "feed_url": canonical_url(str(source.get("url") or "")),
                    "feed_entry_id": clean_text(raw.get("id") or raw.get("guid") or "", 1_000),
                    "published_at_quality": published_quality,
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
    sleeper: Callable[[float], None] | None = None,
) -> list[Item]:
    """Collect all configured RSS/Atom feeds.

    ``client`` and ``feed_parser`` are injectable for offline tests.  In normal
    operation the function lazily uses HTTPX and feedparser.
    """

    if feed_parser is None:
        import feedparser  # type: ignore[import-not-found]

        feed_parser = feedparser.parse
    http_config = config.get("feed_http") if isinstance(config.get("feed_http"), Mapping) else {}
    timeout = float(http_config.get("timeout_seconds", config.get("http_timeout", 20)))
    attempts = _bounded_int(http_config.get("max_attempts"), DEFAULT_FEED_ATTEMPTS, HARD_FEED_ATTEMPTS)
    backoff = max(0.0, min(float(http_config.get("backoff_seconds", 1.0)), 30.0))
    max_bytes = _bounded_int(
        http_config.get("max_bytes"), DEFAULT_FEED_MAX_BYTES, HARD_FEED_MAX_BYTES, minimum=1_024,
    )
    sleeper = sleeper or time.sleep
    cache_path_value = http_config.get("cache_path")
    cache_path = Path(str(cache_path_value)).expanduser() if cache_path_value else None
    cache = _load_feed_cache(cache_path)
    cache_changed = False

    owned_client = client is None
    if owned_client:
        import httpx  # type: ignore[import-not-found]

        client = httpx.Client(
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
    warning = warn or (lambda message: print(f"warning: {message}", file=sys.stderr))
    items: list[Item] = []
    with client if owned_client else nullcontext(client) as active_client:
        for source in config.get("sources", []):
            if not isinstance(source, Mapping) or not source.get("url"):
                continue
            name = clean_text(source.get("name") or source.get("url"), 300)
            url = str(source["url"])
            cache_key = canonical_url(url) or url
            cached = cache.get(cache_key) if isinstance(cache.get(cache_key), Mapping) else {}
            request_headers: dict[str, str] = {}
            if cached.get("etag"):
                request_headers["If-None-Match"] = str(cached["etag"])
            if cached.get("last_modified"):
                request_headers["If-Modified-Since"] = str(cached["last_modified"])
            try:
                response = None
                replayed_items: list[Item] | None = None
                last_error: Exception | None = None
                for attempt in range(1, attempts + 1):
                    retryable = True
                    try:
                        response = (
                            active_client.get(url, headers=request_headers)
                            if request_headers
                            else active_client.get(url)
                        )
                        status_code = int(getattr(response, "status_code", 200))
                        if status_code == 304:
                            replayed_items = _cached_feed_items(cached)
                            if replayed_items is None:
                                # A validator-only cache from an older release
                                # cannot replay the feed. Fetch once without a
                                # conditional header instead of dropping items.
                                response = active_client.get(url)
                                response.raise_for_status()
                            else:
                                response = None
                            break
                        retryable = status_code in {408, 425, 429} or status_code >= 500
                        response.raise_for_status()
                        break
                    except Exception as exc:
                        last_error = exc
                        if not retryable or attempt >= attempts:
                            raise
                        sleeper(backoff * (2 ** (attempt - 1)))
                if replayed_items is not None:
                    items.extend(replayed_items)
                    continue
                if response is None:
                    if last_error is not None:
                        raise last_error
                    continue
                content = bytes(response.content)
                if len(content) > max_bytes:
                    raise ValueError(f"response exceeds {max_bytes} byte limit")
                headers = getattr(response, "headers", {})
                content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
                if content_type and content_type not in ALLOWED_FEED_CONTENT_TYPES:
                    raise ValueError(f"unexpected content type: {content_type}")
                feed = feed_parser(content)
                entries = getattr(feed, "entries", feed.get("entries", []) if isinstance(feed, Mapping) else [])
                if getattr(feed, "bozo", False) and not entries:
                    raise ValueError(str(getattr(feed, "bozo_exception", "invalid feed")))
                source_items = _feed_items(source, feed, now)
                items.extend(source_items)
                validators = {
                    "etag": clean_text(headers.get("etag"), 500),
                    "last_modified": clean_text(headers.get("last-modified"), 500),
                    # Cache only the already bounded metadata Items. Raw feed
                    # bodies are intentionally never persisted.
                    "items": [asdict(item) for item in source_items],
                }
                has_validator = bool(validators["etag"] or validators["last_modified"])
                if has_validator and validators != cached:
                    cache[cache_key] = validators
                    cache_changed = True
                elif not has_validator and cache_key in cache:
                    del cache[cache_key]
                    cache_changed = True
            except Exception as exc:  # One broken publisher must not stop the collection run.
                warning(f"could not read {name}: {exc}")
    if cache_changed:
        _write_feed_cache(cache_path, cache, warning)
    return items


def _bounded_int(value: Any, default: int, maximum: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _load_feed_cache(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _cached_feed_items(cache_entry: Mapping[str, Any]) -> list[Item] | None:
    raw_items = cache_entry.get("items")
    if not isinstance(raw_items, list) or len(raw_items) > 1_000:
        return None
    expected = set(Item.__dataclass_fields__)
    items: list[Item] = []
    for raw in raw_items:
        if not isinstance(raw, Mapping) or set(raw) != expected:
            return None
        try:
            item = Item(**raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(item.metadata, dict) or not isinstance(item.tags, list) or not isinstance(item.authors, list):
            return None
        items.append(item)
    return items


def _write_feed_cache(
    path: Path | None, cache: Mapping[str, Any], warning: Callable[[str], None],
) -> None:
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(cache, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError as exc:
        warning(f"could not update feed HTTP cache: {exc}")


def _published(value: str) -> datetime | None:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
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
        published = _published(original.published_at)
        if published is None:
            age = lookback
            date_quality_adjustment = float(research.get("unknown_date_penalty", -0.5))
        else:
            age = now_utc - published
            if age > lookback or age < -MAX_FUTURE_SKEW:
                continue
            date_quality_adjustment = 0.0
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
            + date_quality_adjustment
        )
        candidate = replace(original, score=round(score, 6))
        key = canonical_url(candidate.url) or candidate.id
        current = unique.get(key)
        if current is None or (candidate.score, candidate.published_at) > (current.score, current.published_at):
            unique[key] = candidate
    return sorted(unique.values(), key=lambda item: (item.score, item.published_at, item.id), reverse=True)
