# Implementation Plan

Dependency-ordered. Do the milestones in order; within a milestone, tasks marked
**[parallel]** can be done concurrently.

Read `docs/DECISIONS.md`, `docs/DATA-MODEL.md`, and `AGENTS.md` before starting
any task. Every task below assumes those are known.

**Global definition of done**, applied to every task in addition to its own:
`ruff check` and `ruff format` clean, type hints on public functions, tests
passing, no new dependency added without justification in the PR description, no
`ponytail:` shortcut left unlabelled.

---

## M0 — Foundation

Goal: a runnable skeleton with a database and a job queue. No data yet.

### M0-0 Reconnaissance spike — **do this before writing any code**
- **Goal.** Prove the two assumptions the whole architecture rests on, and
  produce the fixtures M1 needs anyway.
- **Why.** ADR-001 asserts discovery is a registry-import problem. Nothing in the
  plan verifies the registry is actually importable. If SARAS disallows in
  `robots.txt` or hard rate-limits, the backbone is dead — and the current
  sequence would surface that three milestones in. Same for fee coverage: fees
  come from S2 only, affordability is 30 of 100 Fit points, and M2-1's own DoD
  already expects a 50-75% discovery rate. If real fee coverage is 30%, the
  ranking is curriculum + geography, which does not need a pipeline.
- **Depends on.** Nothing. Half a day, by hand, no package required.
- **Deliverables.**
  1. `saras.cbse.gov.in/robots.txt` saved and read. **Go/no-go on the backbone.**
  2. One state list page (Karnataka) + 5 detail pages saved to
     `tests/fixtures/cbse_saras/`. These are the M1-1 and M1-3 fixtures.
  3. Confirm the list endpoint's stated total matches its row count, and whether
     it paginates or caps. Record the answer in `docs/SOURCES.md` S1.
  4. **Fee-coverage probe.** Take 30 Bengaluru schools from that list, visit each
     site by hand, record in a scratch CSV: MPD page found (y/n), format
     (HTML/PDF), fee table present and parseable (y/n), class-wise strength
     present (y/n). Save 12 of these pages to `tests/fixtures/mpd/` — M2-2
     requires at least 12 real fixtures and this is where they come from.
- **DoD.** Written into `docs/SOURCES.md`: the robots position, the pagination
  answer, and the observed fee-coverage rate over 30 schools. If fee coverage is
  under ~35%, stop and re-open ADR-011 before building M2 — that is a business
  conversation, not a parser problem.

### M0-1 Project scaffolding
- **Goal.** `school_intel/` package per the layout in `docs/ARCHITECTURE.md`.
- **Why.** Every later task references these paths; inventing a different layout
  costs more than it saves.
- **Depends on.** Nothing.
- **Files.** `pyproject.toml`, `school_intel/__init__.py`, `school_intel/config.py`,
  `school_intel/cli.py`, `Makefile`, `.env.example`.
- **Behaviour.** `pyproject.toml` gains: `sqlalchemy`, `alembic`, `psycopg[binary]`,
  `pydantic-settings`, `httpx`, `selectolax`, `pdfplumber`, `typer`,
  `jinja2`, `python-multipart`, `openpyxl`, `rapidfuzz`, `pyyaml`.
  Dev: `pytest`, `pytest-asyncio`, `ruff`, `respx`.
  **`anthropic` goes in an optional extra** (`[project.optional-dependencies] llm`),
  not the base install — the pipeline must run without it. ADR-013.
  Existing `fastapi` stays. Python 3.13 (`.python-version` already set). Manage
  with `uv`.
- **Interfaces.** `config.Settings` (pydantic-settings) reads `DATABASE_URL`,
  `RAW_STORE_PATH`, `USER_AGENT`, `LOG_LEVEL`,
  `EXTRACTION_LLM_ENABLED` (**default `false`**), and optionally
  `ANTHROPIC_API_KEY` / `SERPER_API_KEY`. **A missing `ANTHROPIC_API_KEY` must not
  raise** while `EXTRACTION_LLM_ENABLED` is false. `cli.py` is a `typer.Typer()` with stub subcommands: `import`,
  `fetch`, `extract`, `resolve`, `score`, `signals`, `eval`, `serve`, `worker`.
- **Edge cases.** Never read `os.environ` outside `config.py`.
- **Tests.** `test_config.py`: settings load from env; a missing required var
  raises at import, not at first use.
- **DoD.** `python -m school_intel.cli --help` lists every subcommand. `make dev`
  installs and runs lint plus tests.
- **Delete `main.py`.** It is the uv placeholder and has no purpose here.

### M0-2 Database and migrations
- **Goal.** Full schema from `docs/DATA-MODEL.md` in Alembic.
- **Why.** Everything downstream writes to it. Getting it right once is cheaper
  than eight migrations.
- **Depends on.** M0-1.
- **Files.** `school_intel/db.py`, `school_intel/models.py`, `migrations/`.
- **Behaviour.** SQLAlchemy 2.0 declarative models mirroring the DDL exactly —
  same table names, same column names, same constraints, same partial indexes.
  Enable `pg_trgm` in the first migration.
- **Interfaces.** `db.engine`, `db.session_scope()` contextmanager,
  `db.upgrade_head()`.
- **Edge cases.** `observations.institution_id` FK is declared **after**
  `institutions` exists — use two migrations or a deferred constraint. The partial
  unique index on `observations` (`WHERE superseded_by IS NULL`) must be written
  as raw SQL; Alembic autogenerate will miss it.
- **Tests.** `test_migrations.py`: upgrade to head then downgrade to base on a
  scratch database, no errors. Assert every table in `docs/DATA-MODEL.md` exists.
- **DoD.** `alembic upgrade head` on an empty Postgres 16 produces every table and
  index in the spec.

### M0-3 Job queue and worker
- **Goal.** Postgres-backed queue, per ADR-012. No Redis, no Celery.
- **Why.** Durable, inspectable in SQL, one fewer service.
- **Depends on.** M0-2.
- **Files.** `school_intel/jobs/queue.py`, `school_intel/jobs/worker.py`.
- **Behaviour.** `enqueue(kind, payload, dedupe_key=None, not_before=None)`
  inserts, silently no-ops on `dedupe_key` conflict. `claim(worker_id)` runs the
  `FOR UPDATE SKIP LOCKED` statement from `docs/DATA-MODEL.md`. `complete(id)`,
  `fail(id, error)` — the latter re-queues with backoff `2s, 8s, 30s` until
  `attempts >= max_attempts`, then `state='dead'`.
