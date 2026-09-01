# Auditable dry run

A dry run is not a single conversational answer that imitates all stages. It must exercise the same artifacts and role boundaries as production while suppressing the final publication commit.

## Required isolation

Use a unique run key such as `YYYY-MM-DDTHHMM-JST` and store artifacts under:

```text
gpt_handoff/dry_runs/<run-key>/
  company.json
  research.json
  curated.json
  article-preview.md
  evaluation.json
```

Company Scout, Research Scout, Curator, and Writer-preview must be executed as logically independent stages. Each downstream stage reads only the committed upstream files; it must not rely on facts retained in the same conversation context.

## Procedure

1. Company Scout writes strict `daily-signal-scout/v2` to `company.json`.
2. Research Scout writes strict `daily-signal-scout/v2` to `research.json`.
3. Validate both files with the existing strict validator contract. Any unsupported field, source kind, missing coverage detail, stale timestamp, or excerpt over 400 characters fails the Scout stage.
4. Curator records the exact upstream blob identifiers in `source_files` and writes `daily-signal-curated/v1`.
5. Writer-preview reads only `curated.json`, opens only adopted URLs for final verification, and writes `article-preview.md`. It must not write to `naoyamd/daily-signal/content/daily/`.
6. Write `evaluation.json` with the metrics defined in `policy.yaml` and all stage warnings.

## Minimum acceptance criteria

- both Scout payloads pass strict `daily-signal-scout/v2` validation
- mandatory company coverage is 100%; every active source has a valid `checked_sources` entry
- Curator `status` is `ready`
- every selected material claim has a source URL and non-`unverified` status
- self-reported benchmarks and sponsored surveys retain attribution
- exact and semantic duplicates are documented rather than silently discarded
- Writer-preview preserves Curator order and item tiers
- selected retention ratio is at least 0.80 unless fact verification explains every removal
- claim retention ratio is at least 0.75
- report metric retention ratio is at least 0.80
- no prior-day fallback, invented date, invented model identity, or internal stage name appears in public prose

## Evaluation questions

The review should answer these rather than merely judging whether the article reads well:

1. Which query lanes returned useful candidates, and which produced only noise?
2. What percentage of Company and Research candidates overlapped?
3. Which selected claims failed final verification, and why were they not caught earlier?
4. Did the Curator choose a coherent set without overrepresenting one vendor, model family, or source domain?
5. Did the Writer preserve mechanisms, denominators, caveats, and attribution, or replace them with generic prose?
6. Were high-quality reports and engineering signals given enough depth relative to routine model announcements?
7. Did any stage succeed only because it reused unstated conversational memory?

A run that cannot answer these questions is a demonstration, not a pipeline test.
