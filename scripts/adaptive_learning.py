#!/usr/bin/env python3
"""Rebuildable adaptive ranking and research planning for the collector.

The SQLite database is learning state, never an article store.  It contains
Beta posterior aggregates, compact editorial assessments, selected item IDs,
and item-to-signal bindings.  Titles, URLs, abstracts, excerpts, article
bodies, and rendered Markdown are intentionally absent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlsplit


SCHEMA_VERSION = 1
SIGNAL_KINDS = ("source", "keyword", "priority_area", "query", "domain_group", "source_kind")
MAX_INBOX_BYTES = 2_000_000


@dataclass(frozen=True)
class LearningSettings:
    prior_alpha: float = 1.0
    prior_beta: float = 1.0
    source_weight: float = 0.55
    keyword_weight: float = 0.30
    priority_area_weight: float = 0.15
    exploration_weight: float = 0.10
    exploration_cap: float = 0.25
    max_per_source: int = 0
    relevance_weight: float = 0.40
    quality_weight: float = 0.35
    novelty_weight: float = 0.25
    selection_weight: float = 0.25
    editorial_reward_weight: float = 1.0
    explicit_feedback_weight: float = 1.0


class LearningConnection(sqlite3.Connection):
    """SQLite connection whose context manager also releases the file handle."""

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc, traceback))
        finally:
            self.close()


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Iterable[None]:
    """Commit/rollback an operation without closing a reusable connection."""
    try:
        yield
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _finite(section: Mapping[str, Any], key: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(section.get(key, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"learning.{key} must be a finite number >= {minimum}") from exc
    if not math.isfinite(value) or value < minimum:
        raise ValueError(f"learning.{key} must be a finite number >= {minimum}")
    return value


def settings_from_config(config: Mapping[str, Any] | None = None) -> LearningSettings:
    config = config or {}
    section = config.get("learning", config.get("adaptive_learning", config))
    if not isinstance(section, Mapping):
        raise ValueError("learning config must be an object")
    editorial = section.get("editorial_weights", {})
    if not isinstance(editorial, Mapping):
        raise ValueError("learning.editorial_weights must be an object")
    cap_default = _finite(section, "max_learning_bonus", 0.25)
    max_per_source = int(section.get("max_per_source", 0))
    if max_per_source < 0:
        raise ValueError("learning.max_per_source must be >= 0")
    result = LearningSettings(
        prior_alpha=_finite(section, "prior_alpha", 1.0, 0.000001),
        prior_beta=_finite(section, "prior_beta", 1.0, 0.000001),
        source_weight=_finite(section, "source_weight", 0.55),
        keyword_weight=_finite(section, "keyword_weight", 0.30),
        priority_area_weight=_finite(section, "priority_area_weight", 0.15),
        exploration_weight=_finite(section, "exploration_weight", 0.10),
        exploration_cap=_finite(section, "exploration_cap", cap_default),
        max_per_source=max_per_source,
        relevance_weight=_finite(editorial, "relevance", 0.40),
        quality_weight=_finite(editorial, "quality", 0.35),
        novelty_weight=_finite(editorial, "novelty", 0.25),
        selection_weight=_finite(section, "selection_weight", 0.25),
        editorial_reward_weight=_finite(section, "editorial_reward_weight", 1.0),
        explicit_feedback_weight=_finite(section, "explicit_feedback_weight", 1.0),
    )
    if result.source_weight + result.keyword_weight + result.priority_area_weight <= 0:
        raise ValueError("source, keyword, and priority-area weights cannot all be zero")
    if result.relevance_weight + result.quality_weight + result.novelty_weight <= 0:
        raise ValueError("editorial assessment weights cannot all be zero")
    if result.selection_weight > 1:
        raise ValueError("learning.selection_weight must be <= 1")
    return result


def open_database(path: str | Path) -> sqlite3.Connection:
    raw = str(path)
    if raw != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(raw, factory=LearningConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    initialize_database(connection)
    return connection


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS processed_events (
            event_id TEXT PRIMARY KEY,
            payload_hash TEXT NOT NULL,
            event_type TEXT NOT NULL,
            article_id TEXT,
            item_id TEXT,
            recorded_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS signal_stats (
            kind TEXT NOT NULL CHECK (
                kind IN ('source','keyword','priority_area','query','domain_group','source_kind')
            ),
            name TEXT NOT NULL,
            alpha REAL NOT NULL,
            beta REAL NOT NULL,
            observations INTEGER NOT NULL,
            PRIMARY KEY (kind, name)
        );
        CREATE TABLE IF NOT EXISTS editorial_runs (
            event_id TEXT PRIMARY KEY REFERENCES processed_events(event_id),
            article_id TEXT,
            assessments_json TEXT NOT NULL,
            selected_ids_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS explicit_feedback (
            event_id TEXT PRIMARY KEY REFERENCES processed_events(event_id),
            article_id TEXT,
            item_id TEXT,
            reward REAL NOT NULL CHECK (reward >= 0 AND reward <= 1),
            reason TEXT NOT NULL,
            CHECK (article_id IS NOT NULL OR item_id IS NOT NULL)
        );
        CREATE TABLE IF NOT EXISTS item_signal_bindings (
            item_id TEXT NOT NULL,
            kind TEXT NOT NULL CHECK (
                kind IN ('source','keyword','priority_area','query','domain_group','source_kind')
            ),
            name TEXT NOT NULL,
            PRIMARY KEY (item_id, kind, name)
        );
        """
    )
    current = connection.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()
    if current and int(current["value"]) != SCHEMA_VERSION:
        raise RuntimeError(f"unsupported learning schema: {current['value']}")
    connection.execute(
        "INSERT OR IGNORE INTO schema_meta(key,value) VALUES ('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )
    connection.commit()


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _name(value: Any, field: str) -> str:
    return " ".join(_identifier(value, field).split()).casefold()


def _unique_names(values: Iterable[Any], field: str) -> list[str]:
    return list(dict.fromkeys(_name(value, field) for value in values if value is not None and str(value).strip()))


def _bounded(value: Any, field: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be between 0 and 1")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be between 0 and 1") from exc
    if not math.isfinite(result) or not 0 <= result <= 1:
        raise ValueError(f"{field} must be between 0 and 1")
    return result


def _research(config: Mapping[str, Any]) -> Mapping[str, Any]:
    value = config.get("research", {})
    if not isinstance(value, Mapping):
        raise ValueError("research config must be an object")
    return value


def _priority_areas(config: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = _research(config).get("priority_areas", [])
    if isinstance(raw, Mapping):
        raw = [{"name": key, "keywords": value} for key, value in raw.items()]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("research.priority_areas must be a list or object")
    result: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        if isinstance(value, str):
            area = {"name": value, "keywords": [value], "queries": [value], "weight": 1.0}
        elif isinstance(value, Mapping):
            area = dict(value)
            area["name"] = _identifier(value.get("name", value.get("id")), f"priority_areas[{index}].name")
            keywords = value.get("keywords", value.get("terms"))
            if keywords is None:
                matching_topics = [
                    topic
                    for topic in config.get("topics", [])
                    if isinstance(topic, Mapping)
                    and str(topic.get("name", "")).casefold() == area["name"].casefold()
                ]
                keywords = [
                    keyword for topic in matching_topics for keyword in topic.get("keywords", [])
                ]
            if not keywords and value.get("scope"):
                keywords = [
                    term.strip()
                    for term in re.split(r"[/,;|、・]", str(value["scope"]))
                    if term.strip()
                ]
            if not keywords:
                keywords = [area["name"]]
            queries = value.get("queries", value.get("seed_queries", []))
            if isinstance(keywords, str):
                keywords = [keywords]
            if isinstance(queries, str):
                queries = [queries]
            if not isinstance(keywords, Sequence) or not isinstance(queries, Sequence):
                raise ValueError(f"priority_areas[{index}] keywords and queries must be lists")
            area["keywords"] = [str(term).strip() for term in keywords if str(term).strip()]
            area["queries"] = [str(term).strip() for term in queries if str(term).strip()]
            area["weight"] = float(value.get("weight", 1.0))
        else:
            raise ValueError(f"priority_areas[{index}] must be a string or object")
        result.append(area)
    return result


def _exclude_terms(config: Mapping[str, Any]) -> list[str]:
    raw = _research(config).get("exclude_terms", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("research.exclude_terms must be a list")
    return _unique_names(raw, "exclude_terms")


def _candidate_signals(candidate: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, list[str]]:
    result = {kind: [] for kind in SIGNAL_KINDS}
    if candidate.get("source"):
        result["source"] = [_name(candidate["source"], "candidate.source")]
    if candidate.get("source_kind"):
        result["source_kind"] = [_name(candidate["source_kind"], "candidate.source_kind")]
    raw_keywords = candidate.get("matched_keywords", candidate.get("keywords", []))
    if isinstance(raw_keywords, str):
        raw_keywords = [raw_keywords]
    tags = candidate.get("tags", [])
    if isinstance(tags, str):
        tags = [tags]
    if not isinstance(raw_keywords, Sequence) or not isinstance(tags, Sequence):
        raise ValueError("candidate matched_keywords/keywords and tags must be lists")
    result["keyword"] = _unique_names(raw_keywords, "candidate.matched_keywords")
    raw_queries = candidate.get("queries", candidate.get("query", []))
    if isinstance(raw_queries, str):
        raw_queries = [raw_queries]
    if not isinstance(raw_queries, Sequence):
        raise ValueError("candidate.query/queries must be a string or list")
    result["query"] = _unique_names(raw_queries, "candidate.queries")
    searchable = " ".join(
        str(candidate.get(key, "")) for key in ("title", "excerpt", "query", "source", "source_kind")
    )
    searchable += " " + " ".join(map(str, [*raw_keywords, *tags]))
    haystack = searchable.casefold()
    result["priority_area"] = [
        _name(area["name"], "priority_area.name")
        for area in _priority_areas(config)
        if any(str(term).casefold() in haystack for term in area["keywords"])
    ]
    host = urlsplit(str(candidate.get("url", ""))).hostname or ""
    supplied_groups = candidate.get("domain_groups", [])
    if isinstance(supplied_groups, str):
        supplied_groups = [supplied_groups]
    if not isinstance(supplied_groups, Sequence):
        raise ValueError("candidate.domain_groups must be a list")
    result["domain_group"] = _unique_names(supplied_groups, "candidate.domain_groups")
    groups = _research(config).get("domain_groups", {})
    if isinstance(groups, Mapping):
        for group, domains in groups.items():
            if isinstance(domains, str):
                domains = [domains]
            if isinstance(domains, Sequence) and any(
                host.casefold() == str(domain).casefold()
                or host.casefold().endswith("." + str(domain).casefold())
                for domain in domains
            ):
                normalized_group = _name(group, "domain_group")
                if normalized_group not in result["domain_group"]:
                    result["domain_group"].append(normalized_group)
    return result


def _is_excluded(candidate: Mapping[str, Any], config: Mapping[str, Any]) -> bool:
    values: list[str] = []
    for key in ("title", "excerpt", "query", "source", "source_kind"):
        values.append(str(candidate.get(key, "")))
    for key in ("matched_keywords", "keywords", "tags"):
        raw = candidate.get(key, [])
        values.extend(map(str, raw if isinstance(raw, Sequence) and not isinstance(raw, str) else [raw]))
    haystack = " ".join(values).casefold()
    return any(term in haystack for term in _exclude_terms(config))


def _posterior(
    connection: sqlite3.Connection, kind: str, name: str, settings: LearningSettings
) -> tuple[float, int]:
    row = connection.execute(
        "SELECT alpha,beta,observations FROM signal_stats WHERE kind=? AND name=?",
        (kind, _name(name, kind)),
    ).fetchone()
    if row is None:
        return settings.prior_alpha / (settings.prior_alpha + settings.prior_beta), 0
    return float(row["alpha"]) / (float(row["alpha"]) + float(row["beta"])), int(row["observations"])


def _exploration(counts: Sequence[int], total: int, settings: LearningSettings) -> float:
    active = list(counts) or [0]
    raw = sum(math.sqrt(math.log(total + 2.0) / (count + 1.0)) for count in active) / len(active)
    return min(settings.exploration_cap, settings.exploration_weight * raw)


def rank_candidate(
    connection: sqlite3.Connection,
    candidate: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    config = config or {}
    candidate_id = _identifier(candidate.get("id"), "candidate.id")
    if _is_excluded(candidate, config):
        return {"id": candidate_id, "excluded": True, "score": 0.0, "posterior": 0.0, "exploration_bonus": 0.0, "observations": 0}
    settings = settings_from_config(config)
    signals = _candidate_signals(candidate, config)
    means: dict[str, float] = {}
    counts: list[int] = []
    prior = settings.prior_alpha / (settings.prior_alpha + settings.prior_beta)
    for kind in ("source", "keyword", "priority_area"):
        rows = [_posterior(connection, kind, name, settings) for name in signals[kind]]
        means[kind] = sum(row[0] for row in rows) / len(rows) if rows else prior
        counts.extend(row[1] for row in rows)
    total = int(
        connection.execute(
            "SELECT COALESCE(SUM(observations),0) FROM signal_stats WHERE kind IN ('source','keyword','priority_area')"
        ).fetchone()[0]
    )
    weight_total = settings.source_weight + settings.keyword_weight + settings.priority_area_weight
    exploitation = (
        settings.source_weight * means["source"]
        + settings.keyword_weight * means["keyword"]
        + settings.priority_area_weight * means["priority_area"]
    ) / weight_total
    bonus = _exploration(counts, total, settings)
    static_area_boost = sum(
        float(area.get("weight", 1.0))
        for area in _priority_areas(config)
        if _name(area["name"], "priority_area.name") in signals["priority_area"]
    ) * float(_research(config).get("priority_area_boost", 0.0))
    return {
        "id": candidate_id,
        "excluded": False,
        "score": round(exploitation + bonus + static_area_boost, 12),
        "posterior": round(exploitation, 12),
        "exploration_bonus": round(bonus, 12),
        "observations": sum(counts),
        "matched_priority_areas": signals["priority_area"],
    }


def rank_candidates(
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank candidates and retain only their ID-to-signal learning bindings."""
    config = config or {}
    settings = settings_from_config(config)
    ranked: list[dict[str, Any]] = []
    eligible: list[Mapping[str, Any]] = []
    for candidate in candidates:
        row = rank_candidate(connection, candidate, config)
        if row["excluded"]:
            continue
        eligible.append(candidate)
        row["_source"] = _name(candidate.get("source", "unknown"), "candidate.source")
        ranked.append(row)
    register_candidates(connection, eligible, config)
    ranked.sort(key=lambda row: (-float(row["score"]), str(row["id"])))
    result: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    for row in ranked:
        source = row.pop("_source")
        if settings.max_per_source and counts.get(source, 0) >= settings.max_per_source:
            continue
        counts[source] = counts.get(source, 0) + 1
        result.append(row)
    return result


def register_candidates(
    connection: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None = None,
) -> int:
    """Persist only item IDs and learning signals for later publisher feedback."""
    config = config or {}
    changed = 0
    with _transaction(connection):
        for candidate in candidates:
            item_id = _identifier(candidate.get("id"), "candidate.id")
            for kind, names in _candidate_signals(candidate, config).items():
                for signal_name in names:
                    cursor = connection.execute(
                        "INSERT OR IGNORE INTO item_signal_bindings VALUES (?,?,?)",
                        (item_id, kind, signal_name),
                    )
                    changed += cursor.rowcount
    return changed


def reviewed_item_ids(connection: sqlite3.Connection) -> set[str]:
    """Return item IDs that already received a publisher editorial decision."""

    reviewed: set[str] = set()
    for row in connection.execute("SELECT assessments_json FROM editorial_runs"):
        assessments = json.loads(row["assessments_json"])
        if not isinstance(assessments, list):
            raise ValueError("stored editorial assessments must be a list")
        for assessment in assessments:
            if not isinstance(assessment, Mapping):
                raise ValueError("stored editorial assessment must be an object")
            reviewed.add(_identifier(assessment.get("id"), "stored candidate feedback ID"))
    return reviewed


def _event_hash(value: Mapping[str, Any]) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _claim_event(
    connection: sqlite3.Connection,
    event_id: str,
    event_type: str,
    article_id: str | None,
    item_id: str | None,
    normalized_payload: Mapping[str, Any],
) -> bool:
    digest = _event_hash(normalized_payload)
    row = connection.execute("SELECT payload_hash FROM processed_events WHERE event_id=?", (event_id,)).fetchone()
    if row:
        if row["payload_hash"] != digest:
            raise ValueError(f"event_id {event_id!r} was already used with different content")
        return False
    connection.execute(
        "INSERT INTO processed_events VALUES (?,?,?,?,?,?)",
        (event_id, digest, event_type, article_id, item_id, datetime.now(timezone.utc).isoformat()),
    )
    return True


def _apply_reward(
    connection: sqlite3.Connection,
    signals: Mapping[str, Sequence[str]],
    reward: float,
    settings: LearningSettings,
    evidence_weight: float,
) -> None:
    for kind in SIGNAL_KINDS:
        for signal_name in signals.get(kind, []):
            connection.execute(
                """INSERT INTO signal_stats(kind,name,alpha,beta,observations) VALUES (?,?,?,?,1)
                   ON CONFLICT(kind,name) DO UPDATE SET
                     alpha=alpha+excluded.alpha-?, beta=beta+excluded.beta-?, observations=observations+1""",
                (
                    kind,
                    _name(signal_name, kind),
                    settings.prior_alpha + reward * evidence_weight,
                    settings.prior_beta + (1.0 - reward) * evidence_weight,
                    settings.prior_alpha,
                    settings.prior_beta,
                ),
            )


def _known_signals(connection: sqlite3.Connection, item_ids: Sequence[str]) -> dict[str, list[str]]:
    result = {kind: [] for kind in SIGNAL_KINDS}
    if not item_ids:
        return result
    placeholders = ",".join("?" for _ in item_ids)
    for row in connection.execute(
        f"SELECT DISTINCT kind,name FROM item_signal_bindings WHERE item_id IN ({placeholders})",
        tuple(item_ids),
    ):
        result[row["kind"]].append(row["name"])
    return result


def _normalize_assessments(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError("candidate_feedback must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        if not isinstance(raw, Mapping):
            raise ValueError(f"candidate_feedback[{index}] must be an object")
        item_id = _identifier(raw.get("id"), f"candidate_feedback[{index}].id")
        if item_id in seen:
            raise ValueError(f"duplicate candidate feedback ID: {item_id}")
        seen.add(item_id)
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
            raise ValueError(f"candidate_feedback[{index}].reason must contain 1-2000 characters")
        result.append(
            {
                "id": item_id,
                "relevance": _bounded(raw.get("relevance"), f"{item_id}.relevance"),
                "quality": _bounded(raw.get("quality"), f"{item_id}.quality"),
                "novelty": _bounded(raw.get("novelty"), f"{item_id}.novelty"),
                "reason": reason.strip(),
            }
        )
    return result


def extract_selected_ids(feedback: Mapping[str, Any]) -> list[str]:
    selected: list[str] = []

    def add(values: Any, field: str) -> None:
        if values is None:
            return
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError(f"{field} must be a list")
        for value in values:
            item_id = _identifier(value, field)
            if item_id not in selected:
                selected.append(item_id)

    add(feedback.get("source_ids", feedback.get("selected_ids")), "source_ids")
    items = feedback.get("items", [])
    if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
        raise ValueError("items must be a list")
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            raise ValueError(f"items[{index}] must be an object")
        if item.get("id"):
            add([item["id"]], f"items[{index}].id")
        add(item.get("source_ids"), f"items[{index}].source_ids")
    return selected


def record_editorial_feedback(
    connection: sqlite3.Connection,
    event_id: str,
    article_id: str | None,
    candidate_feedback: Sequence[Mapping[str, Any]],
    selected_ids: Sequence[str],
    candidate_signals: Mapping[str, Mapping[str, Any]] | None = None,
    config: Mapping[str, Any] | None = None,
) -> bool:
    config = config or {}
    event_id = _identifier(event_id, "event_id")
    article_id = _identifier(article_id, "article_id") if article_id else None
    assessments = _normalize_assessments(candidate_feedback)
    selected = list(dict.fromkeys(_identifier(value, "selected_ids") for value in selected_ids))
    known_ids = {row["id"] for row in assessments}
    unknown = [item_id for item_id in selected if item_id not in known_ids]
    if unknown:
        raise ValueError(f"selected IDs have no candidate feedback: {', '.join(unknown)}")
    candidate_signals = candidate_signals or {}
    normalized_event = {
        "event_id": event_id,
        "article_id": article_id,
        "candidate_feedback": assessments,
        "selected_ids": selected,
    }
    settings = settings_from_config(config)
    editorial_total = settings.relevance_weight + settings.quality_weight + settings.novelty_weight
    with _transaction(connection):
        if not _claim_event(connection, event_id, "publisher_feedback", article_id, None, normalized_event):
            return False
        connection.execute(
            "INSERT INTO editorial_runs VALUES (?,?,?,?)",
            (
                event_id,
                article_id,
                json.dumps(assessments, ensure_ascii=False, separators=(",", ":")),
                json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        selected_set = set(selected)
        for row in assessments:
            known = _known_signals(connection, [row["id"]])
            raw_signals = candidate_signals.get(row["id"])
            if raw_signals:
                signals = _candidate_signals(raw_signals, config)
                for kind in SIGNAL_KINDS:
                    signals[kind] = list(dict.fromkeys([*signals[kind], *known[kind]]))
                for kind, names in signals.items():
                    for signal_name in names:
                        connection.execute(
                            "INSERT OR IGNORE INTO item_signal_bindings VALUES (?,?,?)",
                            (row["id"], kind, signal_name),
                        )
            else:
                signals = known
            editorial_reward = (
                settings.relevance_weight * row["relevance"]
                + settings.quality_weight * row["quality"]
                + settings.novelty_weight * row["novelty"]
            ) / editorial_total
            reward = (1.0 - settings.selection_weight) * editorial_reward
            reward += settings.selection_weight * (1.0 if row["id"] in selected_set else 0.0)
            _apply_reward(connection, signals, reward, settings, settings.editorial_reward_weight)
    return True


def _signals_from_envelope(envelope: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    raw = envelope.get("candidate_signals")
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items() if isinstance(value, Mapping)}
    bundle = envelope.get("bundle", envelope.get("candidate_bundle"))
    if isinstance(bundle, Mapping):
        raw = bundle.get("items", [])
    elif isinstance(envelope.get("candidates"), Sequence):
        raw = envelope["candidates"]
    else:
        raw = []
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("candidate bundle items must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"candidate bundle item {index} must be an object")
        result[_identifier(item.get("id"), f"candidate bundle item {index}.id")] = item
    return result


def record_publisher_feedback(
    connection: sqlite3.Connection,
    envelope: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> bool:
    """Record the publisher's scalar feedback envelope, ignoring body fields."""
    if not isinstance(envelope, Mapping):
        raise ValueError("publisher feedback must be an object")
    feedback = envelope.get("feedback", envelope.get("draft", envelope))
    if not isinstance(feedback, Mapping):
        raise ValueError("publisher feedback payload must be an object")
    assessments = feedback.get("candidate_feedback", envelope.get("candidate_feedback", []))
    selected = extract_selected_ids(feedback)
    article = feedback.get("article", envelope.get("article", envelope.get("article_id")))
    identity = {
        "article": article,
        "candidate_feedback": _normalize_assessments(assessments),
        "selected_ids": selected,
    }
    event_id = envelope.get("event_id", envelope.get("feedback_id", envelope.get("id")))
    if event_id is None:
        event_id = "publisher:" + _event_hash(identity)[:24]
    return record_editorial_feedback(
        connection,
        event_id,
        article,
        identity["candidate_feedback"],
        selected,
        _signals_from_envelope(envelope),
        config,
    )


def record_explicit_feedback(
    connection: sqlite3.Connection,
    event_id: str,
    article_id: str | None,
    item_id: str | None,
    reward: Any,
    reason: str = "",
    config: Mapping[str, Any] | None = None,
    signals: Mapping[str, Any] | None = None,
) -> bool:
    config = config or {}
    article_id = _identifier(article_id, "article_id") if article_id else None
    item_id = _identifier(item_id, "item_id") if item_id else None
    if not article_id and not item_id:
        raise ValueError("explicit feedback requires article_id or item_id")
    reward = _bounded(reward, "reward")
    if not isinstance(reason, str) or len(reason) > 2_000:
        raise ValueError("reason must be a string of at most 2000 characters")
    supplied_signals = _candidate_signals(signals, config) if signals else {kind: [] for kind in SIGNAL_KINDS}
    normalized = {
        "event_id": event_id,
        "article_id": article_id,
        "item_id": item_id,
        "reward": reward,
        "reason": reason.strip(),
        "signals": supplied_signals,
    }
    settings = settings_from_config(config)
    with _transaction(connection):
        if not _claim_event(connection, event_id, "explicit_feedback", article_id, item_id, normalized):
            return False
        connection.execute(
            "INSERT INTO explicit_feedback VALUES (?,?,?,?,?)",
            (event_id, article_id, item_id, reward, reason.strip()),
        )
        item_ids = [item_id] if item_id else []
        if supplied_signals and any(supplied_signals.values()):
            learned_signals = supplied_signals
            if item_id:
                for kind, names in supplied_signals.items():
                    for signal_name in names:
                        connection.execute(
                            "INSERT OR IGNORE INTO item_signal_bindings VALUES (?,?,?)",
                            (item_id, kind, signal_name),
                        )
        else:
            if article_id and not item_ids:
                for row in connection.execute(
                    "SELECT selected_ids_json FROM editorial_runs WHERE article_id=?", (article_id,)
                ):
                    item_ids.extend(json.loads(row["selected_ids_json"]))
            learned_signals = _known_signals(connection, list(dict.fromkeys(item_ids)))
        _apply_reward(
            connection,
            learned_signals,
            reward,
            settings,
            settings.explicit_feedback_weight,
        )
    return True


def process_exchange_event(
    connection: sqlite3.Connection,
    envelope: Mapping[str, Any],
    config: Mapping[str, Any] | None = None,
) -> bool:
    if not isinstance(envelope, Mapping):
        raise ValueError("exchange event must be an object")
    if "candidate_feedback" in envelope or "feedback" in envelope or "draft" in envelope:
        return record_publisher_feedback(connection, envelope, config)
    rating = envelope.get("rating", envelope.get("reward", envelope.get("value")))
    if isinstance(rating, str):
        aliases = {"up": 1.0, "down": 0.0}
        if rating.casefold() not in aliases:
            raise ValueError("rating must be up, down, or a number between 0 and 1")
        rating = aliases[rating.casefold()]
    event_id = envelope.get("event_id", envelope.get("feedback_id", envelope.get("id")))
    if event_id is None:
        event_id = "exchange:" + _event_hash(
            {
                "article": envelope.get("article", envelope.get("article_id")),
                "item_id": envelope.get("item_id"),
                "rating": rating,
                "note": envelope.get("note", envelope.get("reason", "")),
                "created_at": envelope.get("created_at"),
            }
        )[:24]
    return record_explicit_feedback(
        connection,
        event_id,
        envelope.get("article", envelope.get("article_id")),
        envelope.get("item_id"),
        rating,
        envelope.get("note", envelope.get("reason", "")),
        config,
        envelope.get("signals") if isinstance(envelope.get("signals"), Mapping) else None,
    )


def _events(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping) and "events" in value:
        value = value["events"]
    if isinstance(value, Mapping):
        return [value]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and all(
        isinstance(item, Mapping) for item in value
    ):
        return list(value)
    raise ValueError("exchange JSON must contain an event, list, or events object")


def ingest_exchange_inbox(
    connection: sqlite3.Connection,
    inbox: str | Path,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    inbox = Path(inbox)
    paths = sorted(inbox.glob("*.json")) if inbox.is_dir() else [inbox]
    result: dict[str, Any] = {"files": len(paths), "processed": 0, "skipped": 0, "errors": []}
    for path in paths:
        try:
            if path.stat().st_size > MAX_INBOX_BYTES:
                raise ValueError(f"exchange file exceeds {MAX_INBOX_BYTES} bytes")
            payload = json.loads(path.read_text(encoding="utf-8"))
            events = _events(payload)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            result["errors"].append({"file": str(path), "error": str(exc)})
            continue
        for index, event in enumerate(events):
            try:
                changed = process_exchange_event(connection, event, config)
                result["processed" if changed else "skipped"] += 1
            except (TypeError, ValueError, sqlite3.Error) as exc:
                result["errors"].append({"file": str(path), "index": index, "error": str(exc)})
    return result


def _seed_queries(config: Mapping[str, Any]) -> list[str]:
    research = _research(config)
    raw = research.get("seed_queries", [])
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, Sequence):
        raise ValueError("research.seed_queries must be a list")
    result = [str(value).strip() for value in raw if str(value).strip()]
    for area in _priority_areas(config):
        result.extend(area["queries"] or area["keywords"] or [area["name"]])
    excludes = _exclude_terms(config)
    return list(dict.fromkeys(query for query in result if not any(term in query.casefold() for term in excludes)))


def _query_plan(
    connection: sqlite3.Connection,
    queries: Sequence[str],
    limit: int,
    exploitation_ratio: float,
    settings: LearningSettings,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    total = int(
        connection.execute("SELECT COALESCE(SUM(observations),0) FROM signal_stats WHERE kind='query'").fetchone()[0]
    )
    rows: list[dict[str, Any]] = []
    for query in queries:
        mean, observations = _posterior(connection, "query", query, settings)
        bonus = _exploration([observations], total, settings)
        rows.append(
            {
                "kind": "query",
                "name": query,
                "posterior": round(mean, 12),
                "observations": observations,
                "exploration_bonus": round(bonus, 12),
                "score": round(mean + bonus, 12),
            }
        )
    limit = min(limit, len(rows))
    exploit_count = round(limit * exploitation_ratio)
    if limit >= 2:
        exploit_count = min(limit - 1, max(1, exploit_count))
    else:
        exploit_count = limit
    high = sorted(rows, key=lambda row: (-row["posterior"], -row["observations"], row["name"]))[:exploit_count]
    selected = {row["name"].casefold() for row in high}
    explore = [row for row in rows if row["name"].casefold() not in selected]
    explore.sort(key=lambda row: (row["observations"], -row["exploration_bonus"], row["name"]))
    explore = explore[: limit - len(high)]
    for row in high:
        row["strategy"] = "high_reward"
    for row in explore:
        row["strategy"] = "underexplored"
    schedule: list[dict[str, Any]] = []
    for index in range(max(len(high), len(explore), 0)):
        if index < len(high):
            schedule.append(high[index])
        if index < len(explore):
            schedule.append(explore[index])
    return high, explore, schedule


def _configured_sources(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw: Any = _research(config).get("must_check_sources", [])
    if not raw:
        raw = config.get("sources", [])
    watch = config.get("watchlist", {})
    if not raw and isinstance(watch, Mapping):
        raw = watch.get("sources", [])
    if not raw:
        raw = config.get("publisher_sources", [])
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        raise ValueError("configured sources must be a list")
    return [source for source in raw if isinstance(source, Mapping) and source.get("enabled", True)]


def _source_identity(source: Mapping[str, Any], index: int) -> str:
    return _identifier(source.get("id", source.get("name", source.get("url"))), f"sources[{index}]")


def build_watchlist(
    connection: sqlite3.Connection,
    config: Mapping[str, Any],
    as_of: date,
) -> list[dict[str, Any]]:
    sources = _configured_sources(config)
    research = _research(config)
    watch_config = config.get("watchlist", {}) if isinstance(config.get("watchlist"), Mapping) else {}
    coverage_days = max(1, int(research.get("watchlist_coverage_days", watch_config.get("coverage_days", 7))))
    default_size = math.ceil(len(sources) / coverage_days) if sources else 0
    size = int(research.get("watchlist_size", watch_config.get("max_sources", default_size)))
    size = max(0, min(size, len(sources)))
    if not size:
        return []
    rotation_days = max(1, int(research.get("watchlist_rotation_days", watch_config.get("rotation_days", 1))))
    settings = settings_from_config(config)
    rows: list[dict[str, Any]] = []
    safe_fields = (
        "id",
        "name",
        "url",
        "domains",
        "source_kind",
        "category",
        "tags",
        "priority_areas",
        "weight",
    )
    for index, source in enumerate(sources):
        identity = _source_identity(source, index)
        posterior, observations = _posterior(connection, "source", str(source.get("name", identity)), settings)
        rows.append(
            {
                "identity": identity,
                "source": {key: source[key] for key in safe_fields if key in source},
                "posterior": round(posterior, 12),
                "observations": observations,
            }
        )
    rows.sort(key=lambda row: row["identity"].casefold())
    slots = size
    if rows and slots:
        period = as_of.toordinal() // rotation_days
        start = (period * slots) % len(rows)
        chosen = [rows[(start + offset) % len(rows)] for offset in range(slots)]
    else:
        chosen = []
    prior = settings.prior_alpha / (settings.prior_alpha + settings.prior_beta)
    for row in chosen:
        row["strategy"] = (
            "high_reward" if row["observations"] > 0 and row["posterior"] > prior else "rotation"
        )
    chosen.sort(
        key=lambda row: (
            row["strategy"] != "high_reward",
            -row["posterior"],
            row["identity"].casefold(),
        )
    )
    return [
        {
            **row["source"],
            "watchlist_id": row["identity"],
            "strategy": row["strategy"],
            "posterior": row["posterior"],
            "observations": row["observations"],
        }
        for row in chosen
    ]


def _as_date(value: date | datetime | str | None) -> date:
    if value is None:
        return datetime.now(timezone.utc).date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def build_research_plan(
    connection: sqlite3.Connection,
    config: Mapping[str, Any],
    as_of: date | datetime | str | None = None,
) -> dict[str, Any]:
    research = _research(config)
    maximum = max(1, int(research.get("max_queries", 8)))
    ratio = float(research.get("exploitation_ratio", 0.6))
    if not 0 <= ratio <= 1:
        raise ValueError("research.exploitation_ratio must be between 0 and 1")
    high, explore, schedule = _query_plan(
        connection, _seed_queries(config), maximum, ratio, settings_from_config(config)
    )
    plan_date = _as_date(as_of)
    groups = research.get("domain_groups", {})
    if not isinstance(groups, Mapping):
        raise ValueError("research.domain_groups must be an object")
    active_sources = build_watchlist(connection, config, plan_date)
    configured_sources = _configured_sources(config)
    coverage_days = max(1, int(research.get("watchlist_coverage_days", 7)))
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": plan_date.isoformat(),
        "max_queries": maximum,
        "exploitation_ratio": ratio,
        "queries": [row["name"] for row in schedule],
        "domain_groups": dict(groups),
        "priority_areas": _priority_areas(config),
        "exclude_terms": _exclude_terms(config),
        "watchlist": {
            "active_sources": active_sources,
            "total_sources": len(configured_sources),
            "coverage_days": coverage_days,
            "rotation_day": (plan_date.toordinal() % coverage_days) + 1,
        },
        "high_reward": high,
        "underexplored": explore,
        "schedule": schedule,
    }


def learning_report(connection: sqlite3.Connection) -> dict[str, Any]:
    signals = {kind: [] for kind in SIGNAL_KINDS}
    for row in connection.execute("SELECT * FROM signal_stats ORDER BY kind,name"):
        signals[row["kind"]].append(
            {
                "name": row["name"],
                "alpha": round(float(row["alpha"]), 12),
                "beta": round(float(row["beta"]), 12),
                "posterior": round(float(row["alpha"]) / (float(row["alpha"]) + float(row["beta"])), 12),
                "observations": int(row["observations"]),
            }
        )
    runs = [
        {
            "event_id": row["event_id"],
            "article_id": row["article_id"],
            "candidate_feedback": json.loads(row["assessments_json"]),
            "selected_ids": json.loads(row["selected_ids_json"]),
        }
        for row in connection.execute("SELECT * FROM editorial_runs ORDER BY event_id")
    ]
    explicit = [
        dict(row)
        for row in connection.execute(
            "SELECT event_id,article_id,item_id,reward,reason FROM explicit_feedback ORDER BY event_id"
        )
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "totals": {
            "events": int(connection.execute("SELECT COUNT(*) FROM processed_events").fetchone()[0]),
            "publisher_feedback": len(runs),
            "explicit_feedback": len(explicit),
            "signal_observations": int(
                connection.execute("SELECT COALESCE(SUM(observations),0) FROM signal_stats").fetchone()[0]
            ),
        },
        "signals": signals,
        "publisher_feedback": runs,
        "explicit_feedback": explicit,
    }


plan = build_research_plan
feedback = record_explicit_feedback
ingest = ingest_exchange_inbox
report = learning_report


def _load_config(path: Path | None) -> Mapping[str, Any]:
    if path is None:
        return {}
    import yaml

    value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(value, Mapping):
        raise ValueError("config must be an object")
    return value


def _print(value: Any, output: Path | None = None) -> None:
    text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    else:
        print(text, end="")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path(".collector/learning.sqlite3"))
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--config", type=Path, required=True)
    plan_parser.add_argument("--as-of")
    plan_parser.add_argument("--output", type=Path)
    feedback_parser = commands.add_parser("feedback")
    feedback_parser.add_argument("--config", type=Path)
    feedback_parser.add_argument("--input", type=Path, required=True)
    ingest_parser = commands.add_parser("ingest")
    ingest_parser.add_argument("--config", type=Path)
    ingest_parser.add_argument("--inbox", type=Path, required=True)
    report_parser = commands.add_parser("report")
    report_parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    with open_database(args.db) as connection:
        if args.command == "plan":
            _print(build_research_plan(connection, _load_config(args.config), args.as_of), args.output)
            return 0
        if args.command == "feedback":
            events = _events(json.loads(args.input.read_text(encoding="utf-8")))
            changed = [process_exchange_event(connection, event, _load_config(args.config)) for event in events]
            _print({"processed": sum(changed), "skipped": len(changed) - sum(changed)})
            return 0
        if args.command == "ingest":
            result = ingest_exchange_inbox(connection, args.inbox, _load_config(args.config))
            _print(result)
            return 2 if result["errors"] else 0
        _print(learning_report(connection), args.output)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