- **Interfaces.** `HANDLERS: dict[str, Callable[[dict], Awaitable[None]]]`,
  registered by each stage module. `worker.run(concurrency: int = 8)`.
- **Edge cases.** A crashed worker leaves rows `running` — a startup sweep resets
  `state='running' AND locked_at < now() - interval '30 minutes'` back to
  `pending`. Handler exceptions must never kill the worker loop.
- **Tests.** `test_queue.py`: two concurrent claimers never get the same job;
  dedupe_key prevents duplicates; a failing job retries then dies; the stale-lock
  sweep works.
- **DoD.** `python -m school_intel.cli worker` processes an enqueued no-op job and
  exits cleanly on SIGTERM.

### M0-4 Source registry seed **[parallel]**
- **Goal.** Seed `source_registry` from `docs/SOURCES.md`.
- **Depends on.** M0-2.
- **Files.** `school_intel/sources/__init__.py`, `school_intel/sources/seed.yaml`.
- **Behaviour.** Idempotent upsert on startup. One row per S1-S9, with
  `authority_tier`, `refresh_days`, `rate_limit_rps` exactly as documented.
  `*.gov.in` sources get `rate_limit_rps = 0.5`.
- **Tests.** `test_source_registry.py`: seed is idempotent; every source used
  anywhere in code exists in the registry.
- **DoD.** All nine sources present with the documented tiers.

---

## M1 — Registry backbone

Goal: national CBSE coverage, resolved into canonical institutions, with a proven
rebuild. **This milestone alone delivers real value** — every senior-secondary
CBSE school in India with district, principal, address and website.

**Import is national; enrichment is campaign-scoped (ADR-017).** ~9,600
senior-secondary schools out of 33,147 affiliated `[VERIFIED M0-0]`.

### M1-1 CBSE SARAS importer
- **Goal.** Import the affiliated list and closed list; enqueue detail fetches.
- **Why.** ADR-001. This is the backbone of the entire system.
- **Depends on.** M0-0, M0-3, M0-4.
- **Files.** `school_intel/sources/cbse_saras.py`,
  `school_intel/extract/parsers/cbse_saras.py`.
- **Scope: import national, enrich by campaign (ADR-017).** `cli import
  cbse_saras` runs **all states** by default — it is ~38 POSTs and the list
  already carries principal, address and website, so a national directory is
  nearly free and lets BD ask about any metro from day one.
  `--state KA` narrows it for development and testing.
  **The expensive stage is enrichment, not import.** M2-1 reads
  `enrichment_priority` (config, not code) and enqueues MPD work for
  **Karnataka, Tamil Nadu, Maharashtra, Telangana** first, then tier-1 metros.
  Do not conflate the two scopes; they differ by two orders of magnitude in cost.
- **Behaviour.**
  1. Fetch the list endpoint per state (the national table is large; per-state
     keeps responses manageable and failures granular).
  2. Parse the HTML table with `selectolax`. Columns per `docs/SOURCES.md` S1.
  3. **Filter to `School Status = 'Senior Secondary Level'`** before enqueuing
     anything. This is the hard gate and the largest cost saving in the pipeline.
  4. Write a `registry_snapshots` row.
  5. Enqueue one `fetch` job per surviving affiliation number for
     `/SARAS/AffiliatedList/AfflicationDetails/{n}`.
  6. Import the closed/disaffiliated category too; those set `status`.
- **Interfaces.** `Importer` protocol in `sources/base.py`:
  `async def run(self) -> ImportResult` where
  `ImportResult = {snapshot_id, rows_seen, jobs_enqueued}`.
- **Edge cases.** Pagination or a size cap on the list endpoint — verify the row
  count matches the page's own stated total and **fail loudly** if not.
  **Structure-drift guard (mandatory):** if parsed rows are fewer than 50% of the
  previous snapshot's `row_count`, abort the run without writing a snapshot. Skipping
  this guard will make M4-3 emit thousands of false `disaffiliation` signals.
- **Tests.** `test_cbse_saras_parser.py` against 3 checked-in HTML fixtures
  (a normal state page, an empty result, a malformed page). Assert the level filter
  works and the drift guard fires.
- **DoD.** A dry run against one state produces the expected count of senior-
  secondary schools and enqueues matching detail jobs.

### M1-2 Fetch layer
- **Goal.** The single HTTP path used by every source.
- **Depends on.** M0-3.
- **Files.** `school_intel/fetch/client.py`, `robots.py`, `denylist.py`, `store.py`.
- **Behaviour.** `httpx.AsyncClient`, HTTP/2, follow redirects (max 5), 30s
  timeout. Per registrable-domain `asyncio.Semaphore(1)` plus a token bucket at
  `source_registry.rate_limit_rps`. Check `denylist.py` **before** the request.
  Check `robots.txt` (24h cache) before the first request to a new host. On
  success, sha256 the body, write `raw_documents` if the hash is new, always write
  a `fetches` row.
- **Interfaces.** `async def fetch(url: str, source_id: str) -> FetchResult`
  where `FetchResult = {fetch_id, content_hash | None, status, blocked_reason | None}`.
- **Edge cases.** Denylist hit -> `fetches` row with `denylist_hit` set, no request
  made. Robots disallow -> `robots_allowed=false`, no request. `Retry-After`
  honoured. Non-HTML content types recorded accurately; PDFs stored as-is.
- **Tests.** `test_fetch.py` with `respx`. **`test_denylist.py` is mandatory and
  must assert both directions** — result/merit/topper URLs blocked,
  `fee-structure` / `mandatory-disclosure` / `about-us` allowed. See
  `docs/COMPLIANCE.md`.
- **DoD.** Fetching the same URL twice writes two `fetches` rows and one
  `raw_documents` row.

