#!/usr/bin/env python3
"""Validate the newest committed GPT pipeline dry run without publishing it."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from scripts.web_scout import diagnose_scout_handoff

REQUIRED_CURATED = {
    "schema",
    "generated_at",
    "source_files",
    "status",
    "summary",
    "editorial_plan",
    "coverage_audit",
    "selected",
    "wildcards",
    "backlog",
    "rejected",
    "warnings",
}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: JSON root must be an object")
    return value


def git_blob(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", f"HEAD:{path.as_posix()}"],
        check=True,
        text=True,
        capture_output=True,
    )
    return result.stdout.strip()


def latest_run(root: Path) -> Path:
    runs = sorted(path for path in root.iterdir() if path.is_dir() and (path / "article-preview.md").exists())
    if not runs:
        raise ValueError("no completed dry-run preview found")
    return runs[-1]


def validate_scout(path: Path, reference_now: datetime) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = load_json(path)
    diagnostics = diagnose_scout_handoff(
        payload,
        now=reference_now,
        max_items=80,
        max_age_hours=3,
        research_plan=payload.get("research_plan"),
    )
    if not diagnostics.get("valid"):
        raise ValueError(f"{path}: strict scout validation failed: {diagnostics.get('errors')}")
    execution = payload.get("research_plan", {}).get("execution", {})
    required_execution = {
        "stage",
        "policy_version",
        "local_date",
        "started_at",
        "completed_at",
        "status",
        "planned_query_count",
        "executed_query_count",
    }
    missing = sorted(required_execution - set(execution))
    if missing:
        raise ValueError(f"{path}: missing execution fields: {', '.join(missing)}")
    if execution.get("status") not in {"complete", "partial"}:
        raise ValueError(f"{path}: invalid execution status")
    return payload, diagnostics


def validate_curated(run: Path, company: dict[str, Any], research: dict[str, Any]) -> dict[str, Any]:
    path = run / "curated.json"
    curated = load_json(path)
    missing = sorted(REQUIRED_CURATED - set(curated))
    if missing:
        raise ValueError(f"{path}: missing fields: {', '.join(missing)}")
    if curated.get("schema") != "daily-signal-curated/v1":
        raise ValueError(f"{path}: unsupported schema")
    if curated.get("status") != "ready":
        raise ValueError(f"{path}: status is not ready")

    generated = datetime.fromisoformat(str(curated["generated_at"]))
    if generated.tzinfo is None:
        raise ValueError(f"{path}: generated_at lacks timezone")
    local_date = generated.date().isoformat()
    for source_payload, name in ((company, "company.json"), (research, "research.json")):
        source_generated = datetime.fromisoformat(str(source_payload["generated_at"]))
        if source_generated.date().isoformat() != local_date:
            raise ValueError(f"{path}: {name} local date mismatch")
        if source_generated > generated:
            raise ValueError(f"{path}: {name} is newer than curated output")
        if (generated - source_generated).total_seconds() > 3 * 3600:
            raise ValueError(f"{path}: {name} is stale at curation time")

    expected_sources = {
        (run / "company.json").as_posix(): git_blob(run / "company.json"),
        (run / "research.json").as_posix(): git_blob(run / "research.json"),
    }
    recorded = {str(item.get("path")): str(item.get("blob_sha")) for item in curated.get("source_files", [])}
    if recorded != expected_sources:
        raise ValueError(f"{path}: source_files do not match committed upstream blobs: {recorded} != {expected_sources}")

    selected = curated.get("selected")
    wildcards = curated.get("wildcards")
    if not isinstance(selected, list) or not isinstance(wildcards, list):
        raise ValueError(f"{path}: selected/wildcards must be arrays")
    if not 8 <= len(selected) <= 12:
        raise ValueError(f"{path}: selected count outside policy range: {len(selected)}")
    if not 1 <= len(wildcards) <= 3:
        raise ValueError(f"{path}: wildcard count outside policy range: {len(wildcards)}")

    selected_ids = [str(item.get("id")) for item in selected]
    wildcard_ids = [str(item.get("id")) for item in wildcards]
    if len(selected_ids) != len(set(selected_ids)) or len(wildcard_ids) != len(set(wildcard_ids)):
        raise ValueError(f"{path}: duplicate curated IDs")
    if set(selected_ids) & set(wildcard_ids):
        raise ValueError(f"{path}: item appears in both selected and wildcards")

    plan = curated.get("editorial_plan")
    if not isinstance(plan, dict) or plan.get("ordered_ids") != selected_ids:
        raise ValueError(f"{path}: editorial_plan order does not match selected order")
    lead_ids = plan.get("lead_ids")
    if not isinstance(lead_ids, list) or not 2 <= len(lead_ids) <= 3 or not set(lead_ids) <= set(selected_ids):
        raise ValueError(f"{path}: invalid lead_ids")

    allowed_status = {"verified", "partially_verified"}
    allowed_tiers = {"lead", "standard", "report"}
    for group_name, items in (("selected", selected), ("wildcards", wildcards)):
        for index, item in enumerate(items):
            if item.get("tier") not in allowed_tiers:
                raise ValueError(f"{path}: {group_name}[{index}] invalid tier")
            for field in (
                "id", "event_key", "title", "primary_url", "published_at", "organization",
                "category", "source_kind", "factual_points", "claims", "why_it_matters",
                "practical_implication", "tags", "scores", "weighted_score", "confidence", "provenance",
            ):
                if field not in item:
                    raise ValueError(f"{path}: {group_name}[{index}] missing {field}")
            claims = item.get("claims")
            if not isinstance(claims, list) or not claims:
                raise ValueError(f"{path}: {group_name}[{index}] has no claims")
            for claim in claims:
                if claim.get("verification_status") not in allowed_status:
                    raise ValueError(f"{path}: unverified material claim in {group_name}[{index}]")
                if not str(claim.get("source_url", "")).startswith("https://"):
                    raise ValueError(f"{path}: claim lacks public HTTPS source")
            if item.get("tier") == "report" and not isinstance(item.get("report_details"), dict):
                raise ValueError(f"{path}: report item lacks report_details")

    audit = curated.get("coverage_audit")
    if not isinstance(audit, dict):
        raise ValueError(f"{path}: coverage_audit missing")
    if audit.get("input_item_count") != len(company.get("items", [])) + len(research.get("items", [])):
        raise ValueError(f"{path}: input_item_count mismatch")
    return curated


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path("gpt_handoff/dry_runs"))
    parser.add_argument("--run", type=Path)
    args = parser.parse_args()

    run = args.run or latest_run(args.root)
    curated_seed = load_json(run / "curated.json")
    reference_now = datetime.fromisoformat(str(curated_seed["generated_at"]))
    company, company_diag = validate_scout(run / "company.json", reference_now)
    research, research_diag = validate_scout(run / "research.json", reference_now)
    curated = validate_curated(run, company, research)

    output = {
        "run": run.as_posix(),
        "company_valid": True,
        "research_valid": True,
        "curated_valid": True,
        "company_items": len(company.get("items", [])),
        "research_items": len(research.get("items", [])),
        "selected": len(curated.get("selected", [])),
        "wildcards": len(curated.get("wildcards", [])),
        "company_diagnostics": company_diag,
        "research_diagnostics": research_diag,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
