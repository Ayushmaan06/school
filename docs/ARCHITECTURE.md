# Architecture

Read `docs/DECISIONS.md` first. It explains *why* this shape was chosen and which
parts of `Project-Doc.md` were rejected. This document explains *what* to build.

## One-line summary

Import authoritative registries, store raw bytes immutably, extract claims with
an LLM into an append-only observations log, resolve those claims deterministically
into a canonical institution table, score it, and serve a ranked exportable list.

## The pipeline

```
                          [ cron / CLI ]
                                |
                                v
  +---------------------------------------------------------------+
  |  1. IMPORT          registry importers (one per source)       |
  |                     CBSE SARAS, CISCE, IB, Cambridge,         |
  |                     UDISE+, group campus directories          |
  +---------------------------------------------------------------+
                                | enqueue fetch jobs
                                v
  +---------------------------------------------------------------+
  |  2. FETCH           httpx + asyncio, per-domain semaphore     |
  |                     robots check, PII denylist, retry/backoff |
  |                     -> fetches + raw_documents (immutable)    |
  +---------------------------------------------------------------+
                                |
                +---------------+---------------+
                v                               v
  +--------------------------+     +--------------------------------+
  |  3a. PARSE  (tier 1)     |     |  3b. PATTERNS  (tier 2)        |
  |  label + table parsers   |     |  regex: emails, phones,        |
  |  selectolax (HTML)       |     |  Indian numerals, fee lines,   |
  |  pdfplumber (PDF)        |     |  stream keywords               |
  +--------------------------+     +--------------------------------+
                |                               |
                |     +--------------------------------------+
                +---> |  3c. LLM FALLBACK  (tier 3)          |
                      |  OFF BY DEFAULT.                     |
                      |  EXTRACTION_LLM_ENABLED=false        |
                      |  Only when 1+2 found nothing for a   |
                      |  decision-critical field.            |
                      +--------------------------------------+
                |                               |
                +---------------+---------------+
                                v
  +---------------------------------------------------------------+
  |  4. OBSERVATIONS    append-only claims, one row per           |
  |                     (entity, field, value, source, evidence)  |
  +---------------------------------------------------------------+
                                |
                                v
  +---------------------------------------------------------------+
  |  5. RESOLVE         normalise -> link on stable IDs ->         |
  |     (deterministic) block+score fuzzy -> conflict policy ->    |
  |                     apply human overrides                     |
  |                     mid-band matches -> review_queue          |
  +---------------------------------------------------------------+
                                |
                                v
  +---------------------------------------------------------------+
  |  6. CANONICAL       institutions, institution_boards, groups,  |
  |     (derived)       people, roles  -- fully rebuildable        |
  +---------------------------------------------------------------+
                                |
                +---------------+---------------+
                v                               v
  +--------------------------+     +--------------------------------+
  |  7. SCORE                |     |  8. SIGNALS                    |
  |  fit + confidence,       |     |  monthly registry snapshot     |
  |  versioned, explainable  |     |  diff -> deterministic events  |
  +--------------------------+     +--------------------------------+
                |                               |
                +---------------+---------------+
                                v
  +---------------------------------------------------------------+
  |  9. SERVE           FastAPI: /search /institutions/{id}        |
  |                     /institutions/{id}/why  /export            |
  |                     Jinja + HTMX UI, CSV/XLSX export          |
  +---------------------------------------------------------------+
```

## Stage contracts

Each stage has one job, one input shape, one output shape. A stage must be
independently runnable and idempotent.

| Stage | In | Out | Trigger | Idempotency key |
|---|---|---|---|---|
| 1 Import | source config | fetch jobs | cron per `source_registry.refresh_days` | `jobs.dedupe_key` = `source_id:url:yyyymmdd` |
| 2 Fetch | fetch job | `fetches` row + `raw_documents` row | worker picks up job | `content_hash` (content-addressed; unchanged page stores nothing new) |
| 3a Parse (tier 1) | `raw_documents` row | `observations` rows | new/changed `content_hash` | `(content_hash, extractor, field, entity_key)` |
| 3b Patterns (tier 2) | `raw_documents` row | `observations` rows | fields tier 1 missed | same as 3a |
| 3c LLM (tier 3, **off**) | `raw_documents` row | `observations` rows | only if enabled **and** tiers 1-2 found nothing for a decision-critical field | same as 3a |
| 4 ,  | ,  | ,  | ,  | append-only, never updated in place |
| 5 Resolve | all observations + overrides | canonical rows | after extraction, or on demand | full recompute is the normal mode |
| 6 ,  | ,  | ,  | ,  | truncate-and-rebuild is safe and expected |
| 6b Group rollup | canonical | `groups.qualifying_campus_count` | after resolve, **before** score | full recompute |
| 7 Score | canonical + `scoring.yaml` | `scores` rows | after 6b, or on config change | `(institution_id, model_version, campaign, computed_at)` |
| 8 Signals | two `registry_snapshots` | `signals` rows | monthly | `(institution_id, signal_type, event_date)` |
| 9 Serve | canonical + scores | HTTP | request | read-only; **never triggers a fetch** |