### M1-3 CBSE detail page parser
- **Goal.** Turn detail HTML into observations. Deterministic — no LLM.
- **Depends on.** M1-1, M1-2.
- **Files.** `school_intel/extract/parsers/cbse_saras.py` (extend).
- **Behaviour.** Label-driven extraction, not positional: find the cell whose text
  matches the label and read its sibling. One `observations` row per field in the
  S1 mapping table, `extractor='parser:cbse_saras_detail_v1'`, `confidence=1.0`,
  `entity_key='cbse:{affiliation_no}'`, `evidence_span` = the matched row text.
- **Edge cases.** Missing labels are normal — emit no observation rather than an
  empty one. `School Status` maps to `has_class_12` and `grade_high`.
  `Affiliation Period` splits into two dates; formats vary (`01/04/2022 To
  31/03/2027`). Trust/Society name may be blank. **Do not** store principal
  qualifications (`docs/COMPLIANCE.md` purpose limitation).
- **Tests.** Golden-file test against the verified live sample
  (affiliation 330801) asserting every documented field, plus a fixture with half
  the labels absent.
- **DoD.** 100 real detail pages produce observations with zero exceptions and a
  logged per-field fill rate.

### M1-4 Normalisation
- **Goal.** Pure functions per `docs/ENTITY-RESOLUTION.md` stage 1.
- **Depends on.** M0-1.  **[parallel with M1-1..3]**
- **Files.** `school_intel/extract/normalize.py`,
  `school_intel/extract/normalize_data/abbreviations.yaml`,
  `normalize_data/city_renames.yaml`, `normalize_data/titles.yaml`.
  **Note the directory name.** An earlier draft of this plan said
  `normalize/abbreviations.yaml` alongside `normalize.py` — a module and a
  package cannot share a name. The data directory is `normalize_data/`.
- **Interfaces.** `match_key(name) -> str`, `normalize_city(city, state) -> str`,
  `normalize_person(name) -> str`, `normalize_title(raw) -> str`,
  `parse_inr(text) -> int | None`, `normalize_domain(url) -> str | None`,
  `normalize_phone(text) -> list[str]`, `normalize_legal_entity(name) -> str`.
- **Edge cases.** `parse_inr` returns `None` on an ambiguous period — never
  guesses monthly vs annual. A 12x error moves an institution across four scoring
  bands, so silence is mandatory here.
- **Tests.** `test_normalize.py`, table-driven, including every case listed in
  `docs/ENTITY-RESOLUTION.md` "Testing requirements".
- **DoD.** All listed cases pass. No I/O, no database, no network in this module.

### M1-5 Resolver v1
- **Goal.** Observations -> canonical `institutions` + `institution_boards`,
  stable-ID linkage only.
- **Depends on.** M1-3, M1-4.
- **Files.** `school_intel/resolve/build.py`, `link.py`, `conflict.py`.
- **Behaviour.** Full recompute is the normal mode. Group observations by
  `entity_key`; link by stable ID; apply the per-field priority table from
  `docs/SOURCES.md`; apply `merge_decisions`; apply `overrides` **last**; write
  canonical rows and set `observations.institution_id`.
- **Interfaces.** `def rebuild(run_at: datetime) -> RebuildResult`. `run_at` is
  **passed in**, never `now()` inside — determinism depends on it.
- **Edge cases.** Ties within a source -> newer `observed_at`. Numeric
  disagreement > 20% between sources -> keep both, set the `*_disputed` flag,
  raise a `review_queue` conflict (`Project-Doc.md` 6.7). Losing observations are
  never deleted.
- **Tests.** `test_conflict.py`: priority order respected per field; override
  beats every source; >20% disagreement raises a conflict.
- **DoD.** `cli resolve` produces populated `institutions` with `has_class_12` set
  and `last_resolved_at` stamped.

### M1-6 Rebuild guarantee — **do not skip**
- **Goal.** Prove the four properties in `docs/DATA-MODEL.md` "Rebuild contract".
- **Why.** ADR-009. If rebuild is not trustworthy, no later improvement is safe.
  This is the load-bearing test of the whole architecture.
- **Depends on.** M1-5.
- **Files.** `Makefile` (`make rebuild`),
  `tests/integration/test_rebuild.py`.
- **Behaviour.** `make rebuild` clears the purely derived tables, upserts
  institutions and groups on their stable-ID columns, replays, then prunes.
  Never touches `observations`, `fetches`, `raw_documents`, `signals`,
  `review_queue`, `overrides`, `merge_decisions`, `eval_gold`.
  **Not `TRUNCATE ... CASCADE`** — see `docs/DATA-MODEL.md` "Why this is not
  TRUNCATE ... CASCADE". `[VERIFIED M1-5]` That statement deletes the
  observation layer, because CASCADE ignores `ON DELETE` rules and truncates
  every referencing table.
- **Tests.** Four assertions: (a) two consecutive rebuilds from identical inputs
  produce identical canonical rows; (b) every `overrides` row is reflected after
  rebuild; (c) every `merge_decisions` row is honoured; (d) raw and observation
  row counts are unchanged by a rebuild.
- **DoD.** All four pass in CI.

### M1-7 PIN centroids and metro mapping **[parallel]**
- **Goal.** Load S9 once. Populate `lat`/`lon` with `geo_precision='pin_centroid'`
  and build the `pin -> metro_area` table.
- **Why it lives here and not in M3.** It was M3-7. It is a one-off CSV load with
  no importer, no fetch loop and no unverified shape, and it feeds a 15-point
  scoring component plus the district/state cross-check on S1 addresses. Leaving
  a trivial blocking task inside the parallel coverage milestone meant scoring
  could not run until six unrelated importers existed.
- **Depends on.** M0-2. **[parallel with M1-1..M1-6]**
- **Files.** `school_intel/sources/pin_centroids.py`,
  `school_intel/score/metro_areas.yaml`.
- **Behaviour.** Per `docs/SOURCES.md` S9. `pin -> metro_area` is an **editable
  config table, not code** — NCR spans three states (`Project-Doc.md` 6.2) and
  the mapping will need hand edits.
