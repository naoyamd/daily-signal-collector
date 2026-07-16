"""Apply publisher outcome events to the Markdown Vault across the JSON boundary."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from scripts.knowledge_pool import KnowledgePool


FEEDBACK_SCHEMA = "daily-signal-feedback/v1"
CANDIDATES_SCHEMA = "daily-signal-candidates/v1"


def _objects(root: Path) -> list[tuple[Path, dict[str, Any]]]:
    files = sorted(root.rglob("*.json")) if root.is_dir() else [root]
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in files:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(value, dict):
            result.append((path, value))
    return result


def apply_feedback(vault: Path, feedback_root: Path, candidates_root: Path) -> dict[str, Any]:
    handoffs = {
        str(value.get("batch_id")): value
        for _path, value in _objects(candidates_root)
        if value.get("schema") == CANDIDATES_SCHEMA and value.get("batch_id")
    }
    pool = KnowledgePool(vault)
    report: dict[str, Any] = {"applied": 0, "skipped": 0, "errors": []}
    for path, event in _objects(feedback_root):
        if event.get("schema") != FEEDBACK_SCHEMA:
            report["skipped"] += 1
            continue
        bundle = handoffs.get(str(event.get("batch_id") or ""))
        if bundle is None:
            report["errors"].append({"file": str(path), "error": "candidate batch not found"})
            continue
        normalized = {
            **event,
            "source_ids": list(event.get("selected_ids") or []),
            "article": event.get("article_id"),
        }
        try:
            pool.record_editorial_outcomes(bundle, normalized, article=event.get("article_id"))
            report["applied"] += 1
        except (OSError, KeyError, TypeError, ValueError) as exc:
            report["errors"].append({"file": str(path), "error": str(exc)})
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--vault", type=Path, required=True)
    parser.add_argument("--feedback", type=Path, required=True)
    parser.add_argument("--candidates", type=Path, required=True)
    args = parser.parse_args()
    report = apply_feedback(args.vault, args.feedback, args.candidates)
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