Stage 6b exists to break a real circular dependency: scoring's group-leverage
component reads `qualifying_campus_count`, which must therefore be computed from
the hard gate and the affordability floor **only** ,  never from a Fit score. See
`docs/DATA-MODEL.md`, "Five things that are easy to get wrong", item 2. If
anything in `resolve/` ever imports from `score/fit.py`, the cycle is back.

### The two rules that make everything else work

**Rule 1 ,  a user request never triggers a fetch or an LLM call.** Reads hit
Postgres only. This is the one architectural principle `Project-Doc.md` got
exactly right and it must not be eroded for a "just-in-time enrichment" feature.

**Rule 2 ,  the canonical layer is disposable.** `make rebuild` drops and
regenerates stages 5-7 from stages 1-4 plus `overrides`. If any code change makes
a rebuild lossy, that change is wrong. This is what lets you improve extraction
prompts, models, source priorities, and scoring formulas without re-crawling and
without losing history.

## Repository layout

Create this structure. Do not invent additional top-level packages.

```
school_intel/
  __init__.py
  config.py               # pydantic-settings; env + YAML
  db.py                   # engine, session, migrations entrypoint

  sources/                # STAGE 1 - one module per source
    __init__.py           # SOURCE_REGISTRY dict, discovery
    base.py               # Importer protocol
    cbse_saras.py         # affiliated list + detail pages + closed list
    cbse_mpd.py           # Mandatory Public Disclosure on school sites
    cisce.py
    ib.py
    cambridge.py
    udise.py
    group_directory.py    # Narayana / Deeksha / BASE / Sri Chaitanya ...
    search_fill.py        # Serper - fill missing website URLs ONLY

  fetch/                  # STAGE 2
    client.py             # httpx client, retry, per-domain semaphore
    robots.py             # robots.txt cache + check
    denylist.py           # ADR-006 PII URL denylist - HARD GATE
    store.py              # content-addressed raw_documents

  extract/                # STAGE 3
    parsers/              # 3a deterministic
      cbse_saras.py
      group_directory.py
    llm/                  # 3b
      client.py           # Anthropic client, Batch API submit/poll
      schemas.py          # pydantic models = the structured output schemas
      prompts/            # versioned .md prompt files, e.g. mpd_v2.md
    normalize.py          # names, cities, states, titles, currency, PIN
    normalize_data/       # abbreviations / city_renames / titles YAML

  resolve/                # STAGE 5
    link.py               # stable-ID linkage
    blocking.py           # candidate generation
    match.py              # scoring + thresholds
    conflict.py           # per-field source priority (docs/SOURCES.md)
    groups.py             # ADR-005 / ADR-008 chain resolution
    build.py              # orchestrates a full canonical rebuild

  score/                  # STAGE 7
    fit.py
    confidence.py
    flags.py
    scoring.yaml          # ALL weights and bands live here, not in code

  signals/                # STAGE 8
    snapshot.py
    diff.py

  api/                    # STAGE 9
    app.py
    routes_search.py
    routes_institution.py
    routes_why.py         # provenance endpoint - see below
    routes_export.py
    nlq.py                # NL -> validated filter JSON -> SQL
    templates/            # Jinja + HTMX
    static/

  jobs/
    queue.py              # Postgres FOR UPDATE SKIP LOCKED
    worker.py             # single worker loop
    schedule.py           # cron definitions

  cli.py                  # typer: import, fetch, extract, resolve, score, eval

migrations/               # alembic
tests/
  unit/
  integration/
  eval/                   # ADR-015 gold-set harness
docs/
```

## "Why does the system believe this?"

This is a stated requirement and it is a first-class endpoint, not a nice-to-have.

`GET /institutions/{id}/why?field=fee_annual_inr` returns:

```json
{
  "field": "fee_annual_inr",
  "value": {"min": 185000, "max": 210000, "year": 2025},
  "winning_observation": {
    "observation_id": 88412,
    "source_id": "cbse_mpd",
    "authority_tier": 3,
    "evidence_span": "Class XI-XII (Science): Tuition Fee Rs 1,85,000 p.a.; Development Fee Rs 25,000 p.a.",
    "url": "https://example-school.edu.in/mandatory-disclosure",
    "fetched_at": "2026-07-14T09:22:11Z",
    "raw_document": "/raw/9f2a...c1/index.html",
    "extractor": "llm:haiku-4.5/mpd_v2",
    "confidence": 0.91
  },
  "conflicting_observations": [
    {"value": 150000, "source_id": "third_party_directory",
     "authority_tier": 4, "observed_at": "2024-03-02",
     "lost_because": "lower authority_tier"}
  ],
  "policy_applied": "fee_annual_inr: cbse_mpd only (docs/SOURCES.md)"
}
```