- **Tests.** `test_pin_centroids.py`: a known Bengaluru PIN resolves to
  `Bengaluru Urban`; an NCR PIN in Haryana and one in Delhi both map to the same
  `metro_area`; an unknown PIN yields no coordinates rather than a guess.
- **DoD.** Every institution with a `pincode` has `lat`/`lon` and, where
  applicable, a `metro_area`.

---

## M2 — Enrichment

Goal: fee data, PCM counts, and contacts from institution websites. This is what
turns a directory into an intelligence system.

### M2-1 MPD discovery
- **Goal.** Find each institution's Mandatory Public Disclosure page.
- **Scope.** Driven by `enrichment_priority` (ADR-017): KA, TN, MH, TG first,
  then tier-1 metros. Never the whole national corpus in one pass.
- **Depends on.** M1-5.
- **Files.** `school_intel/sources/cbse_mpd.py`.
- **Behaviour.** **Homepage first, guessed paths second** — M0-0 measured this
  and the order matters: schools link the MPD page from their nav under a dozen
  different slugs (`/mandatory-discloser`, `/cbse-mandatory-disclosure`,
  `/mandatory_public_disclosure.php`, `/disclosure/b1.pdf`), so harvesting
  anchors off the homepage beats guessing paths. Match an anchor on **either its
  href or its tag-stripped link text** against
  `/mandator|disclosur|appendix.?ix|public.?disclos/i` — matching only the text
  between `>` and `<` misses every site that wraps its nav labels in a `<span>`.
  Then fall back to the candidate paths in `docs/SOURCES.md` S2.
  **Cap at 6 fetches per institution**, and note the cap must cover three hops,
  not two — see the next bullet. Accept a page containing at least two of:
  `fee`, `student strength`, `affiliation`, `trust`.
- **The fee is one hop deeper. `[VERIFIED M0-0]`** Accepting the MPD index page is
  not the end of discovery. The Appendix-IX `FEE STRUCTURE OF THE SCHOOL` row
  links out to a PDF; the page itself carries no fee figure. Resolve that link
  (by row label, not anchor text — see `docs/SOURCES.md` S2) and enqueue it.
  A discovery step that stops at the index measures 0% fee coverage and looks
  like a parser bug when it is not.
- **Edge cases.** Many schools publish a PDF — accept `application/pdf`. Some
  redirect to a parent-portal login: detect and abandon rather than looping. No
  website -> enqueue an S8 gap-fill job (M3-5). Wrong-school pages (shared
  hosting) -> require a fuzzy name match against `canonical_name` before
  accepting.
- **Tests.** `test_mpd_discovery.py` with `respx`: found via homepage anchor
  (href match **and** nested-`<span>` text match as separate cases); found at a
  guessed path; MPD served as a PDF; login wall; not found; **and the three-hop
  case — homepage -> index -> fee PDF.** Assert the 6-fetch cap holds in all
  branches, including the three-hop one.
- **DoD.** Run over 200 real institutions; log the hit rate **at each hop
  separately** — homepage reachable, MPD index found, fee document reached. One
  blended number hides which hop is failing, and they fail for different reasons
  (dead domains vs. slug variety vs. missing uploads). Compare against the M0-0
  baseline in `docs/SOURCES.md` S2 and treat a large gap as a bug in this task.

### M2-2 Deterministic extraction — tiers 1 and 2
- **Goal.** MPD pages (HTML and PDF) -> observations, with **no API calls and no
  model inference**. ADR-013.
- **Why.** Appendix-IX is a standardised statutory form with numbered rows and
  fixed labels, not prose. Label-and-table parsing is faster, free, and more
  reproducible than inference on exactly this shape of input. It also makes the
  tune-and-rerun loop milliseconds instead of hours, which is what actually
  matters during a prototype.
- **Depends on.** M2-1.
- **Files.** `school_intel/extract/mpd/tables.py` (tier 1),
  `patterns.py` (tier 2), `schemas.py`, `labels.yaml`.
- **Behaviour.**

  **Tier 1 — label and table parser.** `selectolax` for HTML, `pdfplumber` for
  PDF (good at tables, pure Python, no system dependency). Strip nav, footer,
  script, style first. Then, for each target field, find the cell whose normalised
  text matches a known label and read the adjacent or below cell. Labels live in
  `labels.yaml` as a list of synonyms per field, **never hardcoded** — the
  proforma wording varies between schools and the list will grow. Example:

  ```yaml
  fee_annual_inr:
    - "fee structure"
    - "fees structure"
    - "annual fee"
    - "fee details"
    - "school fee"
  # M0-0: on the compliant proforma this label's row holds a LINK, not a value.
  # Tier 1 takes the href from the matched row; the figure is in the linked PDF.
  fee_structure_link:
    - "fee structure of the school"
    - "fee structure"
  class_wise_strength:
    - "no. of students"
    - "number of students"
    - "student strength"
    - "class wise enrolment"
    - "section wise enrolment"
  ```

  **Tier 2 — pattern extractors,** for fields tier 1 missed because the page is
  free-layout rather than tabular:
  - `email` — RFC-ish regex, reject anything on a known CDN or webmaster domain
  - `phone` — Indian formats: `+91`, `0XX`, 10-digit, with separators
  - `fee_annual_inr` — `parse_inr` from M1-4 applied to numbers near a fee keyword.
    **`[VERIFIED M0-0]` Do not require a `Rs`/`₹`/`/-` marker** — real fee tables
    carry the currency in a column header and the figures bare. Prefer the
    class-12 PCM/science row over a school-wide figure; see `docs/SOURCES.md` S2.
  - `streams` — keyword presence: Science, Commerce, Arts, PCM, PCB, PCMB
  - `principal_name` — text following a principal label, honorifics stripped

- **Interfaces.** `schemas.MPDExtraction` — every field `Optional`, plus a
  required `evidence: dict[str, str]` mapping field name to the verbatim matched
  source text, plus `student_data_detected: bool`. **Same schema every tier
  writes to**, so tiers are interchangeable and comparable.
