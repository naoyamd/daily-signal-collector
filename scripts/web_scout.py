"""Validate a bounded local JSON handoff produced by OpenClaw.

The scout runs in a separate, network-enabled process.  This module is the
trust boundary on the collector VPS: it only reads the local handoff, applies
size/type/freshness/coverage limits, and converts safe metadata into
:class:`scripts.models.Item` instances.  It never fetches a URL itself.

``daily-signal-scout/v1`` remains accepted by the normal pipeline.  New
handoffs should use ``daily-signal-scout/v2``; callers that need a hard
contract (and the ``validate`` CLI) pass ``strict=True``.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import math
import sys
from collections.abc import Callable, Mapping, MutableMapping, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from scripts.models import Item, canonical_url, clean_text, item_id, normalize_doi


SCOUT_SCHEMA_V1 = "daily-signal-scout/v1"
SCOUT_SCHEMA_V2 = "daily-signal-scout/v2"
# ``SCOUT_SCHEMA`` is the current writer schema.  ``SCOUT_SCHEMA_V1`` is kept
# explicit so old fixtures and integrations can continue to name v1.
SCOUT_SCHEMA = SCOUT_SCHEMA_V2
DEFAULT_SCHEMA = SCOUT_SCHEMA_V1

DEFAULT_MAX_BYTES = 2_000_000
HARD_MAX_BYTES = 8_000_000
DEFAULT_MAX_ITEMS = 80
HARD_MAX_ITEMS = 200
DEFAULT_MAX_AGE_HOURS = 36.0
MAX_EXCERPT_CHARS = 400
MAX_PUBLISHED_FUTURE_SKEW = timedelta(hours=6)

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

# The v2 envelope is deliberately small.  Item fields are validated for type
# and bounded below, but aliases used by older scout prompts remain accepted.
_V2_TOP_LEVEL_FIELDS = {
    "schema",
    "generated_at",
    "items",
    "searched_queries",
    "warnings",
    "checked_sources",
    "research_plan",
}
_ITEM_FIELDS = {
    "title",
    "headline",
    "url",
    "link",
    "source",
    "source_kind",
    "category",
    "published_at",
    "published",
    "date",
    "excerpt",
    "summary",
    "description",
    "doi",
    "authors",
    "query",
    "tags",
    "topics",
    "organization",
    "company",
    "document_type",
    "type",
    "language",
    "score",
    "author",
    # Full content is never retained; accepting it as an ignored field keeps
    # v1 compatibility while strict v2 reports it as a contract violation.
    "full_text",
}
_COVERAGE_STATUSES = {"found", "no_new_finding", "unreachable"}


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
        parsed = default
    return parsed if math.isfinite(parsed) else default


def _utc(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    # A timezone is required for generated_at.  Published dates remain
    # backwards-compatible and are treated as UTC when they are naive.
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _parse_published_timestamp(value: Any) -> datetime | None:
    """Parse an item date, allowing date-only and naive values as UTC."""

    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc)


def _published(value: Any, now: datetime) -> tuple[str, str]:
    parsed = _parse_published_timestamp(value)
    if parsed is None:
        quality = "unknown" if value in (None, "") else "invalid"
        return "", quality
    if parsed > now + MAX_PUBLISHED_FUTURE_SKEW:
        return "", "future_rejected"
    return parsed.isoformat(), "reported"


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


def _diagnostic_list(diagnostics: MutableMapping[str, Any] | None, name: str) -> list[str]:
    if diagnostics is None:
        return []
    values = diagnostics.setdefault(name, [])
    if not isinstance(values, list):
        values = []
        diagnostics[name] = values
    return values


def _issue(
    warning: Callable[[str], None],
    diagnostics: MutableMapping[str, Any] | None,
    message: str,
    *,
    error: bool = False,
) -> None:
    """Record a human-readable diagnostic and forward it to the caller."""

    _diagnostic_list(diagnostics, "errors" if error else "warnings").append(message)
    warning(message)


def _payload_warnings(value: Any) -> tuple[list[str], bool]:
    """Return bounded payload warnings and whether the field had a bad type."""

    if value is None:
        return [], False
    if not isinstance(value, list):
        return [], True
    result: list[str] = []
    malformed = False
    for entry in value[:50]:
        if not isinstance(entry, str):
            malformed = True
            continue
        text = clean_text(entry, 400)
        if text and text not in result:
            result.append(text)
    return result, malformed


def _normal_name(value: Any) -> str:
    return clean_text(value, 300).casefold()


def _active_source_names(research_plan: Any) -> list[str]:
    """Extract active watchlist names from common research-plan shapes."""

    if not isinstance(research_plan, Mapping):
        return []
    watchlist = research_plan.get("watchlist")
    if isinstance(watchlist, Mapping):
        raw = watchlist.get("active_sources", [])
    else:
        raw = research_plan.get("active_sources", [])
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for source in raw:
        value = source
        if isinstance(source, Mapping):
            value = source.get("name") or source.get("watchlist_id") or source.get("id")
        name = _normal_name(value)
        if name and name not in result:
            result.append(name)
    return result


def _normalise_coverage(
    raw: Any,
    *,
    strict: bool,
    warning: Callable[[str], None],
    diagnostics: MutableMapping[str, Any] | None,
) -> tuple[list[dict[str, str]], bool]:
    """Validate and sanitize ``checked_sources``.

    Each entry has a bounded ``name`` and ``status`` plus at least one of a
    bounded ``query`` or ``warning``.  The returned list is safe to retain in
    Item metadata and diagnostic JSON.
    """

    if raw is None:
        if strict:
            _issue(warning, diagnostics, "checked_sources must be an array", error=True)
            return [], False
        return [], True
    if not isinstance(raw, list):
        _issue(warning, diagnostics, "checked_sources must be an array", error=strict)
        return [], False
    coverage: list[dict[str, str]] = []
    valid = True
    for index, entry in enumerate(raw[:200]):
        if not isinstance(entry, Mapping):
            _issue(warning, diagnostics, f"checked_sources[{index}] must be an object", error=strict)
            valid = False
            continue
        name = clean_text(entry.get("name") or entry.get("source"), 300)
        status = clean_text(entry.get("status"), 50).casefold().replace("-", "_")
        query = clean_text(entry.get("query"), 240)
        note = clean_text(entry.get("warning") or entry.get("reason"), 400)
        if not name:
            _issue(warning, diagnostics, f"checked_sources[{index}].name is required", error=strict)
            valid = False
        if not status:
            _issue(warning, diagnostics, f"checked_sources[{index}].status is required", error=strict)
            valid = False
        elif status not in _COVERAGE_STATUSES:
            _issue(
                warning,
                diagnostics,
                f"checked_sources[{index}] has unsupported status: {status}",
                error=strict,
            )
            # Lenient v1/v2 loading keeps unknown statuses for forward
            # compatibility, but strict v2 has the three-value contract.
            valid = False
        missing_detail = not query and not note
        if status == "found" and not query:
            missing_detail = True
        if status in {"no_new_finding", "unreachable"} and not note:
            missing_detail = True
        if missing_detail:
            _issue(
                warning,
                diagnostics,
                f"checked_sources[{index}] requires query or warning",
                error=strict,
            )
            valid = False
        normalized = _normal_name(name)
        if normalized and any(_normal_name(old.get("name")) == normalized for old in coverage):
            _issue(
                warning,
                diagnostics,
                f"checked_sources contains duplicate source: {name}",
                error=strict,
            )
            valid = False
        if name and status:
            sanitized = {"name": name, "status": status}
            if query:
                sanitized["query"] = query
            if note:
                sanitized["warning"] = note
            coverage.append(sanitized)
    if len(raw) > 200:
        _issue(warning, diagnostics, "checked_sources exceeds configured limit 200", error=strict)
        valid = False
    return coverage, valid


def _freshness_limit_hours(
    value: Any,
    *,
    max_age: Any = None,
) -> float:
    candidate = max_age if max_age is not None else value
    if isinstance(candidate, timedelta):
        return candidate.total_seconds() / 3_600
    try:
        parsed = float(candidate)
    except (TypeError, ValueError):
        return DEFAULT_MAX_AGE_HOURS
    return parsed if math.isfinite(parsed) and parsed >= 0 else DEFAULT_MAX_AGE_HOURS


def _check_generated_at(
    payload: Mapping[str, Any],
    *,
    now: datetime,
    strict: bool,
    max_age_hours: Any,
    max_age: Any,
    warning: Callable[[str], None],
    diagnostics: MutableMapping[str, Any] | None,
) -> datetime | None:
    raw = payload.get("generated_at")
    if raw is None or raw == "":
        if strict:
            _issue(warning, diagnostics, "generated_at is required for v2", error=True)
        return None
    generated = _parse_timestamp(raw)
    if generated is None:
        _issue(warning, diagnostics, "generated_at must be an ISO 8601 timestamp with timezone", error=strict)
        return None
    if diagnostics is not None:
        diagnostics["generated_at"] = generated.isoformat()
    if generated > now:
        _issue(warning, diagnostics, "generated_at is in the future", error=strict)
    age_seconds = (now - generated).total_seconds()
    if diagnostics is not None:
        diagnostics["age_seconds"] = age_seconds
    limit = _freshness_limit_hours(max_age_hours, max_age=max_age)
    if age_seconds > limit * 3_600:
        _issue(
            warning,
            diagnostics,
            f"generated_at is stale ({age_seconds / 3_600:.2f}h > {limit:g}h)",
            error=strict,
        )
    return generated


def validate_scout_handoff(
    payload: Any,
    now: datetime | None = None,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    base_score: float = 1.1,
    warn: Callable[[str], None] | None = None,
    strict: bool = False,
    max_age_hours: float | timedelta | None = DEFAULT_MAX_AGE_HOURS,
    max_age: float | timedelta | None = None,
    freshness_hours: float | timedelta | None = None,
    max_age_seconds: float | None = None,
    research_plan: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    diagnostics: MutableMapping[str, Any] | None = None,
) -> list[Item]:
    """Validate a decoded OpenClaw handoff and return bounded metadata items.

    The default mode is intentionally backwards-compatible with v1.  Set
    ``strict=True`` to require the v2 envelope, freshness, typed coverage, and
    active-source matching.  Diagnostics are optionally accumulated in a
    caller-provided mutable mapping (the CLI uses this to emit JSON).
    """

    warning = _warning(warn)
    now = _utc(now)
    maximum = _bounded_int(max_items, DEFAULT_MAX_ITEMS, HARD_MAX_ITEMS)
    # Keep an internal error sink even when the caller only wants the Item
    # list.  Otherwise strict mode could accidentally return partial records
    # because there would be nowhere to observe envelope-level errors.
    if diagnostics is None and strict:
        diagnostics = {}
    if diagnostics is not None:
        diagnostics.setdefault("errors", [])
        diagnostics.setdefault("warnings", [])
        diagnostics.setdefault("payload_warnings", [])
        diagnostics.setdefault("item_count", 0)
        diagnostics["strict"] = bool(strict)
    if not isinstance(payload, Mapping):
        _issue(warning, diagnostics, "JSON root must be an object", error=True)
        return []

    payload_warning_list, bad_warning_type = _payload_warnings(payload.get("warnings"))
    if diagnostics is not None:
        diagnostics["payload_warnings"] = payload_warning_list
    if bad_warning_type:
        _issue(warning, diagnostics, "warnings must be an array of strings", error=strict)
    for message in payload_warning_list:
        # Preserve source-provided warnings while making them visible through
        # the normal collector warning channel and machine-readable output.
        _issue(warning, diagnostics, f"payload warning: {message}")

    schema_value = payload.get("schema")
    schema = clean_text(schema_value, 100) if schema_value is not None else ""
    effective_schema = schema or DEFAULT_SCHEMA
    if schema not in {"", SCOUT_SCHEMA_V1, SCOUT_SCHEMA_V2}:
        _issue(warning, diagnostics, f"unsupported schema: {schema}", error=True)
        return []
    if strict and schema != SCOUT_SCHEMA_V2:
        _issue(warning, diagnostics, f"strict validation requires schema {SCOUT_SCHEMA_V2}", error=True)
    if diagnostics is not None:
        diagnostics["schema"] = effective_schema

    if strict and effective_schema == SCOUT_SCHEMA_V2:
        unknown = sorted(set(payload) - _V2_TOP_LEVEL_FIELDS)
        if unknown:
            _issue(warning, diagnostics, f"unsupported top-level fields: {', '.join(map(str, unknown))}", error=True)

    # Freshness is a v2 strict contract.  Supplying max_age in normal mode is
    # useful for callers that want a diagnostic without opting into all v2
    # requirements, so we still inspect a present timestamp there.
    effective_age = freshness_hours if freshness_hours is not None else max_age_hours
    effective_max_age = max_age
    if max_age_seconds is not None and effective_max_age is None:
        effective_max_age = timedelta(seconds=max_age_seconds)
    generated_at = _check_generated_at(
        payload,
        now=now,
        strict=bool(strict and effective_schema == SCOUT_SCHEMA_V2),
        max_age_hours=effective_age,
        max_age=effective_max_age,
        warning=warning,
        diagnostics=diagnostics,
    )
    del generated_at  # Retained in diagnostics; Items use publication time.

    raw_plan: Any = research_plan or plan
    if raw_plan is None and isinstance(payload.get("research_plan"), Mapping):
        raw_plan = payload.get("research_plan")
    active_names = _active_source_names(raw_plan)
    coverage, coverage_valid = _normalise_coverage(
        payload.get("checked_sources"),
        strict=bool(strict and effective_schema == SCOUT_SCHEMA_V2),
        warning=warning,
        diagnostics=diagnostics,
    )
    checked_names = {_normal_name(entry.get("name")) for entry in coverage if entry.get("name")}
    missing_sources = [name for name in active_names if name not in checked_names]
    if missing_sources:
        pretty = ", ".join(missing_sources)
        _issue(
            warning,
            diagnostics,
            f"checked_sources missing active sources: {pretty}",
            error=bool(strict and effective_schema == SCOUT_SCHEMA_V2),
        )
    if diagnostics is not None:
        diagnostics["checked_sources"] = coverage
        diagnostics["active_sources"] = active_names
        diagnostics["missing_active_sources"] = missing_sources
        diagnostics["coverage_valid"] = bool(coverage_valid and not missing_sources)

    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        _issue(warning, diagnostics, "items must be an array", error=True)
        return []
    if len(raw_items) > maximum:
        if strict and effective_schema == SCOUT_SCHEMA_V2:
            _issue(warning, diagnostics, f"items exceeds configured limit {maximum}", error=True)
            return []
        _issue(warning, diagnostics, f"truncated {len(raw_items)} items to configured limit {maximum}")

    raw_queries = payload.get("searched_queries")
    if strict and effective_schema == SCOUT_SCHEMA_V2:
        if not isinstance(raw_queries, list) or not raw_queries:
            _issue(warning, diagnostics, "searched_queries must be a non-empty array", error=True)
        elif any(not isinstance(query, str) or not clean_text(query, 240) for query in raw_queries):
            _issue(
                warning,
                diagnostics,
                "searched_queries must contain only non-empty strings",
                error=True,
            )
    searched_queries = _strings(raw_queries, limit=20, text_limit=240)
    payload_metadata = {
        "scout_schema": effective_schema,
        "searched_queries": searched_queries,
        "warnings": payload_warning_list,
        "payload_warnings": payload_warning_list,
        "scout_warnings": payload_warning_list,
        "checked_sources": coverage,
    }
    records: list[Item] = []
    seen: set[str] = set()
    fatal_item_error = False
    for index, raw in enumerate(raw_items[:maximum]):
        if not isinstance(raw, Mapping):
            _issue(warning, diagnostics, f"discarded a non-object item at index {index}", error=strict)
            fatal_item_error = fatal_item_error or strict
            continue
        if strict and effective_schema == SCOUT_SCHEMA_V2:
            unknown_item_fields = sorted(set(raw) - _ITEM_FIELDS)
            if unknown_item_fields:
                _issue(
                    warning,
                    diagnostics,
                    f"items[{index}] has unsupported fields: {', '.join(map(str, unknown_item_fields))}",
                    error=True,
                )
                fatal_item_error = True
            if raw.get("full_text") or raw.get("content") or raw.get("body"):
                _issue(warning, diagnostics, f"items[{index}] must not include full text", error=True)
                fatal_item_error = True

        raw_url = raw.get("url") or raw.get("link")
        if not is_public_https_url(raw_url):
            _issue(warning, diagnostics, f"discarded item {index} with non-public HTTPS URL", error=strict)
            fatal_item_error = fatal_item_error or strict
            continue
        url = canonical_url(str(raw_url))
        if not url:
            _issue(warning, diagnostics, f"discarded item {index} with invalid URL", error=strict)
            fatal_item_error = fatal_item_error or strict
            continue
        title_raw = raw.get("title") or raw.get("headline") or "Untitled"
        title = clean_text(title_raw, 300) or "Untitled"
        doi = normalize_doi(raw.get("doi") or raw_url)
        identifier = item_id(url, title, doi)
        if identifier in seen:
            continue
        seen.add(identifier)

        reported_kind = clean_text(raw.get("source_kind"), 100).casefold().replace("-", "_")
        if strict and effective_schema == SCOUT_SCHEMA_V2:
            if not reported_kind:
                _issue(warning, diagnostics, f"items[{index}].source_kind is required", error=True)
                fatal_item_error = True
            elif reported_kind not in ALLOWED_SOURCE_KINDS:
                _issue(
                    warning,
                    diagnostics,
                    f"items[{index}].source_kind is unsupported: {reported_kind}",
                    error=True,
                )
                fatal_item_error = True
        source_kind = reported_kind if reported_kind in ALLOWED_SOURCE_KINDS else "business_intelligence"
        category = clean_text(raw.get("category") or "Business intelligence", 200)
        if strict and effective_schema == SCOUT_SCHEMA_V2:
            required_fields = {
                "title": clean_text(raw.get("title"), 300),
                "source": clean_text(raw.get("source"), 300),
                "category": clean_text(raw.get("category"), 200),
            }
            for field_name, field_value in required_fields.items():
                if not field_value:
                    _issue(warning, diagnostics, f"items[{index}].{field_name} is required", error=True)
                    fatal_item_error = True
        tags = _strings(raw.get("tags") or raw.get("topics"), limit=30, text_limit=100)
        tags = list(dict.fromkeys([source_kind, category, *tags]))[:50]
        authors = _strings(raw.get("authors"), limit=30, text_limit=160)
        score = _float(raw.get("score", base_score), _float(base_score, 1.1))
        published_raw = raw.get("published_at") or raw.get("published") or raw.get("date")
        published_value, published_quality = _published(published_raw, now)
        published_datetime = _parse_published_timestamp(published_raw)
        if strict and published_raw not in (None, "") and published_datetime is None:
            _issue(warning, diagnostics, f"items[{index}].published_at must be ISO 8601 or YYYY-MM-DD", error=True)
            fatal_item_error = True
        if strict and published_datetime is not None and published_datetime > now + MAX_PUBLISHED_FUTURE_SKEW:
            _issue(
                warning,
                diagnostics,
                f"items[{index}].published_at is more than 6h in the future",
                error=True,
            )
            fatal_item_error = True
        excerpt_raw = raw.get("excerpt") or raw.get("summary") or raw.get("description")
        excerpt_clean = clean_text(excerpt_raw, 100_000)
        if strict and len(excerpt_clean) > MAX_EXCERPT_CHARS:
            _issue(
                warning,
                diagnostics,
                f"items[{index}].excerpt exceeds {MAX_EXCERPT_CHARS} characters",
                error=True,
            )
            fatal_item_error = True
        metadata = {
            **payload_metadata,
            "organization": clean_text(raw.get("organization") or raw.get("company"), 300),
            "document_type": clean_text(raw.get("document_type") or raw.get("type") or reported_kind, 100),
            "language": clean_text(raw.get("language"), 30),
            "published_at_quality": published_quality,
        }
        records.append(
            Item(
                id=identifier,
                title=title,
                url=url,
                source=clean_text(raw.get("source") or "OpenClaw scout", 300),
                category=category,
                published_at=published_value,
                excerpt=excerpt_clean[:MAX_EXCERPT_CHARS],
                score=max(-100.0, min(score, 100.0)),
                source_kind=source_kind,
                doi=doi,
                authors=authors,
                query=clean_text(raw.get("query"), 240),
                tags=tags,
                metadata=metadata,
            )
        )
    if diagnostics is not None:
        diagnostics["item_count"] = len(records)
    # Strict mode is all-or-nothing: a partial handoff must not enter the
    # ranking pipeline even when some records were individually safe.
    if strict and diagnostics is not None and diagnostics.get("errors"):
        return []
    if strict and fatal_item_error:
        return []
    return records


def diagnose_scout_handoff(
    payload: Any,
    now: datetime | None = None,
    *,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_age_hours: float | timedelta | None = DEFAULT_MAX_AGE_HOURS,
    max_age: float | timedelta | None = None,
    freshness_hours: float | timedelta | None = None,
    max_age_seconds: float | None = None,
    research_plan: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return machine-readable strict validation diagnostics for a payload."""

    diagnostics: dict[str, Any] = {
        "valid": False,
        "errors": [],
        "warnings": [],
        "payload_warnings": [],
        "item_count": 0,
    }
    items = validate_scout_handoff(
        payload,
        now,
        max_items=max_items,
        strict=True,
        max_age_hours=max_age_hours,
        max_age=max_age,
        freshness_hours=freshness_hours,
        max_age_seconds=max_age_seconds,
        research_plan=research_plan,
        plan=plan,
        diagnostics=diagnostics,
    )
    diagnostics["item_count"] = len(items)
    diagnostics["valid"] = not diagnostics.get("errors")
    return diagnostics


