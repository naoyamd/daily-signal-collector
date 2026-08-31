# GPT handoff

Scheduled ChatGPT scouts write bounded intermediate artifacts here. Hugo/article generation is intentionally out of scope.

## Daily flow (Asia/Tokyo, weekdays)

- 06:10 `Company Scout` -> `company/YYYY-MM-DD.json`
- 06:20 `Research Scout` -> `research/YYYY-MM-DD.json`
- 07:05 `Signal Curator` -> `curated/YYYY-MM-DD.json`

`Company Scout` and `Research Scout` use the existing `daily-signal-scout/v2` envelope where practical. The curator emits `daily-signal-curated/v1`.

## Curator contract

The curator must read both same-day scout files. It must fail closed if either file is missing, stale, malformed, or clearly incomplete. It must not silently substitute a previous day's file.

`daily-signal-curated/v1` top-level fields:

- `schema`
- `generated_at`
- `source_files`
- `status`
- `summary`
- `selected`
- `wildcards`
- `backlog`
- `rejected`
- `warnings`

Every selected/wildcard/backlog item should retain its source URL and factual provenance. Selection is editorial triage, not article writing.

## Separation of responsibilities

Scouts maximize recall and preserve useful noise. They do not write publishable prose. The curator deduplicates, verifies, scores, and selects. A future Writer may consume only `curated/YYYY-MM-DD.json` and re-check adopted source URLs before creating Hugo content.