- **Edge cases.**
  - **`evidence` is still mandatory.** A parser records the matched row text
    exactly as a model would record its span. `/institutions/{id}/why` does not
    care which tier produced a value, and must not.
  - **The student-data check still runs**, now as a deterministic scan for
    result-table shapes (a column header matching `roll|marks|percentage|rank`
    alongside a name column). On a hit: delete the raw document, add the URL to
    `denylist_learned`, emit no observations. `docs/COMPLIANCE.md`.
  - No match is a **normal outcome**, not an error. Emit no observation and let the
    field stay unknown. Never fall back to "the first number on the page" — that is
    a guessed value, which hard rule 6 forbids.
  - Fee ranges follow `docs/ENTITY-RESOLUTION.md`: min = tuition, max = all
    recurring annual mandatory components; one-off and refundable items excluded.
  - If only a class-12 total is published with no stream split, record
    `class_12_total` and **not** `pcm_12_count`. Estimation is scoring's job, with
    a visible flag.
  - `counsellor_name` is **not in the Appendix-IX proforma** and will be sparse.
    Accept that; do not build a speculative staff-page crawler for it.
- **`[VERIFIED M0-0]` ~30% of fee PDFs are scanned images** with no text layer.
  `pdfplumber` returns 0-1 characters and there is nothing for any tier to parse
  — tier 3 does not rescue this either, since a text LLM also needs text. Detect
  the case explicitly (`extract_text()` under ~50 chars on a PDF whose filename
  or source row says fee) and route it to `review_queue` as
  `unreadable_fee_document` rather than letting it look like a missing page.
  Whether to add OCR is a separate decision with its own ADR — do not add
  Tesseract inside this task.
- **Tests.** `test_mpd_tables.py` and `test_mpd_patterns.py` against **at least 12
  checked-in real fixtures** spanning tabular HTML, free-text HTML, PDF, a
  **scanned PDF** (asserting it routes to review rather than emitting a value),
  a **grade-and-stream fee table** (asserting the class-12 PCM row wins), a
  **nav-shell page whose only numbers are a PIN code and a phone number**
  (asserting zero observations), and a non-conforming page. Assert: evidence present for every populated field; a
  result-table fixture yields zero observations; a no-match page yields zero
  observations rather than garbage. **No network in tests.**
- **DoD.** 200 real MPD pages parsed; **per-field fill rate logged to
  `pipeline_runs.notes`.** That fill rate is the number that decides whether tier 3
  is ever needed — record it before arguing about it.

### M2-2b OCR for scanned fee documents (ADR-016)
- **Goal.** Read the ~30% of fee PDFs that are scanned images.
- **Why.** `[VERIFIED M0-0]` Without it, fee coverage caps near 30%. This is an
  input-modality gap, not an extraction-quality one — tier 3 does not help,
  because a text LLM also needs text.
- **Depends on.** M2-2.
- **Files.** `school_intel/extract/mpd/ocr.py`.
- **Behaviour.** `rapidocr-onnxruntime` (PyPI only, no system binary, no PyTorch
  — keeps `uv sync` sufficient to run the pipeline). Triggered **only** when a
  PDF reached via a fee-structure link yields under ~50 characters from
  `pdfplumber`. `observations.extractor = 'ocr:rapidocr_v1'`. `evidence_span` is
  the OCR'd line, exactly as every other tier records its span.
- **Three requirements from the validated sample (ADR-016).**
  - **Normalise whitespace out of both sides when matching labels** — OCR emits
    `XITOXII`, not `XI TO XII`.
  - **Take the TOTAL column.** Fees are published as instalments
    (`55500 + 27700 x 3 = 138600`); the first figure is a different band. If no
    total column exists, sum the instalments and put the arithmetic in
    `evidence_span`.
  - **Never source `principal_name` from OCR.** Digits come through exact;
    stylised text does not (`Principal` -> `Pingipa!`).
- **Edge cases — the whole point of this task.** A digit misread moves an
  institution across affordability bands (`74080 -> 7408` is four bands), and
  hard rule 6 says missing beats wrong. So:
  - Set the `fee_ocr` flag; render and export it visibly.
  - Reduced `observations.confidence`; loses to any text-layer or HTML fee in
    `resolve/conflict.py`.
  - Must parse as a clean integer in a plausible range (config bounds). No
    repair heuristics, no "closest plausible value" — emit nothing instead.
  - **Within 10% of an affordability band boundary -> `review_queue` as
    `ocr_fee_unverified`.** That is precisely where one wrong digit changes the
    ranking.
  - OCR never runs on a document the denylist rejected, and never on a
    non-fee document.
- **Tests.** `test_ocr_fee.py` against checked-in scanned fixtures: a clean
  grade/stream table OCRs to the class-12 PCM figure; a garbled read emits
  nothing rather than a repaired number; a value near a band edge routes to
  review; a text-layer PDF never invokes OCR at all.
- **DoD.** `make eval` reports OCR precision **as its own row**, separate from the
  deterministic tiers. ADR-016 is reversible on that number — if OCR precision is
  materially worse, the correct outcome is to turn it off, not to tune it forever.

### M2-3 Extractor versioning and the optional LLM tier
- **Goal.** Extractors are versioned and interchangeable; tier 3 exists but stays
  off.
- **Depends on.** M2-2.
- **Behaviour.** `observations.extractor` records the tier and version:
  `parser:mpd_table_v1`, `regex:mpd_patterns_v1`, `llm:haiku-4.5/mpd_v1`. Running a
  new version over existing `raw_documents` **adds** rows and never mutates old
  ones, so two extractor versions are directly comparable on identical inputs via
  M2-4.

  **Tier 3 is built but disabled.** `EXTRACTION_LLM_ENABLED=false` in config, and
  that is the committed default. When enabled it runs `claude-haiku-4-5` with
  strict structured outputs (`output_config={"format": ...}` from the same
  `MPDExtraction` schema) **only** on documents where tiers 1-2 produced nothing
  for a decision-critical field. Prompts are versioned files in
  `extract/mpd/prompts/`. The prompt must carry the student-data instruction
  verbatim from `docs/COMPLIANCE.md`. A schema-invalid response retries once, then
  goes to `review_queue` as `low_confidence_extraction`.

  Build tier 3 last, and only after M2-4 has measured what tiers 1-2 actually
  achieve. If deterministic fill rates are adequate, leaving it as dead-but-tested
  code is the correct outcome — not a failure.
