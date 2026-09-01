# GPT handoff

Scheduled ChatGPT tasks exchange bounded, auditable artifacts here. Hugo publication lives in the separate `naoyamd/daily-signal` repository.

This file and `policy.yaml` are the canonical operating contract. Every scheduled stage must read both files before work. When a task prompt is less specific than these files, these files supply the missing detail. Never loosen a safety or evidence rule merely to produce an output.

Reference artifacts:

- `examples/scout-v2.example.json`: strict Scout envelope
- `examples/curated-v1.example.json`: claim-level evidence and editorial-plan example
- `DRY_RUN.md`: production-equivalent test procedure without publication

## Daily flow (Asia/Tokyo, weekdays)

- 06:10 `Company Scout` -> `company/YYYY-MM-DD.json`
- 06:20 `Research Scout` -> `research/YYYY-MM-DD.json`
- 07:05 `Signal Curator` -> `curated/YYYY-MM-DD.json`
- 07:20 `Daily Signal Writer` reads curated output, publishes in `naoyamd/daily-signal`, then writes `published/YYYY-MM-DD.json`

The stages are intentionally separated. Scouts maximize recall, the Curator makes editorial decisions, and the Writer converts an already-decided plan into public copy. A stage must not quietly absorb the role of another stage.

## Run isolation and readiness

All timestamps must include a timezone and all date paths use Asia/Tokyo.

Each Scout stores execution metadata under `research_plan.execution`:

- `stage`
- `policy_version`
- `local_date`
- `started_at`
- `completed_at`
- `status` (`complete` or `partial`)
- `planned_query_count`
- `executed_query_count`

A dated Scout file is written only after exploration is finished. `partial` requires an explicit warning and complete mandatory coverage records. The Curator must block when an input is missing, stale, malformed, from another local date, or lacks sufficient execution/coverage evidence. Previous-day files are never fallback inputs.

## Exact Scout contract

Company Scout and Research Scout emit strict `daily-signal-scout/v2`. Compatibility is exact, not aspirational.

Allowed top-level fields only:

- `schema`
- `generated_at`
- `items`
- `searched_queries`
- `warnings`
- `checked_sources`
- `research_plan`

Required item fields are `title`, `url`, `source`, `source_kind`, and `category`. Excerpts are at most 400 characters and must contain compact, checkable facts rather than article prose. Do not retain full page text.

Use only the source-kind vocabulary accepted by the existing validator:

- `press_release`
- `technical_report`
- `paper`
- `standard`
- `official`
- `news`
- `journal`
- `conference`
- `corporate_tech`

Map preprints to `paper`, white papers/surveys to `technical_report`, product announcements to `press_release`, official GitHub repositories to `official`, and company technical blogs to `corporate_tech`.

`checked_sources` is also strict:

- `found` requires a non-empty `query`
- `no_new_finding` requires a non-empty `warning` explaining that no qualifying new item was found
- `unreachable` requires a non-empty `warning` describing the access problem
- every active watch source must appear exactly once, using the same source name

This mirrors the repository validator, which rejects an entire strict payload when one item or coverage record violates the contract.

## Company Scout

Read `config/sources.yaml`, `policy.yaml`, and `state/company-watch.json` before selecting companies.

The explicit state file is authoritative when valid. Select seven mandatory watch sources starting at `next_index`. If fewer than five verified candidate items are found after those seven and the exploration budget remains, continue along the same rotation for up to five spillover companies. Discovery organizations outside the required watchlist do not advance the watch cursor.

After the dated handoff commit succeeds, update `state/company-watch.json` with:

- the new cursor
- `last_checked` for every mandatory/spillover watch source actually checked
- the watchlist hash
- the dated handoff path and commit in `last_run`

If the state update fails, keep the dated handoff and report the state failure. It is safer to repeat a company than to skip one silently. Use recent dated files only to reconstruct state when the explicit state file is missing or invalid.

Company exploration must distinguish a newly published signal from an undated evergreen product page. A relevant evergreen page may serve as background for verification, but it is not a new candidate by itself.

## Research Scout

Research Scout uses the rotating query lanes in `policy.yaml`. The targets are coverage guidance rather than filler quotas. Record zero-result queries and access limitations so the Curator can distinguish “not searched” from “searched and found nothing.”

The candidate pool should preserve useful uncertainty and adjacent signals. It must not collapse to famous model announcements. Apply only broad hygiene at Scout time: public HTTPS URL, date plausibility, source-kind compliance, obvious duplicate removal, and a per-domain flood cap. Editorial relevance decisions belong to the Curator.

## Curator contract

The Curator consumes only valid same-day Scout artifacts and emits `daily-signal-curated/v1`.

Required top-level fields:

- `schema`
- `generated_at`
- `source_files`
- `status`
- `summary`
- `editorial_plan`
- `coverage_audit`
- `selected`
- `wildcards`
- `backlog`
- `rejected`
- `warnings`

`source_files` must record each input path and the exact GitHub blob/commit identifier observed. This prevents mixing two revisions from different moments.

For each selected or wildcard item, preserve:

- a stable `event_key`
- `tier` (`lead`, `standard`, or `report`)
- primary and supporting URLs
- compact factual points
- claim-level evidence mapping
- scores and weighted score
- confidence and provenance

Each entry in `claims` contains:

- `text`
- `source_url`
- `verification_status`
- `attribution_type`
- `evidence_location`

A claim may be `verified`, `partially_verified`, or `unverified`. Vendor benchmarks and vendor-funded surveys must be labeled as self-reported or sponsored rather than rewritten as independent fact. Unverified material claims cannot enter `selected`.

The Curator creates an `editorial_plan` with lead IDs, final order, central signals, important tensions/caveats, and desired depth. This is structural guidance, not publishable prose. It prevents the Writer from redoing selection and flattening every item to the same length.

The Curator also writes `coverage_audit`, including input and deduplicated counts, source/organization diversity, geographic coverage, lane coverage, and known gaps. A polished selection without a coverage record is not a successful curation run.

Use `state/published-index.json` plus recent receipts for duplicate control. Exact URLs remain suppressed for 90 days; the same semantic event is normally suppressed for 30 days. A repeat is allowed only for a material delta, and the delta must be explicit in provenance.

## Published receipt and index

After a Writer successfully commits an article, it writes `published/YYYY-MM-DD.json` with at least:

- `schema: daily-signal-published/v1`
- `published_at`
- `article_repository`
- `article_path`
- `article_commit`
- `curated_source`
- `published_item_ids`
- `event_keys`
- `primary_urls`
- `organizations`
- `tags`
- `title`
- `quality_metrics`

`quality_metrics` records selected/wildcard retention, claim retention, report-metric retention, verification failures, dropped IDs/claims, and article character count. These metrics reveal whether the Writer has silently simplified the Curator output.

It then updates `state/published-index.json`, retaining 180 days of compact history. Publication history is not article content; it exists for deterministic duplicate suppression and auditability.

A success receipt must never be written before the article commit. If the article commits but the receipt/index update fails, report `published_with_state_warning` rather than pretending the article was not published.

## Failure philosophy

- Do not invent missing dates, figures, URLs, or coverage.
- Do not backfill a failed day with stale material.
- Do not turn a vendor claim into an independently established result.
- Do not force target counts when evidence is weak.
- Preserve warnings and partial coverage instead of hiding them.
- Prefer a visible blocked/partial run to a plausible-looking but unauditable article.