def _load_json_path(path: str | Path, *, max_bytes: int = DEFAULT_MAX_BYTES) -> Any:
    path_obj = Path(str(path)).expanduser()
    with path_obj.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError(f"file exceeds {max_bytes} byte limit")
    return json.loads(raw.decode("utf-8"))


def load_scout_handoff(
    path_or_config: str | Path | Mapping[str, Any],
    now: datetime | None = None,
    *,
    warn: Callable[[str], None] | None = None,
    strict: bool | None = None,
    max_age_hours: float | timedelta | None = None,
    max_age: float | timedelta | None = None,
    freshness_hours: float | timedelta | None = None,
    max_age_seconds: float | None = None,
    research_plan: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
) -> list[Item]:
    """Read and validate a bounded local OpenClaw JSON handoff.

    ``path_or_config`` may be a path or a mapping containing ``path``,
    ``max_bytes``, ``max_items``, ``weight``, ``strict``, ``max_age_hours``,
    and ``research_plan``.  No URL input is accepted and no network operation
    is performed.  Strict mode is opt-in so existing v1 pipeline handoffs
    continue to load normally.
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
        payload = _load_json_path(path, max_bytes=max_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        warning(f"could not read handoff: {exc}")
        return []

    config_plan: Any = research_plan or plan
    if config_plan is None:
        config_plan = config.get("research_plan") or config.get("plan")
    if isinstance(config_plan, (str, Path)):
        try:
            config_plan = _load_json_path(config_plan, max_bytes=max_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            warning(f"could not read research plan: {exc}")
            config_plan = None
    config_strict = bool(config.get("strict", False)) if strict is None else bool(strict)
    configured_age = (
        max_age_hours
        if max_age_hours is not None
        else (
            freshness_hours
            if freshness_hours is not None
            else config.get(
                "max_age_hours",
                config.get("freshness_hours", config.get("generated_at_max_age_hours", DEFAULT_MAX_AGE_HOURS)),
            )
        )
    )
    configured_max_age = max_age if max_age is not None else config.get("max_age")
    configured_max_age_seconds = (
        max_age_seconds
        if max_age_seconds is not None
        else config.get("max_age_seconds")
    )
    return validate_scout_handoff(
        payload,
        now,
        max_items=_bounded_int(
            config.get("max_items", config.get("max_results")), DEFAULT_MAX_ITEMS, HARD_MAX_ITEMS,
        ),
        base_score=_float(config.get("weight", 1.1), 1.1),
        warn=warning,
        strict=config_strict,
        max_age_hours=configured_age,
        max_age=configured_max_age,
        freshness_hours=freshness_hours,
        max_age_seconds=configured_max_age_seconds,
        research_plan=config_plan if isinstance(config_plan, Mapping) else None,
    )


def _cli_read_payload(path: Path, max_bytes: int) -> tuple[Any, str | None]:
    try:
        return _load_json_path(path, max_bytes=max_bytes), None
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        return None, str(exc)


def _cli_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return _parse_timestamp(value)


def _build_cli_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.web_scout")
    subparsers = parser.add_subparsers(dest="command")
    validate = subparsers.add_parser("validate", help="strictly validate a scout handoff")
    validate.add_argument("path", nargs="?", type=Path, help="local scout JSON handoff")
    validate.add_argument("--path", dest="path_option", type=Path, help="alias for PATH")
    validate.add_argument("--handoff", dest="handoff", type=Path, help="alias for PATH")
    validate.add_argument("--research-plan", "--plan", dest="research_plan", type=Path)
    validate.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS)
    validate.add_argument("--max-items", type=int, default=DEFAULT_MAX_ITEMS)
    validate.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    validate.add_argument("--now", type=str, help="reference ISO 8601 time (for deterministic checks)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; stdout is always one JSON diagnostic object."""

    parser = _build_cli_parser()
    args = parser.parse_args(argv)
    if args.command != "validate":
        parser.print_usage(sys.stderr)
        return 2
    path = args.path or args.handoff or args.path_option
    if path is None:
        parser.error("validate requires PATH or --handoff")
    max_bytes = _bounded_int(args.max_bytes, DEFAULT_MAX_BYTES, HARD_MAX_BYTES, 1_024)
    payload, read_error = _cli_read_payload(path, max_bytes)
    if read_error:
        diagnostics = {
            "valid": False,
            "errors": [f"could not read handoff: {read_error}"],
            "warnings": [],
            "payload_warnings": [],
            "item_count": 0,
        }
    else:
        plan: Any = None
        plan_error: str | None = None
        if args.research_plan:
            plan, plan_error = _cli_read_payload(args.research_plan, max_bytes)
        diagnostics = diagnose_scout_handoff(
            payload,
            _cli_datetime(args.now),
            max_items=args.max_items,
            max_age_hours=args.max_age_hours,
            research_plan=plan if isinstance(plan, Mapping) else None,
        )
        if args.research_plan and plan_error is None and not isinstance(plan, Mapping):
            plan_error = "research plan JSON root must be an object"
        if plan_error:
            diagnostics["errors"].append(f"could not read research plan: {plan_error}")
            diagnostics["valid"] = False
    print(json.dumps(diagnostics, ensure_ascii=False, sort_keys=True))
    return 0 if diagnostics.get("valid") else 1


if __name__ == "__main__":  # pragma: no cover - exercised by subprocess tests
    raise SystemExit(main())