Three properties make this possible and they are all load-bearing:
`observations` is append-only so losers are never deleted; `evidence_span` is
mandatory on every LLM extraction; `raw_documents` keeps the original bytes so
the span can be located in context.

## Rejected components and their promotion triggers

Do not add any of these speculatively. Each may be introduced **only** when its
trigger is met and measured. If you add one, record the measurement in
`docs/DECISIONS.md`.

| Component | Promotion trigger |
|---|---|
| Playwright / headless browser | Static-fetch extraction failure rate on Tier A+B institution sites exceeds 15%, measured over >=500 sites |
| OpenSearch / Elasticsearch | p95 search latency > 500ms on the real corpus, after `pg_trgm` + GIN indexes and an `EXPLAIN ANALYZE` showing Postgres is the bottleneck |
| Celery + Redis | More than one worker machine is genuinely needed, i.e. a full refresh cannot finish inside its cron window on one box |
| Embedding-based entity resolution | Manual review queue exceeds 200 open items/month *and* audit shows deterministic blocking is missing true matches (recall problem, not precision) |
| Separate frontend app (React/Next) | BD asks for interactive functionality Jinja + HTMX genuinely cannot express |
| News / signal pipeline | Explicit stakeholder go-ahead (currently demoted, ADR-002) |
| Precise street-level geocoding | A feature requires sub-PIN distance accuracy; PIN centroid demonstrably insufficient |

## Failure handling

**Fetch failures.** `jobs.attempts` with exponential backoff, `max_attempts` 3,
then `state='dead'`. A dead job is a data-quality signal, not a silent gap: the
institution gets a `fetch_failed` flag and its confidence drops. Never let a
failed fetch quietly leave a field null-and-unexplained.

**Extraction failures.** A malformed or schema-invalid LLM response is retried
once, then written to `review_queue` with `kind='low_confidence_extraction'`.
Never write a guessed value. An absent observation is always better than a wrong
one ,  a wrong answer is worse than no answer.

**Resolve ambiguity.** Match score in the mid-band goes to `review_queue` with
`kind='merge_candidate'`. Never auto-merge above the ambiguity threshold
(`Project-Doc.md` 6.10, retained).

**Source structure drift.** Every deterministic parser asserts an expected shape
(minimum row count, required column headers). A parser that suddenly yields 0
rows or fewer than 50% of the previous snapshot's rows must **fail loudly and
abort**, not write an empty snapshot ,  otherwise stage 8 will emit thousands of
bogus `disaffiliation` signals. This is the single most dangerous failure mode in
the system.

**Partial runs.** Every stage is resumable because every stage is idempotent on
its key. Killing a worker mid-run is safe.

## Observability

Minimum viable, in Postgres ,  no external stack needed at this scale:

- `fetches` gives request volume, status distribution, latency per source.
- A `pipeline_runs` table records stage, started/finished, rows in/out, and
  errors. Every stage writes one row.
- Assertions after each run: institution count changed by < 20% since last run,
  or abort and alert. Same guard as parser drift, at aggregate level.
- Eval harness output (`tests/eval/`) is checked in per run so extraction quality
  is a tracked time series, not a vibe.

## Runtime envelope

Per ADR-013, the prototype has **no per-document inference cost and no external
API dependency**. Every stage runs locally against Postgres and object storage.

| Item | v1 (metros, ~6k institutions) | Full national (~40k) |
|---|---|---|
| Registry imports | free | free |
| Institution site fetches | ~18k requests | ~120k requests |
| Extraction (tiers 1-2, deterministic) | free, ~ms/document | free, ~ms/document |
| Extraction tier 3 (LLM) | **disabled** | **disabled** |
| Website gap-fill (Serper) | optional, disabled by default | optional |
| Hosting | 1 small VM + Postgres + object storage | same |

The binding constraint is therefore **wall-clock on fetching**, not inference.
At 1 request/second/domain across many domains concurrently, a full enrichment
pass is hours, not days ,  and a re-extraction pass over stored documents is
**minutes**, because it makes no network calls at all.

This is the real payoff of ADR-009: tuning a parser is a minutes-long loop.
Infrastructure is not where this project's risk lives. Data correctness is, which
is why ADR-015's eval harness is now the most important thing in M2.
