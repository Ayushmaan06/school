# Context

**Current task.** School intelligence tool for Tensor School BD ,  built and
working. National enrichment is part-done: 7,442 of 33,636 schools visited.

**Key decisions.**
- `extract --reextract` is now INCREMENTAL, keyed on `extraction_runs`
  (content_hash, pipeline version). It is a parser-change operation, not a
  per-enrichment one. Bump an extractor's `_v1` to re-read its documents.
- Live enrichment mines the homepage for phone/email when a school has no
  disclosure page ,  that is where most contacts actually are (~60% yield).
- A 5xx or unreachable homepage short-circuits to `dead_origin` instead of
  trying five guessed paths against a server that is not answering.

**Next steps.**
- One full `extract --reextract --full` (~3h, now resumable) to backfill
  homepage contacts for the ~3,822 schools enriched before 2026-09-08.
  Do NOT backfill `extraction_runs` from `observations` instead ,  those
  homepages have observations but were never mined for contacts.
- Resume `bash scripts/enrich-chunked.sh` (26k schools left, ~8/min).
- Group resolution: 219 Narayana and 199 Sri Chaitanya campuses sit unlinked
  (`group_id` null); linking them needs no new source.

See `STATUS.md` for full state and `RUNBOOK.md` for how to operate it.
