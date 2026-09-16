# Agent Instructions

For any AI coding agent working in this repository. Read this before your first
edit.

## Read these first, in this order

1. `docs/DECISIONS.md` ,  why the architecture is what it is, and which parts of
   `Project-Doc.md` were rejected. **Non-negotiable context.**
2. `docs/ARCHITECTURE.md` ,  stages, contracts, repo layout.
3. `docs/DATA-MODEL.md` ,  the schema is a specification, not a suggestion.
4. `docs/IMPLEMENTATION-PLAN.md` ,  find your task; it has the edge cases and tests.
5. Whichever of `docs/SOURCES.md`, `docs/SCORING.md`,
   `docs/ENTITY-RESOLUTION.md`, `docs/COMPLIANCE.md` covers your task.

`Project-Doc.md` is **historical context only.** It describes an earlier and
materially different understanding of the problem. Where it conflicts with
`docs/`, `docs/` wins. Do not implement from `Project-Doc.md`.

## What this system is

An internal tool that ranks Indian institutions by how good a channel they are for
recruiting class 11-12 PCM students into Tensor School's Rs 24L B.Tech program in
Bengaluru, and tells BD who to contact there.

It is **not** a B2B school-partnership CRM, and it is **not** a student database.
If a change makes sense only under one of those readings, it is the wrong change.

## Hard rules

Violating any of these is a defect regardless of what it enables. Do not add a
flag, an environment variable, or a "temporary" bypass for any of them.

1. **Never store student-level data.** No names, marks, ranks, photos, or contacts
   of any student. The `fetch/denylist.py` gate runs **before** the HTTP request.
   See `docs/COMPLIANCE.md` section 1. This is a legal boundary (DPDP Act 2023
   s.9), not a preference.
2. **Never use Google Places or Maps as a data source.** Their terms permit
   storing only `place_id`. ADR-003.
3. **A user request never triggers a fetch or an LLM call.** Reads hit Postgres
   only. ADR-012, `docs/ARCHITECTURE.md` Rule 1.
4. **Never delete from `observations`, `fetches`, or `raw_documents`.** They are
   append-only and they are the truth. ADR-009.
5. **Never auto-merge an entity pair inside the ambiguity band** (0.65-0.90).
   Route to `review_queue`. `docs/ENTITY-RESOLUTION.md`.
6. **Never write a guessed value.** A missing observation always beats a wrong
   one. Failed extraction goes to `review_queue`, not to a default.
7. **Never blend Confidence into Fit.** Two numbers. ADR-010.
8. **Never hardcode a scoring weight, band, or geography value in Python.** They
   live in `school_intel/score/scoring.yaml`. `docs/SCORING.md`.
9. **Never let an LLM emit SQL, choose an ordering, or influence a score.**
   Extraction and NL-to-`FilterSpec` translation only. ADR-013.
10. **Never skip the structure-drift guard** on registry parsers. A snapshot with
    under 50% of the previous row count aborts. Without this, one parser
    regression emits thousands of false `disaffiliation` signals.

## Where AI belongs, and where it does not

`Project-Doc.md` section 5.4 got this right and it is retained.

**Extraction is deterministic-first (ADR-013).** The default pipeline uses **no
model inference and no API key.** Three tiers:

| Tier | Method | Default |
|---|---|---|
| 1 | Label + table parser ,  `selectolax` (HTML), `pdfplumber` (PDF) | **on** |
| 2 | Pattern extractors ,  emails, phones, Indian numerals, stream keywords | **on** |
| 3 | LLM fallback, `claude-haiku-4-5` | **off** ,  `EXTRACTION_LLM_ENABLED=false` |

This works because the main enrichment target is a **standardised statutory form**
(CBSE Appendix-IX), not prose. Do not "upgrade" it to an LLM-first design.

**Deterministic code, always:** search, filtering, sorting, aggregation,
geography, scoring, conflict resolution, entity matching, group resolution, signal
detection. Auditable, reproducible, and none of them benefit from a model.

**Rules that apply to every tier, parser and model alike:**