- **Edge cases.** With the flag off, the `anthropic` import must be lazy so the
  package is not required to run the pipeline. Assert this: the full extract stage
  runs to completion with **no `ANTHROPIC_API_KEY` set**.
- **Tests.** `test_extractor_versioning.py`: two extractor versions coexist over
  one document; the extract stage completes with the LLM flag off and no API key
  present.
- **DoD.** `cli extract --reextract` re-runs against stored raw documents with
  **zero new fetches and zero API calls** — assert both literally, by checking the
  `fetches` row count is unchanged and that no HTTP client was constructed. This is
  the payoff for ADR-009: improving a parser is a minutes-long loop, not a re-crawl.

### M2-4 Evaluation harness — **do not skip**
- **Goal.** Per-field precision and recall against a hand-verified gold set.
- **Why.** ADR-015. Without this, "improve the parser" is unfalsifiable and every
  later change is a guess. **This task got more important under ADR-013**, not
  less: deterministic coverage is now an unknown to be measured, and this harness
  is what decides whether tier 3 ever gets switched on. It is also how you compare
  a parser version against an LLM version over identical stored documents.
- **Depends on.** M2-2.
- **Files.** `tests/eval/`, `school_intel/cli.py` (`eval` command),
  `eval_gold` seed CSV.
- **Behaviour.** Hand-verify ~200 institutions across all tiers and states —
  budget real human hours for this; it is the highest-leverage manual work in the
  project. **`[M2-4]` The harness is built and the seed gold set has 3 rows**,
  hand-read off the fixtures in `tests/fixtures/mpd/`. Three rows measure that
  the harness works, not that extraction works; the ~200 remains outstanding
  human effort and no eval number should be quoted as meaningful until it exists.
- **`[M2-4]` The three numbers are distinct and the harness keeps them apart.**
  `coverage` = of gold, how many did we produce anything for; `precision` = of
  what we produced, how much was right; `recall` = of what gold has, how much we
  got right. An empty gold set reports `None`, never `0.0` — the latter reads as
  "we scored zero" when it means "there is nothing to measure", and it would be
  a lie in our own favour. `cli eval` compares canonical values against `eval_gold` and reports
  per-field precision, recall, and coverage, plus a diff against the previous run.
- **Interfaces.** Report written to `tests/eval/results/{date}-{extractor}.json`
  and **committed**, so extraction quality becomes a tracked time series.
- **Edge cases.** Fee comparison uses a tolerance band (±5%), not equality.
  Missing-in-gold is distinguished from missing-in-system.
- **DoD.** `make eval` prints a per-field table. **A prompt or model change that
  does not report its eval delta is not reviewable** — enforce this in the PR
  template.

---

## M3 — Coverage

All tasks **[parallel]** after M2-2. Each adds a source; none blocks another.

### M3-1 CISCE importer
Per `docs/SOURCES.md` S3. **Confirm the export shape first** — it is
`[UNVERIFIED]`. Critical detail: **check for the ISC row specifically.** ICSE
without ISC ends at class 10 and fails the hard gate. Cross-check against the 2018
`deedy/cisce_schools_data` dataset for sanity only; never import it as canonical.

### M3-2 IB importer
Per S4. ~250 rows — **hand-verify the entire output.** These are the highest-value
leads in the system (ADR-007 Tier A) and an afternoon of human checking beats any
parser. Capture programmes per school; only `IB_DP` clears the class-12 gate.

### M3-3 Cambridge importer
Per S5. Same trap as CISCE: `CAIE_IGCSE` alone stops around class 10; require
A-Level/AS for the gate.

### M3-4 UDISE+ importer
Per S6. **First task is reconnaissance:** capture the real JSON endpoints from
browser devtools on `kys.udiseplus.gov.in` and record them in
`docs/SOURCES.md`. Rate limit 0.5 rps, government infrastructure. Import only
`udise_code`, `management_type`, `medium`, `total_enrollment`, `grade_low/high`.
The primary purpose is the **UDISE code as the cross-registry linkage key**
(ADR-014). Do not import the ~1.5M-school universe; Tier D is out of scope
(ADR-007).

### M3-5 Serper website gap-fill — **deprioritised, may be dropped**
M0-0 measured 99.1% website coverage on the SARAS list page itself (534/539
Karnataka senior-secondary rows). Ship the `no_website` flag and stop there;
revisit only if the gap is material on the real corpus. Do not add an API key,
a budget cap and a spend log for five schools in five hundred.

Per S8. **One permitted use only:** finding a missing `website`. Accept a result
only if the domain plausibly matches the institution name; otherwise leave null
and set `no_website`. Never extract facts from snippets. Budget cap in config;
log spend to `pipeline_runs`.

### M3-6 Group directory importer
Per S7 and ADR-008. **One config-driven importer**, not one module per brand:
`groups.yaml` holds base URL, row selector, and field map per group. Seed with
Narayana, BASE, Deeksha; add others as configs. `group_type='coaching_chain'`,
`evidence` = directory URL. Apply the affordability floor when computing
`qualifying_campus_count` — a Rs 30k/yr branch is Tier D and must not inflate
chain leverage.

### M3-7 PIN centroids and metro mapping
**Moved to M1-7.** It is a one-off CSV load that blocks the geography component;
it does not belong in the parallel coverage milestone. See M1-7.

---

## M4 — Intelligence

### M4-1 Blocking, matching, group resolution
- **Goal.** `docs/ENTITY-RESOLUTION.md` stages 3-6.
- **Depends on.** M1-4, plus **at least one** of M3-1..M3-6. Cross-registry
  matching has nothing to do until a second registry exists, but it does not need
  all of them — build it against CBSE + whichever registry lands first, then the
  rest are data, not code. **It does not block M4-2** (see below).
- **Files.** `school_intel/resolve/blocking.py`, `match.py`, `groups.py`,
  `match_weights.yaml`.
- **Behaviour.** Blocking keys, weighted scoring, thresholds `0.90` / `0.65`,
  and **every hard blocker** from that document. Group resolution on the two-axis
  table.
