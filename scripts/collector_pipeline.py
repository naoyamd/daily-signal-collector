"""Run collection, pool all signals, rank candidates, and write the handoff."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts.models import Item, collect, rank
from scripts.web_scout import load_scout_handoff


CANDIDATES_SCHEMA = "daily-signal-candidates/v1"
DEFAULT_PUBLICATION_MAX_ITEMS = 8
DEFAULT_CANDIDATE_LIMIT = 30
HARD_MAX_ITEMS = 200
DEFAULT_TTL_HOURS = 24
HARD_TTL_HOURS = 168


def _warn(message: str) -> None:
    print(f"warning: collector pipeline: {message}", file=sys.stderr)


def _bounded_int(value: Any, default: int, maximum: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _aware(value: datetime | None) -> datetime:
    value = value or datetime.now(timezone.utc)
    return value.replace(tzinfo=value.tzinfo or timezone.utc)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _research(config: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = config.get("research")
    if isinstance(direct, Mapping):
        return direct
    adaptive = _mapping(config.get("adaptive_learning"))
    return _mapping(adaptive.get("research"))


def _matched_keywords(item: Item, config: Mapping[str, Any]) -> list[str]:
    haystack = f"{item.title} {item.excerpt} {' '.join(item.tags)}".casefold()
    matched: list[str] = []
    for topic in config.get("topics", []) if isinstance(config.get("topics"), list) else []:
        if not isinstance(topic, Mapping):
            continue
        for raw in topic.get("keywords", []) if isinstance(topic.get("keywords"), list) else []:
            keyword = str(raw).strip()
            if keyword and keyword.casefold() in haystack and keyword not in matched:
                matched.append(keyword)
    return matched[:50]


def _configured_domain_groups(config: Mapping[str, Any]) -> dict[str, list[str]]:
    candidates: list[Any] = [_research(config).get("domain_groups")]
    adaptive = _mapping(config.get("adaptive_learning"))
    candidates.append(_mapping(adaptive.get("research")).get("domain_groups"))
    merged: dict[str, list[str]] = {}
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        for raw_group, raw_domains in candidate.items():
            if isinstance(raw_domains, (str, bytes)) or not isinstance(raw_domains, Sequence):
                continue
            group = str(raw_group).strip()
            if not group:
                continue
            domains = merged.setdefault(group, [])
            for raw_domain in raw_domains:
                text = str(raw_domain or "").strip().casefold()
                if not text:
                    continue
                parsed = urlsplit(text if "://" in text else f"//{text}")
                domain = (parsed.hostname or text.split("/", 1)[0]).lstrip("*.").rstrip(".")
                if domain and domain not in domains:
                    domains.append(domain)
    return merged


def _matched_domain_groups(item: Item, config: Mapping[str, Any]) -> list[str]:
    host = (urlsplit(item.url).hostname or "").casefold().rstrip(".")
    if not host:
        return []
    return [
        group
        for group, domains in _configured_domain_groups(config).items()
        if any(host == domain or host.endswith(f".{domain}") for domain in domains)
    ]


def _candidate_signals(item: Item, config: Mapping[str, Any]) -> dict[str, Any]:
    matched = _matched_keywords(item, config)
    groups = _matched_domain_groups(item, config)
    return {
        "source": item.source,
        "keywords": matched,
        "queries": [item.query] if item.query else [],
        "domain_groups": groups,
        "tags": list(dict.fromkeys(str(tag) for tag in item.tags if str(tag).strip()))[:50],
    }


def _is_excluded(item: Item, config: Mapping[str, Any]) -> bool:
    terms = config.get("exclude_terms", _research(config).get("exclude_terms", []))
    if isinstance(terms, (str, bytes)) or not isinstance(terms, Sequence):
        return False
    haystack = f"{item.title} {item.excerpt} {item.source} {item.category} {' '.join(item.tags)}".casefold()
    for raw_term in terms:
        term = str(raw_term).strip().casefold()
        if not term:
            continue
        if re.fullmatch(r"[a-z0-9]+", term):
            # Do not treat ``uas`` inside ``quasi-steady`` as the UAS token.
            # A trailing plural ``s`` is accepted for terms such as UAV/drone.
            pattern = rf"(?<![a-z0-9]){re.escape(term)}s?(?![a-z0-9])"
            if re.search(pattern, haystack, flags=re.I):
                return True
        elif term in haystack:
            # Japanese and intentional multiword phrases use substring match.
            return True
    return False


def _without_exclude_terms(config: Mapping[str, Any]) -> dict[str, Any]:
    """Avoid re-applying older substring filters after boundary validation."""

    result = dict(config)
    research = dict(_research(config))
    research["exclude_terms"] = []
    result["research"] = research
    result["exclude_terms"] = []
    return result


def _adaptive_rank(
    items: Sequence[Item],
    config: Mapping[str, Any],
    learning_db: Path | None,
    *,
    warn: Callable[[str], None],
) -> list[Item]:
    """Blend aggregate learning facts into base scores without mutating Items."""

    if not items or learning_db is None:
        return list(items)
    try:
        from scripts.adaptive_learning import open_database, rank_candidates

        rows = []
        signals_by_id: dict[str, dict[str, Any]] = {}
        for item in items:
            signals = _candidate_signals(item, config)
            signals_by_id[item.id] = signals
            rows.append(
                {
                    "id": item.id,
                    "title": item.title,
                    "url": item.url,
                    "excerpt": item.excerpt,
                    "source": item.source,
                    "source_kind": item.source_kind,
                    "matched_keywords": signals["keywords"],
                    "query": item.query,
                    "tags": signals["tags"],
                    "domain_groups": signals["domain_groups"],
                    "base_score": item.score,
                }
            )
        connection = open_database(learning_db)
        try:
            facts = rank_candidates(connection, rows, config)
        finally:
            # rank_candidates may use its own transaction context. Closing is
            # idempotent and avoids retaining a VPS file lock between runs.
            connection.close()
    except (ImportError, OSError, sqlite3.Error, TypeError, ValueError) as exc:
        warn(f"adaptive ranking unavailable; using base ranking: {exc}")
        return list(items)

    by_id = {str(row.get("id")): row for row in facts if isinstance(row, Mapping) and row.get("id")}
    settings = _mapping(config.get("learning"))
    adaptive_settings = _mapping(config.get("adaptive_learning"))
    maximum_bonus = _finite_float(
        settings.get("max_learning_bonus", adaptive_settings.get("max_learning_bonus", 1.5)), 1.5,
    )
    maximum_bonus = max(0.0, min(maximum_bonus, 10.0))
    result: list[Item] = []
    for item in items:
        fact = by_id.get(item.id)
        if fact is None:
            # ``rank_candidates`` intentionally omits candidates that exceed
            # adaptive source-diversity caps; honor that eligibility decision.
            continue
        learned_score = _finite_float(fact.get("score", 0.5), 0.5)
        bonus = max(-maximum_bonus, min(maximum_bonus, (learned_score - 0.5) * 2 * maximum_bonus))
        metadata = dict(item.metadata)
        metadata["learning"] = {
            key: fact.get(key)
            for key in ("score", "posterior", "exploration_bonus", "observations")
            if key in fact
        }
        metadata["candidate_signals"] = signals_by_id[item.id]
        result.append(replace(item, score=round(_finite_float(item.score) + bonus, 6), metadata=metadata))
    return sorted(result, key=lambda item: (item.score, item.published_at, item.id), reverse=True)


def _json_safe(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Mapping):
        return {str(key): _json_safe(entry, depth + 1) for key, entry in list(value.items())[:100]}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(entry, depth + 1) for entry in list(value)[:100]]
    return str(value)


def _export_item(item: Item, config: Mapping[str, Any]) -> dict[str, Any]:
    signals = _candidate_signals(item, config)
    metadata = dict(item.metadata)
    metadata["domain_groups"] = signals["domain_groups"]
    return {
        "id": item.id,
        "title": item.title,
        "url": item.url,
        "source": item.source,
        "source_kind": item.source_kind,
        "category": item.category,
        "published_at": item.published_at,
        "excerpt": item.excerpt,
        "score": round(_finite_float(item.score), 6),
        "doi": item.doi,
        "authors": list(item.authors),
        "query": item.query,
        "matched_keywords": signals["keywords"],
        "tags": signals["tags"],
        "metadata": _json_safe(metadata),
        "candidate_signals": signals,
    }


def _handoff_settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("candidates_handoff", "handoff"):
        if isinstance(config.get(key), Mapping):
            return config[key]
    return {}


def build_handoff(
    items: Sequence[Item],
    config: Mapping[str, Any],
    now: datetime,
    *,
    edition: str,
    collection_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Build the versioned collector/blog boundary object."""

    settings = _handoff_settings(config)
    site = _mapping(config.get("site"))
    timezone_name = str(settings.get("timezone") or site.get("timezone") or "UTC")
    try:
        local_now = _aware(now).astimezone(ZoneInfo(timezone_name))
    except ZoneInfoNotFoundError:
        timezone_name = "UTC"
        local_now = _aware(now).astimezone(timezone.utc)
    # ``max_items`` is the publication contract for the blog.  The collector
    # deliberately exports a larger candidate pool for downstream editorial
    # selection, controlled independently by candidate_limit/pool_size.
    max_items = _bounded_int(site.get("max_items"), DEFAULT_PUBLICATION_MAX_ITEMS, HARD_MAX_ITEMS)
    candidate_limit = _bounded_int(
        settings.get("candidate_limit", site.get("candidate_pool_size")),
        DEFAULT_CANDIDATE_LIMIT,
        HARD_MAX_ITEMS,
    )
    ttl_hours = _bounded_int(settings.get("ttl_hours"), DEFAULT_TTL_HOURS, HARD_TTL_HOURS)
    exported = [_export_item(item, config) for item in items[:candidate_limit]]
    identity = json.dumps(
        {
            "edition": edition,
            "generated_at": local_now.isoformat(),
            "ids": [item["id"] for item in exported],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    batch_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    counts = {str(key): int(value) for key, value in collection_counts.items()}
    counts["candidate_items"] = len(exported)
    return {
        "schema": CANDIDATES_SCHEMA,
        "batch_id": batch_id,
        "edition": edition,
        "generated_at": local_now.isoformat(),
        "timezone": timezone_name,
        "expires_at": (local_now + timedelta(hours=ttl_hours)).isoformat(),
        "max_items": max_items,
        "collection_counts": counts,
        "items": exported,
    }


def atomic_write_handoff(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Durably write JSON beside its destination, then atomically replace it."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
    return destination


def run_pipeline(
    config: Mapping[str, Any],
    *,
    vault_path: str | Path,
    output_path: str | Path,
    scout_path: str | Path | None = None,
    learning_db: str | Path | None = None,
    now: datetime | None = None,
    edition: str | None = None,
    rss_collector: Callable[[Mapping[str, Any], datetime], list[Item]] | None = None,
    scout_loader: Callable[..., list[Item]] | None = None,
    pool_factory: Callable[[str | Path], Any] | None = None,
    adaptive_ranker: Callable[[Sequence[Item], Mapping[str, Any], Path | None], list[Item]] | None = None,
    warn: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Execute one complete collector run and return the written handoff."""

    warning = warn or _warn
    now = _aware(now)
    rss_fn = rss_collector or collect
    scout_fn = scout_loader or load_scout_handoff
    rss_items = list(rss_fn(config, now))

    scout_config = dict(_mapping(config.get("openclaw_scout") or config.get("scout")))
    if scout_path is not None:
        scout_config["path"] = str(scout_path)
    scout_items = (
        scout_fn(scout_config, now, warn=warning)
        if scout_config.get("path") and scout_config.get("enabled", True) is not False
        else []
    )
    collected = [*rss_items, *scout_items]
    eligible = [item for item in collected if not _is_excluded(item, config)]

    if pool_factory is None:
        from scripts.knowledge_pool import KnowledgePool

        pool_factory = KnowledgePool
    # Pool every in-scope discovery before ranking. Explicitly excluded terms
    # are a collection boundary (for example consumer drones/UAVs), not merely
    # a low editorial score, and therefore never enter the Vault.
    pool = pool_factory(vault_path)
    pooled_paths = pool.ingest(eligible, collected_at=now)

    ranking_config = _without_exclude_terms(config)
    base_ranked = rank(eligible, ranking_config, set(), now)
    learning_path = Path(learning_db) if learning_db is not None else None
    if adaptive_ranker is None:
        ranked = _adaptive_rank(base_ranked, ranking_config, learning_path, warn=warning)
    else:
        ranked = adaptive_ranker(base_ranked, ranking_config, learning_path)

    selected_edition = str(edition or config.get("edition") or "daily")
    counts = {
        "rss_items": len(rss_items),
        "scout_items": len(scout_items),
        "collected_items": len(collected),
        "pooled_items": len(pooled_paths),
        "excluded_items": len(collected) - len(eligible),
        "ranked_items": len(ranked),
    }
    payload = build_handoff(
        ranked,
        config,
        now,
        edition=selected_edition,
        collection_counts=counts,
    )
    atomic_write_handoff(output_path, payload)
    mark_candidates = getattr(pool, "mark_candidates", None)
    if callable(mark_candidates):
        mark_candidates(payload)
    return payload


def _load_config(path: Path) -> Mapping[str, Any]:
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, Mapping):
        raise ValueError("config root must be an object")
    return value


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/sources.yaml"))
    parser.add_argument("--vault", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--scout", type=Path)
    parser.add_argument("--learning-db", type=Path)
    parser.add_argument("--edition")
    args = parser.parse_args(list(argv) if argv is not None else None)

    config = _load_config(args.config)
    paths = _mapping(config.get("paths"))
    vault = args.vault or Path(str(paths.get("vault") or "data/knowledge-vault"))
    output = args.output or Path(str(paths.get("candidates") or "data/handoff/candidates.json"))
    learning_db = args.learning_db or Path(str(paths.get("learning_db") or "data/adaptive-learning.sqlite3"))
    scout = args.scout or (Path(str(paths["scout"])) if paths.get("scout") else None)
    payload = run_pipeline(
        config,
        vault_path=vault,
        output_path=output,
        scout_path=scout,
        learning_db=learning_db,
        edition=args.edition,
    )
    print(json.dumps({"output": str(output), "batch_id": payload["batch_id"], **payload["collection_counts"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