- **Every extracted field carries an `evidence_span`** ,  the verbatim matched
  source text. A parser records the matched row exactly as a model would record
  its span. An extraction without evidence is a bug; it breaks
  `/institutions/{id}/why`, which must not care which tier produced a value.
- **No match is a normal outcome.** Emit nothing and let the field stay unknown.
  Never fall back to "the first number on the page" ,  that is hard rule 6.
- Extractors are versioned and interchangeable. `observations.extractor` records
  `parser:mpd_table_v1` / `regex:mpd_patterns_v1` / `llm:haiku-4.5/mpd_v1`.
  Re-running a version **adds** rows and never mutates old ones, so versions are
  comparable over identical stored documents via `make eval`.
- Labels and patterns live in `labels.yaml`, never hardcoded ,  proforma wording
  varies between schools and the synonym list will grow.
- The student-data check runs in every tier. `docs/COMPLIANCE.md`.

**If you touch tier 3:** keep the `anthropic` import lazy, keep the flag default
`false`, and only ever invoke it for a decision-critical field that tiers 1-2
missed. The extract stage must run to completion with **no `ANTHROPIC_API_KEY`
set** ,  there is a test for this. Only turn it on with a `make eval` delta showing
deterministic coverage is genuinely inadequate for a specific field.

## Do not add these

Each was considered and rejected with a documented promotion trigger in
`docs/ARCHITECTURE.md`. Adding one speculatively is over-engineering, not
foresight.

Scrapy · Playwright · Celery · Redis · OpenSearch/Elasticsearch · React/Next.js ·
an embedding pipeline · a news ingestion pipeline · a message broker · Docker
Compose with more than Postgres · an ORM abstraction layer over SQLAlchemy · a
generic "plugin framework" for sources · Google Places.

If you believe one is genuinely needed, measure the trigger condition, write it
into `docs/DECISIONS.md` as a new ADR, and say so in the PR. Do not just add it.

## Code conventions

- Python 3.13, managed with `uv`. `ruff` for lint and format, default config.
- SQLAlchemy 2.0 style (`select()`, not legacy `Query`).
- `async` for I/O-bound work (fetch, LLM); plain sync for CPU and database work.
  Do not make everything async for symmetry.
- Pydantic v2 for every external boundary: config, LLM schemas, API request and
  response models.
- Type hints on all public functions. No `Any` without a comment explaining why.
- Config in YAML next to the code that reads it, loaded through
  `config.Settings`. `os.environ` is read **only** in `config.py`.
- One module per source. No inheritance hierarchy ,  a `Protocol` and a plain
  function are enough.
- Deliberate simplifications get a `ponytail:` comment naming the ceiling and the
  upgrade path, e.g.
  `# ponytail: full recompute; incremental resolve if this exceeds 10 min`.

## Testing expectations

Proportionate, not exhaustive. Test the logic that would silently produce wrong
data.

**Mandatory ,  a PR touching these without tests should be rejected:**

| Area | Test file | Why |
|---|---|---|
| PII denylist | `tests/unit/test_denylist.py` | Legal boundary. Assert **both** directions: result/merit/topper blocked, `fee-structure` allowed. Plus: a page whose text contains the proforma heading "C: RESULT AND ACADEMICS" is **not** blocked ,  patterns are URL-only. |
| Normalisation | `tests/unit/test_normalize.py` | Table-driven, every case in `docs/ENTITY-RESOLUTION.md`. Includes `parse_inr` returning `None` on ambiguous periods. |
| Match hard blockers | `tests/unit/test_match.py` | A false merge silently deletes a lead. |
| Group franchise case | `tests/unit/test_groups.py` | Same brand + different trust must never merge. |
| Rebuild contract | `tests/integration/test_rebuild.py` | Four properties. The load-bearing test of the architecture. |
| Scoring bands | `tests/unit/test_fit.py` | Every affordability boundary; a fencepost bug mis-ranks the whole list. |
| Signal diff guard | `tests/unit/test_diff.py` | Row-count guard must fire. |

**No live network calls in tests.** Use `respx` for HTTP and recorded fixtures for
LLM responses. Checked-in HTML fixtures live in `tests/fixtures/`.