- **Edge cases.** Branch-discriminator blocker (`North`/`South`/`Sector`/`II` plus
  differing PIN) is the highest-risk false positive — test it explicitly.
  Franchise case must produce `possible_franchise_network`, never a merge.
- **Tests.** `test_match.py` and `test_groups.py` exactly as specified in
  `docs/ENTITY-RESOLUTION.md` "Testing requirements".
- **DoD.** Mid-band pairs land in `review_queue`; nothing in the band auto-merges;
  the franchise assertions pass.

### M4-1b Group rollup — read this before M4-2
- **Goal.** Populate `groups.campus_count`, `qualifying_campus_count`, and
  `states_present`.
- **Why.** Breaks a real circular dependency. Scoring's group-leverage component
  reads `qualifying_campus_count`; if "qualifying" is implemented as "scores
  well", scoring depends on itself and the pipeline deadlocks or silently
  produces garbage on the first run.
- **Depends on.** M1-5. Uses M4-1 group resolution when it exists; with CBSE only,
  every institution is standalone and the component scores 0, which is correct
  rather than missing.
- **Files.** `school_intel/resolve/groups.py`.
- **Behaviour.** A campus qualifies on the hard gate **plus** the affordability
  floor (fee midpoint >= 100,000, or fee unknown with curriculum tier at
  `CISCE_ISC` or above). **Nothing from `score/` may be imported here.** Exact
  definition in `docs/DATA-MODEL.md` item 2.
- **Tests.** `test_group_rollup.py`: counts correct; a below-floor campus does not
  count; a fee-unknown IB campus does.
- **DoD.** Runs between resolve and score in the pipeline. `grep -r "from
  school_intel.score" school_intel/resolve/` returns nothing.

### M4-2 Scoring
- **Goal.** Fit and Confidence per `docs/SCORING.md`.
- **Depends on.** M2-4, M4-1b, M1-7. **Not M4-1, and not M3.** A CBSE-only
  ranked list is deliverable the moment enrichment and the eval harness exist —
  the sequencing summary below always said so, and the old dependency line
  contradicted it by making scoring wait on six importers. Cross-registry
  matching improves the corpus; it is not a precondition for scoring it.
  M2-4 stays a hard dependency: see the warning at the end of this file.
- **Files.** `school_intel/score/fit.py`, `confidence.py`, `flags.py`,
  `scoring.yaml`.
- **Behaviour.** Hard gate first. Six Fit components, **renormalised over
  components with data**. Confidence separately. Flags never score points.
  `components` JSONB populated exactly as shown in `docs/SCORING.md`.
- **Edge cases.** `possible < 40` -> suppress Fit, set `insufficient_data`.
  Estimated PCM gets the 0.6 component multiplier and the `pcm_estimated` flag.
  `possible_franchise_network` caps group leverage at 1.
- **Tests.** `test_fit.py`: **every affordability band boundary** (a fencepost bug
  here silently mis-ranks the whole list); renormalisation arithmetic; the
  `possible < 40` suppression; confidence independence — assert that changing only
  `confidence` never changes `fit`.
- **DoD.** `cli score` writes rows; the top 20 by Fit are manually sanity-checked
  against `docs/DECISIONS.md` ADR-007 tiers and look right to a human.

### M4-3 Signal diffing
- **Goal.** Deterministic signals from registry snapshots. ADR-002.
- **Depends on.** M1-1, two snapshots existing.
- **Files.** `school_intel/signals/snapshot.py`, `diff.py`.
- **Behaviour.** Diff the two most recent `registry_snapshots` per source. Emit
  `new_affiliation`, `level_upgrade`, `principal_change`, `address_change`,
  `disaffiliation`, `new_campus`.
- **Edge cases.** **The row-count guard is mandatory** — if the newer snapshot has
  under 50% of the previous row count, abort and alert. Without it a single parser
  regression emits thousands of false `disaffiliation` signals, which is the worst
  possible output of this system. A principal-name change that is only a
  normalisation difference (`Dr. A B Sharma` -> `A B Sharma`) must **not** emit a
  signal — compare `normalize_person` output, not raw strings.
- **Tests.** `test_diff.py`: each signal type from synthetic snapshot pairs; the
  row-count guard fires; normalisation-only changes emit nothing.
- **DoD.** Two real consecutive CBSE snapshots produce a plausible, manually
  reviewed signal set.

---

## M5 — Product

### M5-1 Search API
- **Goal.** `GET /search` with filters, sort, and pagination.
- **Depends on.** M4-2.
- **Files.** `school_intel/api/app.py`, `routes_search.py`.
- **Behaviour.** Filters: state, district, city, metro_area, board (via `EXISTS`
  on `institution_boards`, never column equality), institution_type, streams,
  fee range, PCM range, group, min fit, min confidence, flags, has_class_12.
  Sort options are specified precisely below — do not improvise them. **Reads Postgres only — never triggers a
  fetch or an LLM call** (`docs/ARCHITECTURE.md` Rule 1).
- **Sort semantics.** `[STAKEHOLDER]` fee and student-count sorting are
  explicitly required. Both are ambiguous, so both are pinned here.

  | `sort` value | Orders by | Notes |
  |---|---|---|
  | `fit` (default) | `scores.fit` | Latest score row for the active `model_version` + `campaign` |
  | `confidence` | `scores.confidence` | |
  | `fee` | **midpoint** of `fee_annual_inr_min`/`_max` | Same midpoint the affordability component uses, so the list agrees with the score. Never sort on `_min` or `_max` alone |
  | `fee_min` | `fee_annual_inr_min` | For "cheapest entry point" questions |
  | `pcm_12` | `pcm_12_count` | **The ICP-correct student count.** See below |
  | `class_12` | `class_12_total` | All streams in class 12 |
  | `enrollment` | `total_enrollment` | Whole-institution headcount |

  **"Number of students" means three different things and BD will expect the
  wrong one.** `total_enrollment` is the familiar number and the one people ask
  for; `pcm_12_count` is the one that predicts candidates (`docs/SCORING.md`).
  Expose all three as **separately labelled** options — "PCM Class 12", "Class 12
  total", "Total enrollment" — and default the student sort to `pcm_12`. Do not
  silently map a generic "students" parameter onto one of them; label it in the UI
  so the user knows which number they are looking at.

