"""Validate a bounded local JSON handoff produced by OpenClaw.

This module intentionally contains no web client or site-specific parser.
OpenClaw performs web discovery; the collector only accepts its local, atomic
JSON handoff and converts safe metadata into :class:`scripts.models.Item`.
"""

from __future__ import annotations

import ipaddress
import json
import math
import sys
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.models import Item, canonical_url, clean_text, item_id, normalize_doi


SCOUT_SCHEMA = "daily-signal-scout/v1"
DEFAULT_MAX_BYTES = 2_000_000
HARD_MAX_BYTES = 8_000_000
DEFAULT_MAX_ITEMS = 80
HARD_MAX_ITEMS = 200
ALLOWED_SOURCE_KINDS = {
    "press_release",
    "technical_report",
    "paper",
    "standard",
    "official",
    "news",
    "journal",
    "conference",
    "corporate_tech",
}


def is_public_https_url(value: Any) -> bool:
    """Return whether a scout URL looks public without performing DNS lookup."""

    try:
        parts = urlsplit(str(value or "").strip())
        hostname = (parts.hostname or "").rstrip(".").lower()
        # Accessing port also rejects malformed values such as ``:not-a-port``.
        _ = parts.port
    except ValueError:
        return False
    if parts.scheme.lower() != "https" or not hostname or parts.username or parts.password:
        return False
    if hostname == "localhost" or hostname.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return "." in hostname
    return address.is_global


def _bounded_int(value: Any, default: int, hard_maximum: int, minimum: int = 1) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = default
    return max(minimum, min(result, hard_maximum))


def _float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _published(value: Any, now: datetime) -> str:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return now.isoformat()
    parsed = parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _strings(value: Any, *, limit: int = 30, text_limit: int = 160) -> list[str]:
    raw = value if isinstance(value, list) else []
    result: list[str] = []
    for entry in raw[:limit]:
        text = clean_text(entry, text_limit)
        if text and text not in result:
            result.append(text)
    return result


def _warning(warn: Callable[[str], None] | None) -> Callable[[str], None]:
    return warn or (lambda message: print(f"warning: openclaw scout: {message}", file=sys.stderr))


def validate_scout_handoff(
    payload: Any,
    now: datetime | None = None,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    base_score: float = 1.1,
    warn: Callable[[str], None] | None = None,
) -> list[Item]:
    """Validate a decoded OpenClaw handoff and return bounded metadata items."""

    warning = _warning(warn)
    now = _utc(now)
    maximum = _bounded_int(max_items, DEFAULT_MAX_ITEMS, HARD_MAX_ITEMS)
    if not isinstance(payload, Mapping):
        warning("JSON root must be an object")
        return []
    schema = clean_text(payload.get("schema"), 100)
    if schema and schema != SCOUT_SCHEMA:
        warning(f"unsupported schema: {schema}")
        return []
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        warning("items must be an array")
        return []
    if len(raw_items) > maximum:
        warning(f"truncated {len(raw_items)} items to configured limit {maximum}")

    searched_queries = _strings(payload.get("searched_queries"), limit=20, text_limit=240)
    records: list[Item] = []
    seen: set[str] = set()
    for raw in raw_items[:maximum]:
        if not isinstance(raw, Mapping):
            warning("discarded a non-object item")
            continue
        raw_url = raw.get("url") or raw.get("link")
        if not is_public_https_url(raw_url):
            warning("discarded item with non-public HTTPS URL")
            continue
        url = canonical_url(str(raw_url))
        if not url:
            warning("discarded item with invalid URL")
            continue
        title = clean_text(raw.get("title") or raw.get("headline") or "Untitled", 300) or "Untitled"
        doi = normalize_doi(raw.get("doi") or raw_url)
        identifier = item_id(url, title, doi)
        if identifier in seen:
            continue
        seen.add(identifier)

        reported_kind = clean_text(raw.get("source_kind"), 100).casefold().replace("-", "_")
        source_kind = reported_kind if reported_kind in ALLOWED_SOURCE_KINDS else "business_intelligence"
        category = clean_text(raw.get("category") or "Business intelligence", 200)
        tags = _strings(raw.get("tags") or raw.get("topics"), limit=30, text_limit=100)
        tags = list(dict.fromkeys([source_kind, category, *tags]))[:50]
        authors = _strings(raw.get("authors"), limit=30, text_limit=160)
        score = _float(raw.get("score", base_score), _float(base_score, 1.1))
        metadata = {
            "scout_schema": schema or SCOUT_SCHEMA,
            "organization": clean_text(raw.get("organization") or raw.get("company"), 300),
            "document_type": clean_text(raw.get("document_type") or raw.get("type") or reported_kind, 100),
            "language": clean_text(raw.get("language"), 30),
            "searched_queries": searched_queries,
        }
        records.append(
            Item(
                id=identifier,
                title=title,
                url=url,
                source=clean_text(raw.get("source") or "OpenClaw scout", 300),
                category=category,
                published_at=_published(raw.get("published_at") or raw.get("published") or raw.get("date"), now),
                excerpt=clean_text(raw.get("excerpt") or raw.get("summary") or raw.get("description"), 4_000),
                score=max(-100.0, min(score, 100.0)),
                source_kind=source_kind,
                doi=doi,
                authors=authors,
                query=clean_text(raw.get("query"), 240),
                tags=tags,
                metadata=metadata,
            )
        )
    return records


def load_scout_handoff(
    path_or_config: str | Path | Mapping[str, Any],
    now: datetime | None = None,
    *,
    warn: Callable[[str], None] | None = None,
) -> list[Item]:
    """Read and validate a bounded local OpenClaw JSON handoff.

    ``path_or_config`` may be a path or a mapping containing ``path``,
    ``max_bytes``, ``max_items``, and ``weight``.  No URL input is accepted and
    no network operation is performed.
    """

    warning = _warning(warn)
    if isinstance(path_or_config, Mapping):
        config = path_or_config
        path_value = config.get("path")
    else:
        config = {}
        path_value = path_or_config
    if not path_value:
        warning("no local handoff path configured")
        return []
    path = Path(str(path_value)).expanduser()
    max_bytes = _bounded_int(config.get("max_bytes"), DEFAULT_MAX_BYTES, HARD_MAX_BYTES, 1_024)
    try:
        with path.open("rb") as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            warning(f"handoff exceeds {max_bytes} byte limit")
            return []
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        warning(f"could not read handoff: {exc}")
        return []
    return validate_scout_handoff(
        payload,
        now,
        max_items=_bounded_int(
            config.get("max_items", config.get("max_results")), DEFAULT_MAX_ITEMS, HARD_MAX_ITEMS,
        ),
        base_score=_float(config.get("weight", 1.1), 1.1),
        warn=warning,
    )
