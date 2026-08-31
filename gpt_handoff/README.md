# GPT handoff

Scheduled ChatGPT tasks exchange bounded artifacts here. Hugo publication itself lives in the separate `daily-signal` repository.

## Daily flow (Asia/Tokyo, weekdays)

- 06:10 `Company Scout` -> `company/YYYY-MM-DD.json`
- 06:20 `Research Scout` -> `research/YYYY-MM-DD.json`
- 07:05 `Signal Curator` -> `curated/YYYY-MM-DD.json`
- 07:20 `Daily Signal Writer` reads curated output, publishes in `naoyamd/daily-signal`, then -> `published/YYYY-MM-DD.json`

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

When useful, the curator should inspect recent `published/*.json` receipts to avoid re-selecting a topic that has already been published unless there is a material new development.

## Published receipt

After the Writer successfully commits a Hugo article, it writes `published/YYYY-MM-DD.json`. The receipt is publication history, not article content. It should include at least:

- `schema: daily-signal-published/v1`
- `published_at`
- `article_repository`
- `article_path`
- `article_commit`
- `curated_source`
- `published_item_ids`
- `primary_urls`
- `title`

A publication receipt must only be written after the article commit succeeds. If article publication fails, no success receipt is written.

## Separation of responsibilities

Scouts maximize recall and preserve useful noise. They do not write publishable prose. The curator deduplicates, verifies, scores, and selects. The Writer consumes only same-day curated output, re-checks adopted source URLs, writes the Hugo article, and records the publication receipt. The Writer does not perform broad discovery.