**Extraction changes must report an eval delta.** `make eval` output goes in the PR
description. A prompt or model change without it is not reviewable (ADR-015).

## Common wrong assumptions

Things a reasonable agent might assume that are wrong here:

| Assumption | Reality |
|---|---|
| "Bigger school = better lead" | **No.** Class-12 PCM headcount matters; total enrollment barely does. `docs/SCORING.md` |
| "Higher fees = better lead" | **No.** It is a band function that peaks at Rs 1.5-3L/yr. ADR-011 |
| "Import all CBSE schools" | **No.** Filter to Senior Secondary at import. A third of the list has no class 12 and zero value. |
| "Import all of India" | **Yes for the registry, no for enrichment.** ADR-017 separates them: the national list is ~38 POSTs; MPD enrichment is 3-6 fetches per school and follows `enrichment_priority` (KA, TN, MH, TG first). |
| "OCR is banned like Playwright" | **No.** ADR-016 adds local OCR for scanned fee PDFs. It is deterministic, offline, needs no API key, and does not weaken ADR-013. It does carry a mandatory wrong-value guard. |
| "Scoring needs every importer first" | **No.** Scoring depends on M2-4 (eval), M4-1b and M1-7 ,  not on M3 or M4-1. A CBSE-only ranked list ships after M2. |
| "Crawl the school's website" | **No.** Fetch the Mandatory Disclosure page and stop. Cap 6 fetches per institution. |
| "`schools.board` is a column" | **No.** `institution_boards` is many-to-many. Board filters are `EXISTS` subqueries. |
| "`opportunity_score` is a column on the entity" | **No.** Versioned rows in `scores`. |
| "Chains means school chains" | **No.** Coaching and PU chains. ADR-008. |
| "Missing data means a low score" | **No.** It reduces Confidence; Fit renormalises over known components. ADR-010 |
| "Rebuild truncates the canonical tables" | **Not with CASCADE.** `TRUNCATE institutions CASCADE` deletes `observations` and `signals` too - CASCADE ignores `ON DELETE` rules. Institutions are upserted and pruned. `docs/DATA-MODEL.md` |
| "Re-extraction needs re-crawling" | **No.** Raw bytes are stored. `cli extract --reextract` makes zero fetches and zero API calls. |
| "Extraction needs an LLM" | **No.** Appendix-IX is a standardised form. Tiers 1-2 are parsers; tier 3 is off by default. ADR-013 |
| "Use a local BERT model instead" | **Considered and rejected.** Needs labelled training data we don't have, mangles Indian numerals, and is slower than regex. ADR-013 |
| "`region` is North/West/South/East" | **Dropped.** Useless for this ICP. Geography is distance from Bengaluru, config-driven. |
| "Signals come from news" | **No.** Registry snapshot diffs. The news pipeline is demoted. ADR-002 |

## When you are unsure

- **A `[BUSINESS]` item in `docs/DECISIONS.md`** ,  ask. Do not invent an answer and
  do not pick a default that looks reasonable.
- **A `[UNVERIFIED]` source in `docs/SOURCES.md`** ,  spend the hour confirming the
  real shape before writing the importer, then update the doc with what you found.
- **A conflict between two docs** ,  the more specific document wins
  (`docs/SCORING.md` over `docs/ARCHITECTURE.md`). Flag the conflict so it gets
  fixed rather than silently resolved twice.
- **A schema change looks necessary** ,  write the ADR first. The schema is
  deliberately designed to absorb Tier D institutions, more boards, and more
  sources without migration; if you think you need a migration, you may be
  misreading the model.

## Definition of done

A task is done when:

- Its own DoD in `docs/IMPLEMENTATION-PLAN.md` is met.
- `ruff check` and `ruff format --check` pass.
- Its mandatory tests (above) exist and pass.
- No hard rule is violated.
- Anything discovered that contradicts the docs has been written back into the
  docs in the same change. **Stale documentation is a defect here** ,  the whole
  point of `docs/` is that the next agent does not have to rediscover the
  reasoning.
- New `ponytail:` shortcuts name their ceiling and upgrade path.