- **NULL handling — this is the bug most likely to ship.** Postgres defaults to
  `NULLS FIRST` for `DESC`. Since a large fraction of institutions will have
  `fee_unverified` or no PCM count at launch, `ORDER BY fee DESC` would put every
  unknown-value row at the top of the list and look completely broken.
  **Always append `NULLS LAST` explicitly on every sort column**, in both
  directions. Rows with a NULL sort key are returned last and rendered with a
  visible em-dash rather than a zero — a zero reads as "no students", which is a
  wrong answer, not a missing one.
- **Estimated values must be visible when sorted.** `pcm_12_is_estimated` rows
  carry the `pcm_estimated` chip in the result row. An estimated 300 sorting above
  a verified 200 is acceptable **only** if the user can see which is which.
- **Tests.** `test_search.py`: each filter; board filter correctly returns
  dual-affiliated schools; pagination stable under ties (always include a
  tiebreak on `id`). **`test_sort.py`:** fee sort uses the midpoint, not min or
  max; NULLs land last in **both** ASC and DESC; the three student-count sorts
  return genuinely different orderings on a fixture where they diverge; estimated
  rows are flagged in the payload.
- **DoD.** p95 under 300ms on the real corpus. A fee-descending list has a real
  fee at row 1, not a blank.

### M5-2 Institution profile and the `why` endpoint
- **Goal.** `GET /institutions/{id}` and `GET /institutions/{id}/why`.
- **Depends on.** M5-1.
- **Behaviour.** Profile returns canonical fields, boards, roles, group, latest
  score with the full `components` breakdown, flags, signals, and sources. `why`
  returns exactly the payload shape in `docs/ARCHITECTURE.md`, including
  conflicting observations with `lost_because`.
- **Edge cases.** `why` must work for a field with no observation (return an
  explicit "no source" rather than a 404) and for a field decided by an override
  (name the override and its `reason`).
- **Tests.** `test_why.py`: winning observation identified; losers listed with
  reasons; override-decided field attributed to the override.
- **DoD.** Every field of business consequence is explainable end to end. This is
  a stated requirement, not a nice-to-have.

### M5-3 UI
- **Goal.** Jinja + HTMX per ADR-012. `[STAKEHOLDER]` small BD team, list-out.
- **Depends on.** M5-1, M5-2.
- **Behaviour.** Three screens only: search with filter chips; institution
  profile with score breakdown and provenance; review queue for designated
  reviewers. Flags render as visible chips.
  `possible_franchise_network` must be visually distinct from a verified chain —
  that distinction is the whole point of `Project-Doc.md` 6.6.
- **DoD.** A BD user goes from a blank search to a ranked shortlist without
  reading documentation.

### M5-4 Export
- **Goal.** `GET /export` -> CSV and XLSX. This is the real deliverable.
- **Depends on.** M5-1.
- **Behaviour.** Same filters **and the same sort** as search — an export must
  come out in the order the user was looking at, or the sort feature is pointless
  the moment they leave the browser. Columns: name, type, city, state, metro,
  boards, streams, **PCM-12 (+ estimated marker), class-12 total, total
  enrollment**, **fee min / mid / max + fee year**, principal, counsellor, email,
  phone, website, group, fit, confidence, flags, top-3 score reasons, profile URL.
  Include a `why` summary column so the list is usable away from the app.
- **Edge cases.** Cap at 5,000 rows per export. `openpyxl` write-only mode.
  Never export a field flagged `insufficient_data` as though it were known.
  **Unknown fee and unknown student counts export as an empty cell, never `0`** —
  a zero in a spreadsheet gets sorted, summed, and averaged by whoever opens it,
  which silently turns "we don't know" into "this school has no students".
  Write fee columns as Excel numbers, not text, so BD can sort in Excel too.
- **DoD.** A BD user opens the file in Excel and can start calling.

### M5-5 NL query translation
- **Goal.** `docs/SCORING.md` "Natural-language search".
- **Depends on.** M5-1.
- **Behaviour.** Claude with strict structured output -> `FilterSpec` pydantic
  model -> whitelist validation -> the **same** SQL builder as M5-1. The LLM never
  emits SQL, never sees the schema beyond the whitelist, never influences ranking.
- **Edge cases.** Anything outside the whitelist -> return a clarifying question,
  never a guess. The resulting `FilterSpec` renders as editable filter chips so NL
  and manual filtering are provably the same query.
- **Tests.** `test_nlq.py`: the `Project-Doc.md` 6.13 example query decomposes
  correctly; an out-of-whitelist field is rejected; NL and equivalent manual
  filters return identical result sets.
- **DoD.** The example query returns the same rows as the hand-built filter.

### M5-6 Scheduling and observability
- **Goal.** Cron per `source_registry.refresh_days`; `pipeline_runs` populated.
- **Depends on.** everything.
- **Behaviour.** `cli schedule` enqueues due imports. Every stage writes a
  `pipeline_runs` row. Aggregate guard: institution count changing by more than
  20% between runs aborts and alerts.
- **DoD.** A full unattended monthly cycle completes and is auditable in SQL.

---

## Sequencing summary

```
M0-0 (recon)  ->  M0  ->  M1  ->  M2  ->  M4-1b, M4-2, M4-3  ->  M5
                                                                           ->  M3 (parallel)  ->  M4-1  ->  re-score
```

M3 widens the corpus and M4-1 links it together, but neither gates the first
ranked list. Scoring needs enrichment (M2) and the eval harness (M2-4), not more
registries.

Two natural demo points:

- **After M1** — ~9,600 senior-secondary CBSE schools nationally, with district,
  principal, address and website. Already useful and already more than a
  spreadsheet, and already answers "who should we call in Hyderabad".
- **After M2** — fees, PCM counts, and contacts for enriched institutions. This is
  the first version that can actually rank. Take it to BD at this point: three
  states of CBSE senior-secondary schools, ranked, exportable. Feedback on that
  list is worth more than another importer.

Do **not** start M4 scoring before M2-4 (eval) exists. Scoring on unmeasured
extraction quality produces a confident, wrong ranking — which is worse than no
ranking at all, because people act on it.
