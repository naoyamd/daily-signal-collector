"""Obsidian-compatible Markdown knowledge pool.

One collected item maps to one Markdown note.  Markdown is the sole canonical
store; searches, reports, deduplication, and index rebuilding scan the vault.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml


MANAGED_START = "<!-- daily-signal:managed:start -->"
MANAGED_END = "<!-- daily-signal:managed:end -->"
FRONT_MATTER_FIELDS = (
    "id",
    "title",
    "url",
    "doi",
    "source",
    "source_kind",
    "category",
    "published_at",
    "first_collected_at",
    "last_collected_at",
    "status",
    "score",
    "tags",
    "authors",
    "query",
    "collector_metadata",
    "article",
    "editorial_assessment",
)
VALID_STATUSES = {"pooled", "candidate", "selected", "rejected"}
MAX_ID = 240
MAX_TITLE = 300
MAX_URL = 4_000
MAX_EXCERPT = 20_000
MAX_RESULTS = 1_000


def _text(value: Any, limit: int, one_line: bool = False) -> str:
    result = str(value or "").replace("\x00", "").strip()
    if one_line:
        result = re.sub(r"\s+", " ", result)
    return result[: max(0, limit)]


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    try:
        return dict(vars(value))
    except (AttributeError, TypeError) as exc:
        raise TypeError("items must be mappings, dataclasses, or attribute objects") from exc


def _load_object(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    loaded = json.loads(Path(value).read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError("bundle and feedback files must contain a JSON object")
    return loaded


def _iso(value: Any, default: datetime | None = None) -> str:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif value:
        raw = _text(value, 80, True)
        try:
            result = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return raw
    elif default is not None:
        result = default
    else:
        return ""
    return result.replace(tzinfo=result.tzinfo or timezone.utc).isoformat()


def _day(value: Any) -> str:
    raw = _text(value, 80, True)
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        match = re.match(r"\d{4}-\d{2}-\d{2}", raw)
        return match.group(0) if match else ""


def _slug(value: Any, limit: int = 80, fallback: str = "untitled") -> str:
    result = _text(value, limit * 3, True).casefold()
    result = re.sub(r"[^\w.-]+", "-", result, flags=re.UNICODE)
    result = re.sub(r"[-_.]{2,}", "-", result).strip(" .-_")
    if not result or re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])", result):
        result = fallback
    return result[:limit].rstrip(" .-_") or fallback


def _id_suffix(item_id: str) -> str:
    digest = hashlib.sha256(item_id.encode("utf-8")).hexdigest()[:8]
    return f"{_slug(item_id, 36, 'item')}-{digest}"


def _strings(value: Any, *, limit: int = 100, each: int = 200) -> list[str]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, Sequence):
        return []
    result: list[str] = []
    for raw in values[:limit]:
        clean = _text(raw, each, True)
        if clean and clean not in result:
            result.append(clean)
    return result


def _parse_note(document: str) -> tuple[dict[str, Any], str]:
    if not document.startswith("---\n"):
        return {}, document
    end = document.find("\n---\n", 4)
    if end < 0:
        return {}, document
    loaded = yaml.safe_load(document[4:end]) or {}
    if not isinstance(loaded, dict):
        raise ValueError("front matter must be a YAML mapping")
    return loaded, document[end + 5 :]


def _manual_parts(body: str) -> tuple[str, str]:
    start = body.find(MANAGED_START)
    end = body.find(MANAGED_END, start + len(MANAGED_START)) if start >= 0 else -1
    if start < 0 or end < 0:
        return "", body.lstrip("\n")
    return body[:start], body[end + len(MANAGED_END) :]


def _dump_note(metadata: Mapping[str, Any], body: str) -> str:
    ordered = {field: metadata.get(field, "") for field in FRONT_MATTER_FIELDS}
    front = yaml.safe_dump(ordered, allow_unicode=True, sort_keys=False, width=4_096).rstrip()
    return f"---\n{front}\n---\n{body.lstrip()}".rstrip() + "\n"


class KnowledgePool:
    def __init__(self, vault: str | Path):
        self.vault = Path(vault).expanduser().resolve()
        if self.vault.exists() and not self.vault.is_dir():
            raise ValueError(f"vault is not a directory: {self.vault}")

    def _inside(self, path: Path) -> Path:
        resolved = path.resolve(strict=False)
        try:
            resolved.relative_to(self.vault)
        except ValueError as exc:
            raise ValueError(f"unsafe path outside vault: {path}") from exc
        return resolved

    def _write(self, path: Path, content: str) -> None:
        safe = self._inside(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")

    def _read(self, path: Path) -> tuple[dict[str, Any], str]:
        return _parse_note(self._inside(path).read_text(encoding="utf-8"))

    def _item_files(self) -> list[Path]:
        root = self._inside(self.vault / "items")
        if not root.exists():
            return []
        result: list[Path] = []
        for path in root.rglob("*.md"):
            safe = self._inside(path)
            if safe.is_file():
                result.append(safe)
        return sorted(result)

    def _records(self) -> dict[str, tuple[Path, dict[str, Any], str]]:
        result: dict[str, tuple[Path, dict[str, Any], str]] = {}
        for path in self._item_files():
            metadata, body = self._read(path)
            item_id = _text(metadata.get("id"), MAX_ID, True)
            if not item_id:
                continue
            if item_id in result:
                raise ValueError(f"duplicate item id {item_id!r}: {result[item_id][0]} and {path}")
            result[item_id] = path, metadata, body
        return result

    def _new_path(self, item: Mapping[str, Any], collected_at: str) -> Path:
        year = (_day(item.get("published_at")) or _day(collected_at) or "unknown")[:4]
        kind = _slug(item.get("source_kind") or "feed", 40, "feed")
        filename = f"{_slug(item.get('title'), 80)}--{_id_suffix(str(item['id']))}.md"
        return self._inside(self.vault / "items" / kind / year / filename)

    @staticmethod
    def _managed(metadata: Mapping[str, Any], excerpt: str) -> str:
        lines = [
            MANAGED_START,
            f"# {metadata['title']}",
            "",
            "## Summary",
            "",
            excerpt or "_No summary collected._",
            "",
            "## Source",
            "",
        ]
        if metadata.get("url"):
            lines.append(f"- [{metadata.get('source') or 'Open original'}]({metadata['url']})")
        if metadata.get("doi"):
            lines.append(f"- DOI: {metadata['doi']}")
        if metadata.get("authors"):
            lines.append(f"- Authors: {', '.join(metadata['authors'])}")
        if metadata.get("published_at"):
            lines.append(f"- Published: {metadata['published_at']}")
        lines.extend(["", MANAGED_END, ""])
        return "\n".join(lines)

    def ingest(self, items: Iterable[Any], collected_at: datetime | str | None = None) -> list[Path]:
        """Persist every Item-like object, including unpublished background items."""

        now = _iso(collected_at, datetime.now(timezone.utc))
        records = self._records()
        written: list[Path] = []
        touched: set[str] = set()
        for raw in items:
            item = _mapping(raw)
            raw_id = str(item.get("id") or "")
            item_id = _text(raw_id, MAX_ID, True)
            if not item_id:
                raise ValueError("every item requires a non-empty id")
            if len(raw_id) > MAX_ID or any(ord(char) < 32 for char in raw_id):
                raise ValueError("item id is too long or contains control characters")
            item["id"] = item_id
            existing = records.get(item_id)
            path, previous, old_body = existing if existing else (self._new_path(item, now), {}, "")

            def latest(name: str, limit: int = 1_000) -> str:
                supplied = _text(item.get(name), limit, True)
                return supplied or _text(previous.get(name), limit, True)

            try:
                score = float(item.get("score", previous.get("score", 0)) or 0)
            except (TypeError, ValueError):
                score = 0.0
            status = _text(previous.get("status") or item.get("status") or "pooled", 30, True)
            if status not in VALID_STATUSES:
                status = "pooled"
            tags = _strings(previous.get("tags"), each=100)
            for tag in _strings(item.get("tags"), each=100):
                if tag not in tags:
                    tags.append(tag)
            assessment = previous.get("editorial_assessment")
            assessment = dict(assessment) if isinstance(assessment, Mapping) else {}
            if isinstance(item.get("editorial_assessment"), Mapping):
                assessment.update(item["editorial_assessment"])
            collector_metadata = previous.get("collector_metadata")
            collector_metadata = dict(collector_metadata) if isinstance(collector_metadata, Mapping) else {}
            if isinstance(item.get("metadata"), Mapping):
                collector_metadata.update(item["metadata"])
            metadata = {
                "id": item_id,
                "title": latest("title", MAX_TITLE) or "Untitled",
                "url": latest("url", MAX_URL),
                "doi": latest("doi", 300),
                "source": latest("source", 500),
                "source_kind": latest("source_kind", 100) or "feed",
                "category": latest("category", 200) or "Uncategorized",
                "published_at": _iso(item.get("published_at")) or _iso(previous.get("published_at")),
                "first_collected_at": _iso(previous.get("first_collected_at")) or now,
                "last_collected_at": now,
                "status": status,
                "score": round(score, 6),
                "tags": tags[:100],
                "authors": _strings(item.get("authors") or previous.get("authors"), limit=30),
                "query": latest("query", 500),
                "collector_metadata": collector_metadata,
                "article": latest("article", MAX_URL),
                "editorial_assessment": assessment,
            }
            excerpt = _text(item.get("excerpt") or item.get("summary") or item.get("abstract"), MAX_EXCERPT)
            if not excerpt and old_body:
                start = old_body.find("## Summary")
                end = old_body.find("## Source", start)
                if start >= 0 and end > start:
                    excerpt = old_body[start + len("## Summary") : end].strip()
                    if excerpt == "_No summary collected._":
                        excerpt = ""
            before, after = _manual_parts(old_body)
            body = before + self._managed(metadata, excerpt) + after
            self._write(path, _dump_note(metadata, body))
            records[item_id] = path, metadata, body
            written.append(path)
            touched.add(item_id)
        if touched:
            self._write_daily_index(_day(now), touched, records=records)
        return written

    store = ingest

    def _write_daily_index(
        self,
        day: str,
        include: set[str] | None = None,
        *,
        records: Mapping[str, tuple[Path, dict[str, Any], str]] | None = None,
    ) -> Path | None:
        if not day:
            return None
        selected: list[tuple[Path, dict[str, Any]]] = []
        active_records = records if records is not None else self._records()
        for path, metadata, _body in active_records.values():
            active_days = {_day(metadata.get("first_collected_at")), _day(metadata.get("last_collected_at"))}
            if day in active_days or (include and metadata.get("id") in include):
                selected.append((path, metadata))
        selected.sort(key=lambda pair: (str(pair[1].get("source_kind")), str(pair[1].get("title")).casefold()))
        path = self._inside(self.vault / "daily" / day[:4] / f"{day}.md")
        old_body = self._read(path)[1] if path.exists() else ""
        before, after = _manual_parts(old_body)
        lines = [MANAGED_START, f"# Collected signals — {day}", ""]
        for item_path, metadata in selected:
            target = item_path.relative_to(self.vault).with_suffix("").as_posix()
            title = str(metadata.get("title") or metadata.get("id") or "Untitled").replace("|", "-").replace("]", "")
            lines.append(f"- [[{target}|{title}]] — `{metadata.get('status') or 'pooled'}`")
        if not selected:
            lines.append("_No collected signals._")
        lines.extend(["", MANAGED_END, ""])
        index_meta = yaml.safe_dump(
            {"date": day, "type": "daily-signal-index", "item_count": len(selected)},
            allow_unicode=True,
            sort_keys=False,
        ).rstrip()
        document = f"---\n{index_meta}\n---\n{(before + chr(10).join(lines) + after).lstrip()}".rstrip() + "\n"
        self._write(path, document)
        return path

    def _outcome(
        self,
        item_id: str,
        outcome: str,
        assessed_at: str,
        *,
        article: str = "",
        feedback: Mapping[str, Any] | None = None,
        records: dict[str, tuple[Path, dict[str, Any], str]] | None = None,
    ) -> Path:
        if outcome not in {"candidate", "selected", "rejected"}:
            raise ValueError(f"unsupported outcome: {outcome}")
        active_records = records if records is not None else self._records()
        if item_id not in active_records:
            raise KeyError(f"item is not in pool: {item_id}")
        path, metadata, body = active_records[item_id]
        metadata["status"] = outcome
        if article and outcome == "selected":
            metadata["article"] = _text(article, MAX_URL, True)
        assessment = metadata.get("editorial_assessment")
        assessment = dict(assessment) if isinstance(assessment, Mapping) else {}
        assessment.update({"outcome": outcome, "assessed_at": assessed_at})
        feedback = feedback or {}
        reason = _text(feedback.get("reason"), 2_000)
        if reason:
            assessment["reason"] = reason
        for field in ("relevance", "quality", "novelty"):
            value = feedback.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= float(value) <= 1:
                assessment[field] = round(float(value), 4)
        metadata["editorial_assessment"] = assessment
        self._write(path, _dump_note(metadata, body))
        active_records[item_id] = path, metadata, body
        return path

    def mark_candidates(self, bundle: Mapping[str, Any] | str | Path) -> list[Path]:
        data = _load_object(bundle)
        items = data.get("items") or []
        if not isinstance(items, list):
            raise ValueError("bundle.items must be a list")
        assessed_at = _iso(data.get("generated_at"), datetime.now(timezone.utc))
        self.ingest(items, assessed_at)
        records = self._records()
        result = [
            self._outcome(
                _text(_mapping(item).get("id"), MAX_ID, True),
                "candidate",
                assessed_at,
                records=records,
            )
            for item in items
        ]
        self._write_daily_index(_day(assessed_at), records=records)
        return result

    @staticmethod
    def _selected(feedback: Mapping[str, Any]) -> list[str]:
        result: list[str] = []
        if isinstance(feedback.get("source_ids"), list):
            result.extend(_text(value, MAX_ID, True) for value in feedback["source_ids"])
        if isinstance(feedback.get("items"), list):
            for item in feedback["items"]:
                if not isinstance(item, Mapping):
                    continue
                if item.get("id"):
                    result.append(_text(item["id"], MAX_ID, True))
                if isinstance(item.get("source_ids"), list):
                    result.extend(_text(value, MAX_ID, True) for value in item["source_ids"])
        return list(dict.fromkeys(value for value in result if value))

    def record_editorial_outcomes(
        self,
        bundle: Mapping[str, Any] | str | Path,
        feedback: Mapping[str, Any] | str | Path,
        article: str | Path | None = None,
    ) -> dict[str, list[Path]]:
        bundle_data = _load_object(bundle)
        feedback_data = _load_object(feedback)
        candidates = bundle_data.get("items") or []
        if not isinstance(candidates, list):
            raise ValueError("bundle.items must be a list")
        self.mark_candidates(bundle_data)
        candidate_ids = [_text(_mapping(item).get("id"), MAX_ID, True) for item in candidates]
        selected = set(self._selected(feedback_data))
        unknown = selected - set(candidate_ids)
        if unknown:
            raise ValueError(f"selected IDs are absent from bundle: {sorted(unknown)}")
        rows = feedback_data.get("candidate_feedback") or []
        by_id = {
            _text(row.get("id"), MAX_ID, True): row
            for row in rows
            if isinstance(row, Mapping) and row.get("id")
        }
        assessed_at = _iso(feedback_data.get("generated_at") or bundle_data.get("generated_at"), datetime.now(timezone.utc))
        article_value = _text(article or feedback_data.get("article"), MAX_URL, True)
        records = self._records()
        result: dict[str, list[Path]] = {"selected": [], "rejected": []}
        for item_id in candidate_ids:
            status = "selected" if item_id in selected else "rejected"
            result[status].append(
                self._outcome(
                    item_id,
                    status,
                    assessed_at,
                    article=article_value,
                    feedback=by_id.get(item_id),
                    records=records,
                )
            )
        self._write_daily_index(_day(assessed_at), records=records)
        return result

    mark_editorial = record_editorial_outcomes
    mark_editorial_outcomes = record_editorial_outcomes

    def search(
        self,
        query: str = "",
        *,
        status: str | None = None,
        source_kind: str | None = None,
        tags: Iterable[str] | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        terms = [part.casefold() for part in _text(query, 1_000, True).split()]
        wanted_tags = {str(tag).casefold() for tag in tags or []}
        result: list[dict[str, Any]] = []
        for path, metadata, body in self._records().values():
            if status and metadata.get("status") != status:
                continue
            if source_kind and str(metadata.get("source_kind", "")).casefold() != source_kind.casefold():
                continue
            actual_tags = {tag.casefold() for tag in _strings(metadata.get("tags"), each=100)}
            if wanted_tags and not wanted_tags.issubset(actual_tags):
                continue
            haystack = " ".join(map(str, metadata.values())).casefold() + " " + body.casefold()
            if not all(term in haystack for term in terms):
                continue
            row = {field: metadata.get(field) for field in FRONT_MATTER_FIELDS}
            row["path"] = path.relative_to(self.vault).as_posix()
            result.append(row)
        result.sort(key=lambda row: (float(row.get("score") or 0), str(row.get("published_at") or "")), reverse=True)
        return result[: max(0, min(int(limit), MAX_RESULTS))]

    def report(self) -> dict[str, Any]:
        records = self._records()
        status = Counter(str(meta.get("status") or "unknown") for _path, meta, _body in records.values())
        kind = Counter(str(meta.get("source_kind") or "unknown") for _path, meta, _body in records.values())
        category = Counter(str(meta.get("category") or "Uncategorized") for _path, meta, _body in records.values())
        return {
            "total": len(records),
            "status": dict(sorted(status.items())),
            "source_kind": dict(sorted(kind.items())),
            "category": dict(sorted(category.items())),
        }

    def rebuild(self) -> dict[str, Any]:
        records = self._records()  # Validates IDs and duplicate canonical notes.
        days: set[str] = set()
        for _path, metadata, _body in records.values():
            days.update(filter(None, (_day(metadata.get("first_collected_at")), _day(metadata.get("last_collected_at")))))
        for day in sorted(days):
            self._write_daily_index(day)
        result = self.report()
        result["daily_indexes"] = len(days)
        return result


def ingest(vault: str | Path, items: Iterable[Any], collected_at: datetime | str | None = None) -> list[Path]:
    return KnowledgePool(vault).ingest(items, collected_at)


store = ingest


def search(vault: str | Path, query: str = "", **filters: Any) -> list[dict[str, Any]]:
    return KnowledgePool(vault).search(query, **filters)


def report(vault: str | Path) -> dict[str, Any]:
    return KnowledgePool(vault).report()


def rebuild(vault: str | Path) -> dict[str, Any]:
    return KnowledgePool(vault).rebuild()
